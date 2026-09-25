-- KEYS: attempt, pending zset, day, month, reservation, global, agent, model.
-- Each operation is an independent consumer transaction. No content is stored here.
local a=cjson.decode(ARGV[1])
local op=a.op
local MAX=9007199254740991
local function number(v)
    local n=tonumber(v)
    if not n or n<0 or n>MAX or n~=math.floor(n) then error("invalid_integer") end
    return n
end
local function get(key, field) return number(redis.call("HGET",key,field) or "0") end
for i,key in ipairs(KEYS) do
    local kind=redis.call("TYPE",key).ok
    if kind~="none" and kind~=(i==2 and "zset" or "hash") then return redis.error_reply("invalid_key_type") end
end
local now=number(a.now)
local function exists() return redis.call("EXISTS",KEYS[1])==1 end
local function meta() return cjson.decode(redis.call("HGET",KEYS[1],"meta")) end
local function add(plan,key,field,amount)
    plan[key]=plan[key] or {}
    local nextval=(plan[key][field] or get(key,field))+amount
    number(nextval)
    plan[key][field]=nextval
end
local function apply(plan)
    for key,fields in pairs(plan) do for field,value in pairs(fields) do redis.call("HSET",key,field,string.format("%.0f",value)) end end
end
local function cleanup(m)
    local q=redis.call("HGET",KEYS[1],"quota_state")
    local b=redis.call("HGET",KEYS[1],"budget_state")
    if q=="known" and (b=="known" or b=="disabled") then
        redis.call("ZREM",KEYS[2],KEYS[1])
        redis.call("EXPIREAT",KEYS[1],m.dedupe_expiry)
        if redis.call("EXISTS",KEYS[5])==1 then redis.call("EXPIREAT",KEYS[5],m.dedupe_expiry) end
    end
end
if op=="begin" then
    if exists() then return {"exists"} end
    if redis.call("ZCARD",KEYS[2])>=number(a.max_pending) then return {"capacity"} end
    local m=a.meta; local plan={}
    if m.quota_enabled then
        add(plan,KEYS[3],"pending_attempts",1); add(plan,KEYS[4],"pending_attempts",1)
    end
    apply(plan)
    redis.call("HSET",KEYS[1],"meta",cjson.encode(m),"quota_state",m.quota_enabled and "pending" or "known",
        "budget_state",m.budget_enabled and "pending" or "disabled","dispatch","created")
    redis.call("ZADD",KEYS[2],m.deadline,KEYS[1])
    if m.quota_enabled then
        redis.call("EXPIREAT",KEYS[3],m.day_expiry); redis.call("EXPIREAT",KEYS[4],m.month_expiry)
    end
    return {"created"}
elseif op=="query" then
    return {redis.call("HGETALL",KEYS[3]),redis.call("HGETALL",KEYS[4])}
end
if not exists() then return {"missing"} end
local m=meta()
if op=="reserve" then
    if not m.budget_enabled then return {"disabled"} end
    if redis.call("HGET",KEYS[1],"dispatch")~="created" or now>m.deadline then return {"closed"} end
    if redis.call("EXISTS",KEYS[5])==1 then return {"exists"} end
    local amount=number(a.amount); local plan={}
    for _,i in ipairs(m.dimensions) do
        local limit=number(m.limits[i-5]); local spent=get(KEYS[i],"spent"); local reserved=get(KEYS[i],"reserved")
        if spent+reserved+amount>limit then return {"denied"} end
        add(plan,KEYS[i],"reserved",amount)
    end
    apply(plan)
    for _,i in ipairs(m.dimensions) do redis.call("HSET",KEYS[i],"limit",m.limits[i-5]) end
    redis.call("HSET",KEYS[5],"amount",amount,"state","reserved")
    return {"reserved"}
