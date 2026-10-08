-- KEYS: attempt, pending zset, per-user/per-model day, per-user/per-model month.
-- Provider usage is the only numeric Token source. No monetary state lives here.
local a=cjson.decode(ARGV[1])
local op=a.op
local MAX=9007199254740991
local function number(v)
    local n=tonumber(v)
    if not n or n<0 or n>MAX or n~=math.floor(n) then error("invalid_integer") end
    return n
end
local function get(key,field) return number(redis.call("HGET",key,field) or "0") end
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
    number(nextval); plan[key][field]=nextval
end
local function apply(plan)
    for key,fields in pairs(plan) do
        for field,value in pairs(fields) do redis.call("HSET",key,field,string.format("%.0f",value)) end
    end
end
local function close(m)
    redis.call("ZREM",KEYS[2],KEYS[1]); redis.call("EXPIREAT",KEYS[1],m.dedupe_expiry)
end
if op=="begin" then
    if exists() then return {"exists"} end
    if redis.call("ZCARD",KEYS[2])>=number(a.max_pending) then return {"capacity"} end
    local m=a.meta; local plan={}
    add(plan,KEYS[3],"pending_attempts",1); add(plan,KEYS[4],"pending_attempts",1); apply(plan)
    redis.call("HSET",KEYS[1],"meta",cjson.encode(m),"usage_state","pending","dispatch","created")
    redis.call("ZADD",KEYS[2],m.deadline,KEYS[1])
    redis.call("EXPIREAT",KEYS[3],m.day_expiry); redis.call("EXPIREAT",KEYS[4],m.month_expiry)
    return {"created"}
elseif op=="query" then
    return {redis.call("HGETALL",KEYS[3]),redis.call("HGETALL",KEYS[4])}
end
if not exists() then return {"missing"} end
local m=meta()
if op=="dispatch" then
    if redis.call("HGET",KEYS[1],"dispatch")~="created" or now>m.deadline then return {"closed"} end
    redis.call("HSET",KEYS[1],"dispatch","dispatching"); return {"dispatching"}
elseif op=="unsent" then
    local previous=redis.call("HGET",KEYS[1],"usage_state")
    if previous=="unsent" then return {"duplicate"} end
    if previous~="pending" then return {"conflict"} end
    local plan={}
    for i=3,4 do if now<(i==3 and m.day_expiry or m.month_expiry) then add(plan,KEYS[i],"pending_attempts",-1) end end
    apply(plan); redis.call("HSET",KEYS[1],"usage_state","unsent","dispatch","closed"); close(m)
    return {"unsent"}
elseif op=="claim" then
    local due=tonumber(redis.call("ZSCORE",KEYS[2],KEYS[1]) or "0")
    if due>now then return {"busy"} end
    local dispatched=redis.call("HGET",KEYS[1],"dispatch")
    redis.call("HSET",KEYS[1],"dispatch","closed")
    if dispatched=="created" then
        local plan={}
        for i=3,4 do if now<(i==3 and m.day_expiry or m.month_expiry) then add(plan,KEYS[i],"pending_attempts",-1) end end
        apply(plan); redis.call("HSET",KEYS[1],"usage_state","unsent"); close(m); return {"unsent"}
    end
    redis.call("ZADD",KEYS[2],now+60,KEYS[1]); return {"unknown"}
elseif op=="settle" then
    if now>m.started_at+m.replay_max_days*86400 then return {"too_old"} end
    local previous=redis.call("HGET",KEYS[1],"usage_state")
    local known=a.usage~=cjson.null and type(a.usage)=="table"; local fingerprint="unknown"
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
    if previous=="unknown" and known then return {"conflict"} end
    if previous=="unsent" then return {"unsent"} end
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
    redis.call("HSET",KEYS[1],"usage_state",known and "known" or "unknown","usage_fingerprint",fingerprint,"dispatch","closed")
    if known then redis.call("HSET",KEYS[1],"usage",cjson.encode(a.usage)) end
    close(m); return {"applied"}
end
return redis.error_reply("unknown_operation")
