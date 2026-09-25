local M = {}
local order = {"identity", "unified_api", "fallback", "usage_collector", "budget", "user_quota", "audit", "observability"}
function M.build(flags, modules)
    assert(flags.identity and flags.unified_api, "public endpoints require identity and unified API")
    assert(not (flags.budget or flags.user_quota or flags.audit) or flags.usage_collector, "missing usage collector")
    local enabled = {}
    for _, name in ipairs(order) do
        if flags[name] and modules[name] then enabled[#enabled+1] = modules[name] end
    end
    return {run=function(_, phase, ctx)
        for _, module in ipairs(enabled) do
            if module[phase] then
                local ok, err = module[phase](ctx)
                if ok == false then return false, err end
            end
        end
        return true
    end}
end
return M
