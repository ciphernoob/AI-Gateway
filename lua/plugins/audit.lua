local http=require "core.internal_http"
local json=require "cjson.safe"
local config=require "core.config"
local M={}
function M.enabled() return config.get().plugins.audit end
function M.call(method,identity,data)
    if not M.enabled() then return {ok=true},200 end
    local c=config.get().audit
    local client=http.new(); client:set_timeout(config.get().redis.timeout_ms)
    local res,err=client:request_uri(c.base_url.."/internal/"..method,{method="POST",
        body=assert(json.encode({identity=identity,data=data})),headers={["Content-Type"]="application/json",Authorization="Bearer "..c.token}})
    if not res then
        require('plugins.observability').inc('gateway_audit_failures_total',{stage=method,reason='transport'})
        return nil,503,"audit_unavailable"
    end
    local value=json.decode(res.body)
    if res.status>=400 then
        require('plugins.observability').inc('gateway_audit_failures_total',{stage=method,reason=res.status>=500 and 'server' or 'rejected'})
        return nil,res.status,value and value.error and value.error.code or "audit_unavailable"
    end
    return value,res.status
end
function M.prepare(ctx,body)
    if not M.enabled() then return true end
    local data={request_id=ctx.request_id,trace_id=ctx.trace_id,trace_provided=ctx.trace_provided,
        parent_request_id=ctx.parent_request_id,conversation_id=ctx.conversation_id,requested_model=ctx.requested_model,
        deadline=ctx.deadline,request=body}
    return M.call("prepare",ctx.identity,data)
end
function M.attempt(ctx,attempt,body)
    return M.call("attempt",ctx.identity,{request_id=ctx.request_id,attempt_id=attempt.attempt_id,
        provider=attempt.candidate.provider,model=attempt.candidate.model,
        started_at=attempt.started_at,request=body})
end
return M
