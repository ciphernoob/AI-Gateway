local budget=require "plugins.budget"
local redis=require "core.redis_client"
local cfg=require "core.config"
local json=require "cjson.safe"
local M={}
function M.query(identity)
    local day,month=budget.periods(math.floor(ngx.now()))
    local keys=budget.keys(identity,"query","query",day,month)
    local result,err=redis.eval(keys,{op="query",now=math.floor(ngx.now())})
    if not result then return nil,err end
    local user=cfg.get().users[identity.user_id]
    local function window(values,period,limit)
        local fields=redis.hash(values); local out={period=period}
        for _,name in ipairs({"prompt_tokens","completion_tokens","total_tokens","pending_attempts","unknown_attempts"}) do out[name]=tonumber(fields[name]) or 0 end
        if limit==json.null then limit=nil end
        out.token_limit=limit or json.null
        out.remaining_tokens=limit and math.max(limit-out.total_tokens,0) or json.null
        out.remaining_is_exact=limit~=nil and out.pending_attempts==0 and out.unknown_attempts==0
        return out
    end
    return {user_id=identity.user_id,timezone="Asia/Shanghai",enforcement=false,consistency="eventual",
        daily=window(result[1],day,user.daily_token_limit),monthly=window(result[2],month,user.monthly_token_limit)}
end
return M
