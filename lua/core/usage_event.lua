local M={}
local json=require 'cjson.safe'
local function count(n) return type(n)=="number" and n>=0 and n<9007199254740992 and n==math.floor(n) end
function M.validate(usage)
    if type(usage)~="table" then return nil end
    local p,c,t=usage.prompt_tokens,usage.completion_tokens,usage.total_tokens
    if not count(p) or not count(c) or not count(t) or p+c~=t then return nil end
    return {prompt_tokens=p,completion_tokens=c,total_tokens=t}
end
function M.cost(candidate, usage)
    local product=usage.prompt_tokens*candidate.input_rate+usage.completion_tokens*candidate.output_rate
    if product>=9007199254740992 or product<0 then return nil,"cost_overflow" end
    return math.ceil(product/1000000)
end
function M.reserve(candidate, output, n)
    -- Conservatively allow a provider to bill the input once per completion.
    return M.cost(candidate, {prompt_tokens=candidate.context_tokens*n,completion_tokens=output*n})
end
function M.new(ctx,attempt,exact,status,finished_at)
    return {schema_version=1,request_id=ctx.request_id,attempt_id=attempt.attempt_id,trace_id=ctx.trace_id,
        user_id=ctx.identity.user_id,agent_id=ctx.identity.agent_id,model=ctx.requested_model,
        actual_model=attempt.candidate.model,provider=attempt.candidate.provider,
        price_version=attempt.candidate.price_version,started_at=attempt.started_at,finished_at=finished_at,
        prompt_tokens=exact and exact.prompt_tokens or json.null,
        completion_tokens=exact and exact.completion_tokens or json.null,
        total_tokens=exact and exact.total_tokens or json.null,
        usage_source=exact and (attempt.unsent and 'system' or 'provider') or 'unknown',
        usage_status=exact and 'known' or 'unknown',status=status}
end
function M.validate_event(event)
    if type(event)~='table' or event.schema_version~=1 then return false end
    for _,field in ipairs({'request_id','attempt_id','user_id','model','actual_model','provider','price_version'}) do
        if type(event[field])~='string' then return false end
    end
    if type(event.started_at)~='number' or type(event.finished_at)~='number' or event.finished_at<event.started_at then return false end
    if event.status~='completed' and event.status~='failed' and event.status~='interrupted' then return false end
    if event.usage_status=='unknown' then
        return event.usage_source=='unknown' and event.prompt_tokens==json.null and event.completion_tokens==json.null and event.total_tokens==json.null
    end
    return event.usage_status=='known' and M.validate(event)~=nil and
        (event.usage_source=='provider' or (event.usage_source=='system' and event.total_tokens==0))
end
return M
