-- Streaming HTTP reader restricted to the private loopback Nginx relay.
local M={}; M.__index=M
function M:timeout()
    local remaining=self.deadline-ngx.now()
    if remaining<=0 then return nil,'timeout' end
    self.sock:settimeout(math.min(self.read_ms,remaining*1000)); return true
end
function M:read(n)
    local ok,err=self:timeout(); if not ok then return nil,err end
    return self.sock:receive(n)
end
function M:close() self.sock:close() end
function M.open(provider,body,deadline,read_ms)
    local self=setmetatable({sock=ngx.socket.tcp(),deadline=deadline,read_ms=read_ms},M)
    self:timeout()
    local ok=self.sock:connect('127.0.0.1',8081)
    if not ok then self:close(); return nil,'relay_connect_failed' end
    ok=self.sock:send('POST /relay HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\n'..
        'X-Relay-Provider: '..provider..'\r\nX-Relay-Deadline: '..string.format('%.3f',deadline)..'\r\nContent-Length: '..#body..'\r\n\r\n'..body)
    if not ok then self:close(); return nil,'relay_send_failed' end
    local line=self:read('*l'); self.status=line and tonumber(line:match('^HTTP/1%.[01] (%d%d%d) '))
    if not self.status then self:close(); return nil,'relay_headers_failed' end
    self.headers={}; local bytes=0; local ended=false
    for _=1,100 do
        line=self:read('*l'); if not line then break end
        bytes=bytes+#line; if bytes>65536 then break end
        if line=='' then ended=true; break end
        local key,value=line:match('^([^:]+):%s*(.-)%s*$')
        if not key then break end
        key=key:lower(); self.headers[key]=value
    end
    if not ended then self:close(); return nil,'relay_headers_failed' end
    self.chunked=self.headers['transfer-encoding']=='chunked'
    self.remaining=tonumber(self.headers['content-length'])
    return self
end
function M:next()
    if self.done then return nil end
    if self.chunked then
        if not self.chunk_remaining then
            local line,err=self:read('*l'); if not line then return nil,err end
            local size=tonumber(line:match('^([%x]+)'),16)
            if not size or size>2147483647 then return nil,'invalid_chunk' end
            if size==0 then
                for _=1,100 do
                    line,err=self:read('*l'); if not line then return nil,err end
                    if line=='' then self.done=true; return nil end
                end
                return nil,'invalid_trailers'
            end
            self.chunk_remaining=size
        end
        local data,err=self:read(math.min(self.chunk_remaining,8192))
        if not data then return nil,err end
        self.chunk_remaining=self.chunk_remaining-#data
        if self.chunk_remaining==0 then
            local ending; ending,err=self:read(2)
            if ending~='\r\n' then return nil,err or 'invalid_chunk' end
            self.chunk_remaining=nil
        end
        return data
    elseif self.remaining then
        if self.remaining==0 then self.done=true; return nil end
        local data,err=self:read(math.min(self.remaining,8192))
        if not data then return nil,err end
        self.remaining=self.remaining-#data; return data
    else
        local ok,err=self:timeout(); if not ok then return nil,err end
        local data; data,err=self.sock:receiveany(8192)
        if err=='closed' then self.done=true; return nil end
        return data,err
    end
end
return M
