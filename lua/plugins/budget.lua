local redis=require "core.redis_client"
local config=require "core.config"
local usage=require "core.usage_event"
local json=require "cjson.safe"
local M={}
function M.periods(now)
    local shifted=now+28800
    local day=os.date("!%Y-%m-%d",shifted); local month=day:sub(1,7)
    local nextday=math.floor(shifted/86400)*86400+86400-28800
    local t=os.date("!*t",shifted)
    -- Containers run in UTC; calendar rollover uses a UTC process environment.
    local nextmonth=os.time({year=t.year,month=t.month+1,day=1,hour=0,min=0,sec=0})-28800
    return day,month,nextday,nextmonth
end
function M.keys(identity,attempt_id,logical,day,month)
    local epoch=config.get().budget.epoch
    return {"usage:attempt:"..attempt_id,"usage:pending","quota:usage:"..identity.user_id..":"..day,
        "quota:usage:"..identity.user_id..":"..month,"budget:reservation:"..attempt_id,
        "budget:ledger:"..epoch..":global","budget:ledger:"..epoch..":agent:"..(identity.agent_id or "_none"),
        "budget:ledger:"..epoch..":model:"..logical}
end
function M.begin(ctx,attempt)
    local cfg=config.get(); local now=math.floor(attempt.started_at)
    local day,month,day_end,month_end=M.periods(now)
    attempt.period_day=day; attempt.period_month=month
    attempt.keys=M.keys(ctx.identity,attempt.attempt_id,ctx.requested_model,day,month)
    local meta={attempt_id=attempt.attempt_id,request_id=ctx.request_id,trace_id=ctx.trace_id,user_id=ctx.identity.user_id,
        agent_id=ctx.identity.agent_id,requested_model=ctx.requested_model,provider=attempt.candidate.provider,candidate=attempt.candidate,
        started_at=now,deadline=math.ceil(ctx.deadline)+10,day_expiry=day_end+cfg.usage.daily_retention_days*86400,
        month_expiry=month_end+cfg.usage.monthly_retention_days*86400,dedupe_expiry=month_end+cfg.usage.dedupe_retention_days*86400,
        replay_max_days=cfg.usage.replay_max_days,keys=attempt.keys,quota_enabled=cfg.plugins.user_quota,budget_enabled=cfg.plugins.budget,
        dimensions=ctx.identity.agent_id and {6,7,8} or {6,8},
        limits={cfg.budget.global_limit,cfg.budget.agent_limits[ctx.identity.agent_id or ""] or 0,cfg.budget.model_limits[ctx.requested_model]}}
    local result,err=redis.eval(attempt.keys,{op="begin",now=now,meta=meta,max_pending=cfg.redis.max_pending})
    if not result or (result[1]~="created" and result[1]~="exists") then return nil,err or "accounting_unavailable" end
    return true
end
function M.reserve(ctx,attempt)
    if not config.get().plugins.budget then return true end
    local amount,err=usage.reserve(attempt.candidate,ctx.output_tokens,ctx.n)
    if not amount then return nil,err end
    attempt.reserved=amount
    local result; result,err=redis.eval(attempt.keys,{op="reserve",now=math.floor(ngx.now()),amount=amount})
    if not result then return nil,"accounting_unavailable" end
    if result[1]=="denied" then return nil,"budget_exceeded" end
    return result[1]=="reserved" or result[1]=="exists",err
end
function M.dispatch(attempt)
    local result=redis.eval(attempt.keys,{op="dispatch",now=math.floor(ngx.now())})
    return result and result[1]=="dispatching"
end
function M.settle(attempt,exact,reconcile,evidence)
    local now=math.floor(ngx.now())
    local q,qe=redis.eval(attempt.keys,{op="quota",now=now,usage=exact or json.null,reconcile=reconcile,evidence=evidence})
    if not q or q[1]=='conflict' or q[1]=='too_old' or q[1]=='invalid_usage' then return false,{error=qe or (q and q[1])} end
    local cost=exact and usage.cost(attempt.candidate,exact) or nil
    local b,be=redis.eval(attempt.keys,{op="budget",now=now,cost=cost or json.null,reconcile=reconcile,evidence=evidence})
    local accepted={applied=true,duplicate=true,disabled=true,unknown=true}
    return q and b and accepted[q[1]] and accepted[b[1]],{quota=q and q[1],budget=b and b[1],cost=cost,overrun=b and b[2]=="overrun",error=qe or be}
end
return M
