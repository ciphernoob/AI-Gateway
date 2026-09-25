local M={}
local json=require 'cjson.safe'
function M.quota_error(status,headers,body,codes)
    if status~=402 and status~=429 then return nil end
    if #body>65536 or not (headers['content-type'] or ''):lower():match('^application/json') then return nil end
    local value=json.decode(body)
    if type(value)~='table' or type(value.error)~='table' then return nil end
    if value.choices~=nil and value.choices~=json.null and (type(value.choices)~='table' or next(value.choices)) then return nil end
    for _,code in ipairs(codes) do
        if value.error.code==code or value.error.type==code then return code end
    end
end
function M.allowed(state)
    return (state.status==502 or state.status==504) and state.sent=="0" and state.received=="0"
        and (state.header_time=="-" or state.header_time==nil) and not state.headers_sent
        and state.count<state.max_attempts and state.has_candidate and state.now<state.deadline
end
return M
