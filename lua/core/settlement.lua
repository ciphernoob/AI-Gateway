local json=require "cjson.safe"
local config=require "core.config"
local budget=require "plugins.budget"
local audit=require "plugins.audit"
local metrics=require "plugins.observability"
local redis=require "core.redis_client"
local M={queue={},bytes=0}
function M.enqueue(job)
    local encoded=assert(json.encode(job)); local c=config.get().audit
    if #M.queue>=c.queue_max_records or M.bytes+#encoded>c.queue_max_bytes then
        metrics.inc("gateway_settlement_failures_total",{reason="queue_full"}); return false
    end
    M.queue[#M.queue+1]={data=job,bytes=#encoded,tries=0,due=ngx.now()}; M.bytes=M.bytes+#encoded
    return true
end
function M.process(job)
    if job.event and not require('core.usage_event').validate_event(job.event) then
        metrics.inc('gateway_settlement_failures_total',{reason='invalid_event'}); return false
    end
    local ok,details=true,{}
    if job.attempt and job.attempt.keys then
        ok,details=budget.settle(job.attempt,job.usage)
        if not ok then return false end
        local labels={model=job.requested_model,provider=job.attempt.candidate.provider}
        if details.quota=="applied" and job.usage then
            labels.direction="input"; metrics.inc("gateway_tokens_total",labels,job.usage.prompt_tokens)
            labels.direction="output"; metrics.inc("gateway_tokens_total",labels,job.usage.completion_tokens)
            labels.direction=nil
        end
        if details.budget=="applied" then metrics.inc("gateway_cost_micro_usd_total",labels,details.cost or 0) end
        if details.overrun then metrics.inc("gateway_budget_overruns_total",labels) end
    end
    if job.audit and audit.enabled() then
        local result=audit.call("finish",job.identity,job.audit)
        if not result then return false end
        if job.attempt and job.attempt.keys and job.attempt.audit_started then
            result=audit.call("usage",job.identity,{request_id=job.audit.request_id,attempt_id=job.attempt.attempt_id,
                usage=job.usage,cost=details.cost,usage_source=job.audit.usage_source,version=1})
            if not result then return false end
        end
    end
    return true
end
function M.tick(premature)
    if premature or M.busy then return end
    M.busy=true
    local pending=M.queue; M.queue={}
    for _,entry in ipairs(pending) do
        if entry.due>ngx.now() then M.queue[#M.queue+1]=entry
        else
            local called,ok=pcall(M.process,entry.data)
            if called and ok then M.bytes=M.bytes-entry.bytes
            else
                entry.tries=entry.tries+1
                if entry.tries<config.get().audit.retry_count then
                    entry.due=ngx.now()+math.min(2^(entry.tries-1),8); M.queue[#M.queue+1]=entry
                else
                    M.bytes=M.bytes-entry.bytes; metrics.inc("gateway_settlement_failures_total",{reason="retries_exhausted"})
                    metrics.log({event="settlement_incomplete",request_id=entry.data.audit and entry.data.audit.request_id,
                        attempt_id=entry.data.attempt and entry.data.attempt.attempt_id})
                end
            end
        end
    end
    metrics.set("gateway_audit_queue_bytes",{worker=tostring(ngx.worker.id())},M.bytes)
    M.busy=false
end
function M.recover(premature)
    if premature then return end
    local client=redis.connect(); if not client then return end
    local keys=client:zrangebyscore("usage:pending",0,math.floor(ngx.now()),"LIMIT",0,100)
    if type(keys)=="table" then
        for _,key in ipairs(keys) do
            local raw=client:hget(key,"meta")
            if type(raw)=="string" then
                local meta=json.decode(raw)
                local claim=redis.eval(meta.keys,{op="claim",now=math.floor(ngx.now())})
                if claim and claim[1]~="busy" then
                    local exact
                    local saved=client:hget(key,"usage")
                    if type(saved)=="string" then exact=json.decode(saved)
                    elseif claim[1]=="unsent" then exact={prompt_tokens=0,completion_tokens=0,total_tokens=0} end
                    budget.settle({keys=meta.keys,candidate=meta.candidate},exact)
                end
            end
        end
    end
    local cfg=config.get()
    for logical in pairs(cfg.models) do
        local values=client:hmget("budget:ledger:"..cfg.budget.epoch..":model:"..logical,"spent","reserved")
        if values then
            local spent=tonumber(values[1]) or 0; local reserved=tonumber(values[2]) or 0
            metrics.set("gateway_budget_remaining_micro_usd",{model=logical},math.max(cfg.budget.model_limits[logical]-spent-reserved,0))
        end
    end
    metrics.set("gateway_unsettled_attempts",nil,tonumber(client:zcard("usage:pending")) or 0)
    redis.close(client)
end
return M
