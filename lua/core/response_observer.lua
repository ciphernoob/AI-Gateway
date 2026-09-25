local json=require "cjson.safe"
local usage=require "core.usage_event"
local M={}; M.__index=M
function M.new(stream, limits)
    return setmetatable({stream=stream, limits=limits, buffer="", lines={}, event_bytes=0, skip_event=false,
        json_parts={}, bytes=0, capture_bytes=0, choices={}, done=false, invalid=false, truncated=false},M)
end
function M:consume(value, now)
    if type(value)~="table" then self.invalid=true; return end
    if value.usage~=nil and value.usage~=json.null then
        local u=usage.validate(value.usage)
        if u then self.usage=u else self.usage_invalid=true; self.usage=nil end
    end
    if type(value.choices)~="table" then return end
    for _,choice in ipairs(value.choices) do
        if type(choice)~="table" or type(choice.index)~="number" or choice.index<0 or choice.index>127 then
            self.invalid=true; break
        end
        local delta=choice.delta or choice.message
        if type(delta)=="table" then
            if not self.first_content_at and ((type(delta.content)=="string" and #delta.content>0)
                or (type(delta.refusal)=="string" and #delta.refusal>0)) then self.first_content_at=now end
            local out=self.choices[choice.index] or {index=choice.index,content="",refusal="",tool_calls={}}
            self.choices[choice.index]=out
            for _,field in ipairs({"content","refusal"}) do
                if type(delta[field])=="string" and not self.truncated then out[field]=out[field]..delta[field] end
            end
            if type(delta.tool_calls)=="table" then
                for position,tool in ipairs(delta.tool_calls) do
                    local index=tool.index or position-1
                    if type(index)~="number" or index<0 or index>127 then self.invalid=true; break end
                    local f=type(tool["function"])=="table" and tool["function"] or {}
                    if type(f.arguments)=="string" and #f.arguments>0 and not self.first_content_at then self.first_content_at=now end
                    local t=out.tool_calls[index] or {index=index,type="function",["function"]={name="",arguments=""}}
                    out.tool_calls[index]=t
                    if type(tool.id)=="string" then t.id=tool.id end
                    if not self.truncated then
                        for _,key in ipairs({"name","arguments"}) do
                            if type(f[key])=="string" then t["function"][key]=t["function"][key]..f[key] end
                        end
                    end
                end
            end
        end
        if choice.finish_reason and choice.finish_reason~=json.null and self.choices[choice.index] then
            self.choices[choice.index].finish_reason=choice.finish_reason
        end
    end
end
function M:line(line, now)
    line=line:gsub("\r$", "")
    if line=="" then
        if not self.skip_event and #self.lines>0 then
            local data=table.concat(self.lines,"\n")
            if data=="[DONE]" then self.done=true
            else local value=json.decode(data); if value then self:consume(value,now) else self.invalid=true end end
        end
        self.lines={}; self.event_bytes=0; self.skip_event=false
    elseif line:sub(1,5)=="data:" then
        self.event_bytes=self.event_bytes+#line
        if self.event_bytes>self.limits.event_max then self.skip_event=true; self.lines={}; self.invalid=true end
        if not self.skip_event then self.lines[#self.lines+1]=line:sub(6):gsub("^ ","") end
    end
end
function M:feed(chunk, eof, now)
    chunk=chunk or ""; self.bytes=self.bytes+#chunk
    if self.bytes>self.limits.capture_max then self.truncated=true end
    if self.stream then
        self.buffer=self.buffer..chunk
        while true do
            local pos=self.buffer:find("\n",1,true)
            if not pos then break end
            local line=self.buffer:sub(1,pos-1); self.buffer=self.buffer:sub(pos+1)
            self:line(line,now)
        end
        if #self.buffer>self.limits.event_max then self.buffer=""; self.skip_event=true; self.invalid=true end
    elseif self.bytes<=self.limits.json_max then self.json_parts[#self.json_parts+1]=chunk
    else self.json_parts={}; self.invalid=true end
    if eof then
        if not self.stream and not self.invalid then
            local value=json.decode(table.concat(self.json_parts))
            if value then self:consume(value,now); self.response=value; self.done=true else self.invalid=true end
        end
        self.json_parts={}
        if self.usage_invalid then self.usage=nil end
    end
end
function M:content()
    if not self.stream then return self.truncated and nil or self.response end
    local list={}
    for _,choice in pairs(self.choices) do
        local tools={}; for _,tool in pairs(choice.tool_calls) do
            tool.parse_state=json.decode(tool["function"].arguments) and "valid" or "incomplete"
            tools[#tools+1]=tool
        end
        table.sort(tools,function(a,b) return a.index<b.index end)
        list[#list+1]={index=choice.index,message={role="assistant",content=choice.content,refusal=choice.refusal,tool_calls=tools},finish_reason=choice.finish_reason}
    end
    table.sort(list,function(a,b) return a.index<b.index end)
    return {choices=list}
end
return M
