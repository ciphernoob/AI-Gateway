local redis=require "core.redis_client"
local config=require "core.config"
local json=require "cjson.safe"
local M={}
function M.periods(now)
    local shifted=now+28800
    local day=os.date("!%Y-%m-%d",shifted); local month=day:sub(1,7)
    local nextday=math.floor(shifted/86400)*86400+86400-28800
    local t=os.date("!*t",shifted)
    local nextmonth=os.time({year=t.year,month=t.month+1,day=1,hour=0,min=0,sec=0})-28800
    return day,month,nextday,nextmonth
end
function M.keys(user_id,attempt_id,logical,day,month)
    return {"usage:attempt:"..attempt_id,"usage:pending",
        "quota:usage:"..user_id..":"..logical..":"..day,
        "quota:usage:"..user_id..":"..logical..":"..month}
end
function M.begin(ctx,attempt)
    local cfg=config.get(); local now=math.floor(attempt.started_at)
    local day,month,day_end,month_end=M.periods(now)
    attempt.period_day=day; attempt.period_month=month; attempt.logical_model=ctx.requested_model
    attempt.keys=M.keys(ctx.identity.user_id,attempt.attempt_id,attempt.logical_model,day,month)
    local meta={attempt_id=attempt.attempt_id,request_id=ctx.request_id,trace_id=ctx.trace_id,user_id=ctx.identity.user_id,
        agent_id=ctx.identity.agent_id,logical_model=attempt.logical_model,provider=attempt.candidate.provider,
        started_at=now,deadline=math.ceil(ctx.deadline)+10,day_expiry=day_end+cfg.usage.daily_retention_days*86400,
        month_expiry=month_end+cfg.usage.monthly_retention_days*86400,dedupe_expiry=month_end+cfg.usage.dedupe_retention_days*86400,
        replay_max_days=cfg.usage.replay_max_days,keys=attempt.keys}
    local result,err=redis.eval(attempt.keys,{op="begin",now=now,meta=meta,max_pending=cfg.redis.max_pending})
    if not result or (result[1]~="created" and result[1]~="exists") then return nil,err or "accounting_unavailable" end
    return true
end
function M.dispatch(attempt)
    local result=redis.eval(attempt.keys,{op="dispatch",now=math.floor(ngx.now())})
    return result and result[1]=="dispatching"
end
function M.unsent(attempt)
    local result=redis.eval(attempt.keys,{op="unsent",now=math.floor(ngx.now())})
    return result and (result[1]=="unsent" or result[1]=="duplicate")
end
function M.settle(attempt,exact)
    local result,err=redis.eval(attempt.keys,{op="settle",now=math.floor(ngx.now()),usage=exact or json.null})
    local accepted={applied=true,duplicate=true,unknown=true,unsent=true}
    return result and accepted[result[1]] or false,{status=result and result[1],error=err}
end
return M
