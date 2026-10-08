local accounting=require "core.accounting"
local redis=require "core.redis_client"
local cfg=require "core.config"
local json=require "cjson.safe"
local M={}
local fields={"prompt_tokens","completion_tokens","total_tokens","pending_attempts","unknown_attempts"}
local function empty(period) return {period=period,prompt_tokens=0,completion_tokens=0,total_tokens=0,pending_attempts=0,unknown_attempts=0} end
local function window(values,period)
    local raw=redis.hash(values); local out=empty(period)
    for _,name in ipairs(fields) do out[name]=tonumber(raw[name]) or 0 end
    return out
end
local function add(target,value)
    for _,name in ipairs(fields) do target[name]=target[name]+value[name] end
end
local function limit(window,value)
    if value==json.null then value=nil end
    window.token_limit=value or json.null
    window.remaining_tokens=value and math.max(value-window.total_tokens,0) or json.null
    window.remaining_is_exact=value~=nil and window.pending_attempts==0 and window.unknown_attempts==0
end
function M.query(identity,only_model,day,month)
    local now=math.floor(ngx.now()); local current_day,current_month=accounting.periods(now)
    day=day or current_day; month=month or current_month
    local config=cfg.get(); local summary_day=empty(day); local summary_month=empty(month); local models={}
    for logical in pairs(config.models) do
        if not only_model or logical==only_model then
            local keys=accounting.keys(identity.user_id,"query",logical,day,month)
            local result,err=redis.eval(keys,{op="query",now=now})
            if not result then return nil,err end
            local daily=window(result[1],day); local monthly=window(result[2],month)
            add(summary_day,daily); add(summary_month,monthly)
            models[#models+1]={model=logical,daily=daily,monthly=monthly}
        end
    end
    table.sort(models,function(a,b) return a.model<b.model end)
    local user=config.users[identity.user_id]
    limit(summary_day,user.daily_token_limit); limit(summary_month,user.monthly_token_limit)
    return {user_id=identity.user_id,timezone="Asia/Shanghai",enforcement=false,consistency="eventual",
        daily=summary_day,monthly=summary_month,models=models}
end
return M
