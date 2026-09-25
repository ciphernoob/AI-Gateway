local json=require "cjson.safe"
local config=require "core.config"
local M={}
local types={}
local buckets={0.01,0.05,0.1,0.25,0.5,1,2,5,10,30,60}
local function suffix(labels)
    if not labels then return "" end
    local items={}; for k,v in pairs(labels) do items[#items+1]=k..'="'..tostring(v):gsub('\\','\\\\'):gsub('"','\\"'):gsub('\n','\\n')..'"' end
    table.sort(items); return "{"..table.concat(items,",").."}"
end
function M.inc(name,labels,value)
    if not config.get().plugins.observability then return end
    types[name]="counter"
    ngx.shared.metrics:incr(name..suffix(labels),value or 1,0)
end
function M.set(name,labels,value)
    if not config.get().plugins.observability then return end
    types[name]="gauge"; ngx.shared.metrics:set(name..suffix(labels),value)
end
function M.observe(name,labels,value)
    if not value then return end
    M.inc(name.."_sum",labels,value); M.inc(name.."_count",labels,1)
    local copy={}; for k,v in pairs(labels or {}) do copy[k]=v end
    for _,le in ipairs(buckets) do
        copy.le=tostring(le)
        M.inc(name.."_bucket",copy,value<=le and 1 or 0)
    end
    copy.le="+Inf"; M.inc(name.."_bucket",copy,1)
end
function M.log(event)
    -- Callers construct metadata allowlists; never pass request bodies or errors.
    event.config_revision=config.get().revision or 'bootstrap'
    -- Correlation IDs may come from clients. Never let a known credential be
    -- copied into metadata even when submitted as an otherwise valid trace ID.
    for field,value in pairs(event) do
        if type(value)=='string' then
            for _,secret in ipairs(config.get().known_secrets or {}) do
                value=value:gsub(secret:gsub('(%W)','%%%1'),'[REDACTED]')
            end
            event[field]=value
        end
    end
    ngx.log(ngx.NOTICE,assert(json.encode(event)))
end
function M.render()
    ngx.header.content_type="text/plain; version=0.0.4"
    local keys=ngx.shared.metrics:get_keys(0); table.sort(keys)
    for _,key in ipairs(keys) do ngx.say(key.." "..tostring(ngx.shared.metrics:get(key) or 0)) end
end
return M
