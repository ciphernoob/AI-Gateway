-- Narrow HTTP/1.1 client for the internal Audit Store (Content-Length responses).
-- LLM traffic always uses Nginx proxy_pass. No response chunking is accepted here.
local M={}; M.__index=M
function M.new() return setmetatable({timeout=1000},M) end
function M:set_timeout(ms) self.timeout=ms end
function M:request_uri(uri,options)
    options=options or {}
    local scheme,authority,path=uri:match('^(https?)://([^/]+)(/.*)$')
    if not scheme then scheme,authority=uri:match('^(https?)://([^/]+)$'); path='/' end
    if not scheme then return nil,'invalid_url' end
    local host,port=authority:match('^([^:]+):(%d+)$')
    host=host or authority; port=tonumber(port) or (scheme=='https' and 443 or 80)
    local socket=ngx.socket.tcp(); socket:settimeout(self.timeout)
    local ok=socket:connect(host,port)
    if not ok then socket:close(); return nil,'connect_failed' end
    if scheme=='https' then
        ok=socket:sslhandshake(nil,host,true)
        if not ok then socket:close(); return nil,'tls_failed' end
    end
    local body=options.body or ''
    local lines={(options.method or 'GET')..' '..path..' HTTP/1.1','Host: '..authority,'Connection: close','Content-Length: '..#body}
    for k,v in pairs(options.headers or {}) do lines[#lines+1]=k..': '..v end
    ok=socket:send(table.concat(lines,'\r\n')..'\r\n\r\n'..body)
    if not ok then socket:close(); return nil,'send_failed' end
    local line=socket:receive('*l'); local status=line and tonumber(line:match('^HTTP/1%.[01] (%d%d%d) '))
    if not status then socket:close(); return nil,'invalid_status' end
    local headers={}; local ended=false
    for _=1,100 do
        line=socket:receive('*l')
        if not line or #line>8192 then break end
        if line=='' then ended=true; break end
        local k,v=line:match('^([^:]+):%s*(.-)%s*$')
        if not k then break end
        k=k:lower(); if headers[k] then break end
        headers[k]=v
    end
    local length=tonumber(headers['content-length'])
    if not ended or not length or length<0 or length>16*1024*1024 or length~=math.floor(length) or headers['transfer-encoding'] then
        socket:close(); return nil,'invalid_headers'
    end
    local data=length==0 and '' or socket:receive(length); socket:close()
    if not data then return nil,'incomplete_body' end
    return {status=status,headers=headers,body=data}
end
return M