elseif op=="dispatch" then
    if redis.call("HGET",KEYS[1],"dispatch")~="created" or now>m.deadline then return {"closed"} end
    if m.budget_enabled and redis.call("HGET",KEYS[5],"state")~="reserved" then return {"unreserved"} end
    redis.call("HSET",KEYS[1],"dispatch","dispatching")
    return {"dispatching"}
elseif op=="claim" then
    local due=tonumber(redis.call("ZSCORE",KEYS[2],KEYS[1]) or "0")
    if due>now then return {"busy"} end
    local dispatched=redis.call("HGET",KEYS[1],"dispatch")
    redis.call("HSET",KEYS[1],"dispatch","closed")
    redis.call("ZADD",KEYS[2],now+60,KEYS[1])
    return {dispatched=="created" and "unsent" or "unknown"}
elseif op=="quota" then
    if not m.quota_enabled then cleanup(m); return {"disabled"} end
    if not a.reconcile and now>m.started_at+m.replay_max_days*86400 then return {"too_old"} end
    local previous=redis.call("HGET",KEYS[1],"quota_state")
    local known=a.usage~=cjson.null and type(a.usage)=="table"
    local fingerprint="unknown"
    if known then
        local p=number(a.usage.prompt_tokens); local c=number(a.usage.completion_tokens); local t=number(a.usage.total_tokens)
        if p+c~=t then return {"invalid_usage"} end
        fingerprint=string.format("%.0f:%.0f:%.0f",p,c,t)
    end
    if previous=="known" then
        if redis.call("HGET",KEYS[1],"usage_fingerprint")==fingerprint then return {"duplicate"} end
        return {"conflict"}
    end
    if previous=="unknown" and not known then return {"duplicate"} end
    local plan={}
    for i=3,4 do
        local expiry=i==3 and m.day_expiry or m.month_expiry
        if now<expiry then
            if previous=="pending" then add(plan,KEYS[i],"pending_attempts",-1)
            elseif previous=="unknown" then add(plan,KEYS[i],"unknown_attempts",-1) end
            if known then
                for _,field in ipairs({"prompt_tokens","completion_tokens","total_tokens"}) do add(plan,KEYS[i],field,a.usage[field]) end
            else add(plan,KEYS[i],"unknown_attempts",1) end
        end
    end
    apply(plan)
    redis.call("HSET",KEYS[1],"quota_state",known and "known" or "unknown","usage_fingerprint",fingerprint)
    if known then redis.call("HSET",KEYS[1],"usage",cjson.encode(a.usage)) end
    if a.reconcile then redis.call("HSET",KEYS[1],"reconciliation",a.evidence or "") end
    cleanup(m)
    return {"applied"}
elseif op=="budget" then
    if not m.budget_enabled then cleanup(m); return {"disabled"} end
    if not a.reconcile and now>m.started_at+m.replay_max_days*86400 then return {"too_old"} end
    local previous=redis.call("HGET",KEYS[1],"budget_state")
    if a.cost==cjson.null or a.cost==nil then
        if previous~="known" then redis.call("HSET",KEYS[1],"budget_state","unknown") end
        return {"unknown"}
    end
    local cost=number(a.cost)
    if previous=="known" then
        if get(KEYS[1],"actual_cost")==cost then return {"duplicate"} end
        return {"conflict"}
    end
    local reserved=get(KEYS[5],"amount")
    if redis.call("EXISTS",KEYS[5])==0 and cost~=0 then return {"unreserved"} end
    local plan={}
    for _,i in ipairs(m.dimensions) do
        add(plan,KEYS[i],"reserved",-reserved); add(plan,KEYS[i],"spent",cost)
    end
    apply(plan)
    redis.call("HSET",KEYS[1],"budget_state","known","actual_cost",cost)
    redis.call("HSET",KEYS[5],"state","settled","actual_cost",cost)
    cleanup(m)
    return {"applied",cost>reserved and "overrun" or "within_reservation"}
end
return redis.error_reply("unknown_operation")
