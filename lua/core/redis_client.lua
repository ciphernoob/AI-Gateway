local redis=require "resty.redis"
local json=require "cjson.safe"
local config=require "core.config"
local M={}
function M.init()
    local f=assert(io.open("/app/redis/accounting.lua","rb")); M.script=f:read("*a"); f:close()
end
function M.connect()
    local cfg=config.get().redis
    local client=redis:new(); client:set_timeout(cfg.timeout_ms)
    local ok,err=client:connect(cfg.host,cfg.port)
    if not ok then return nil,err end
    local reused=client:get_reused_times()
    if reused==0 then
        ok,err=client:auth(cfg.password)
        if not ok then client:close(); return nil,err end
    end
    return client
end
function M.close(client) if client then client:set_keepalive(10000,100) end end
function M.eval(keys,args)
    local client,err=M.connect(); if not client then return nil,err end
    local argv={M.script,#keys}; for _,key in ipairs(keys) do argv[#argv+1]=key end
    argv[#argv+1]=assert(json.encode(args))
    local result; result,err=client:eval(unpack(argv))
    M.close(client)
    return result,err
end
function M.hash(values)
    local result={}; for i=1,#values,2 do result[values[i]]=values[i+1] end
    return result
end
return M
