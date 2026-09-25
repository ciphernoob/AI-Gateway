local sha = require "resty.sha256"
local hex = require "resty.string".to_hex
local M = {}
function M.authenticate(header, keys, scope)
    if type(header) ~= "string" then return nil, 401 end
    local token = header:match("^Bearer ([^%s]+)$")
    if not token then return nil, 401 end
    local hash = sha:new(); hash:update(token)
    local digest = hex(hash:final())
    for _, key in ipairs(keys) do
        if key.digest == digest and not key.disabled then
            for _, allowed in ipairs(key.scopes) do
                if allowed == scope then return {user_id=key.user_id, agent_id=key.agent_id, scopes=key.scopes} end
            end
            return nil, 403
        end
    end
    return nil, 401
end
return M
