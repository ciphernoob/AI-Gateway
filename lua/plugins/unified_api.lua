local json = require "cjson.safe"
local M = {}
local function integer(x) return type(x)=="number" and x>=1 and x<9007199254740992 and x==math.floor(x) end
function M.validate(body, config)
    if type(body)~="table" or type(body.model)~="string" or not config.models[body.model] then return nil, "invalid_model" end
    if type(body.messages)~="table" or #body.messages==0 then return nil, "invalid_messages" end
    local roles={system=true,developer=true,user=true,assistant=true,tool=true}
    for _, message in ipairs(body.messages) do
        if type(message)~="table" or not roles[message.role] then return nil,"invalid_messages" end
        if message.content==nil and message.tool_calls==nil then return nil,"invalid_messages" end
    end
    if body.stream~=nil and type(body.stream)~="boolean" then return nil,"invalid_stream" end
    if body.max_tokens and body.max_completion_tokens then return nil,"conflicting_output_limits" end
    local output = body.max_completion_tokens or body.max_tokens or config.request.default_output_tokens
    local n = body.n or 1
    if not integer(output) or not integer(n) then return nil,"invalid_output_limit" end
    if body.tools~=nil and (type(body.tools)~="table" or #body.tools==0) then return nil,"invalid_tools" end
    local candidates={}
    for _, candidate in ipairs(config.models[body.model].candidates) do
        local caps={}; for _, cap in ipairs(candidate.capabilities) do caps[cap]=true end
        if output<=candidate.max_output_tokens and n<=candidate.n_max and (not body.stream or caps.stream)
            and (not (body.tools or body.tool_choice) or caps.tools) then candidates[#candidates+1]=candidate end
    end
    if #candidates==0 then return nil,"unsupported_capability_or_limit" end
    -- A candidate failing capability checks is never silently used.
    return {candidates=candidates, output_tokens=output, n=n, stream=body.stream==true, requested_model=body.model}
end
function M.adapt(body, candidate, output)
    local value = assert(json.decode(assert(json.encode(body))))
    value.model = candidate.model
    value.user_id=nil; value.agent_id=nil; value.skip_audit=nil
    if not value.max_completion_tokens and not value.max_tokens then value.max_tokens=output end
    if value.stream then
        if candidate.supports_stream_usage then
            value.stream_options = type(value.stream_options)=="table" and value.stream_options or {}
            value.stream_options.include_usage=true
        else value.stream_options=nil end
    end
    return value
end
return M
