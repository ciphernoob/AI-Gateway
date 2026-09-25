local json = require "cjson.safe"
local M = {}
function M.load(path)
    local file = assert(io.open(path or "/runtime/config.json", "rb"))
    local value = assert(json.decode(file:read("*a")))
    file:close()
    M.value = value
    return value
end
function M.get() return assert(M.value, "configuration not loaded") end
return M
