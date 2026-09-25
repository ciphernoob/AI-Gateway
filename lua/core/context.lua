local json = require "cjson.safe"
local random = require "resty.random"
local str = require "resty.string"
local M = {}
function M.id(prefix) return prefix .. str.to_hex(assert(random.bytes(16, true))) end
function M.new()
    return {request_id=M.id("req_"), started_at=ngx.now(), attempts={}, fallback_count=0}
end
function M.save(ctx)
    -- Only metadata crosses an internal redirect. Request body has its own bounded variable.
    ngx.var.gw_state = assert(json.encode(ctx))
    ngx.ctx.gateway = ctx
end
function M.get()
    if ngx.ctx.gateway then return ngx.ctx.gateway end
    if ngx.var.gw_state and ngx.var.gw_state ~= "" then
        ngx.ctx.gateway = assert(json.decode(ngx.var.gw_state))
        return ngx.ctx.gateway
    end
end
return M
