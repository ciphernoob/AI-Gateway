local json = require "cjson.safe"
local M = {}
function M.exit(status, code, message)
    local ctx = require("core.context").get()
    if ctx then ctx.gateway_error = code; ctx.final_status = status; require("core.context").save(ctx) end
    ngx.status = status
    ngx.header.content_type = "application/json"
    ngx.header["X-Request-ID"] = ctx and ctx.request_id
    ngx.say(assert(json.encode({error={code=code, type="gateway_error", message=message or code}, request_id=ctx and ctx.request_id})))
    return ngx.exit(status)
end
return M
