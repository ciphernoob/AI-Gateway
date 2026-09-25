local json=require 'cjson.safe'
local config=require 'core.config'
local context=require 'core.context'
local errors=require 'core.errors'
local identity=require 'plugins.identity'
local unified=require 'plugins.unified_api'
local audit=require 'plugins.audit'
local budget=require 'plugins.budget'
local metrics=require 'plugins.observability'
local settlement=require 'core.settlement'
local observer=require 'core.response_observer'
local M={}
local zero={prompt_tokens=0,completion_tokens=0,total_tokens=0}
function M.init()
    local cfg=config.load(); require('core.redis_client').init()
    M.plugins=require('core.plugin_manager').build(cfg.plugins,{
        observability={headers=function(ctx)
            ngx.header['X-Request-ID']=ctx.request_id; ngx.header['X-Trace-ID']=ctx.trace_id
            if ctx.current then ngx.header['X-Attempt-ID']=ctx.current.attempt_id end
        end}})
end
function M.init_worker()
    assert(ngx.timer.every(0.2,settlement.tick))
    assert(ngx.timer.every(config.get().redis.recovery_interval,settlement.recover))
end
local function authenticate(scope)
    local ctx=context.get() or context.new(); context.save(ctx)
    local who,status=identity.authenticate(ngx.var.http_authorization,config.get().api_keys,scope)
    if not who then return nil,errors.exit(status,status==401 and 'invalid_api_key' or 'insufficient_scope') end
    ctx.identity=who; context.save(ctx); return ctx
end
local function read_json(maximum)
    if ngx.var.http_content_encoding then return nil,'unsupported_content_encoding' end
    ngx.req.read_body()
    local raw=ngx.req.get_body_data()
    if not raw and ngx.req.get_body_file() then return nil,'body_too_large' end
    if not raw or #raw==0 then return nil,'invalid_json' end
    if #raw>maximum then return nil,'body_too_large' end
    local body=json.decode(raw)
    if type(body)~='table' then return nil,'invalid_json' end
    return body
end
local function respond(value,status)
    ngx.status=status or 200; ngx.header.content_type='application/json'; ngx.say(assert(json.encode(value)))
end
local function resolve(host)
    if host:match('^%d+%.%d+%.%d+%.%d+$') then return host end
    local r=require('resty.dns.resolver'):new({nameservers={'127.0.0.11'},retrans=1,timeout=1000})
    if not r then return nil end
    local records=r:query(host,{qtype=r.TYPE_A})
    if records and not records.errcode then for _,record in ipairs(records) do if record.address then return record.address end end end
end
local function tools_from(content)
    local result={}; local seen={}
    for _,choice in ipairs(content and content.choices or {}) do
        for _,tool in ipairs(choice.message and choice.message.tool_calls or {}) do
            if type(tool.id)=='string' and #tool.id<=200 and not seen[tool.id] then
                seen[tool.id]=true; result[#result+1]={id=tool.id,name=tool['function'] and tool['function'].name}
            end
        end
    end
    return result
end
local function job(ctx,attempt,exact,status,code,final,obs)
    local content=obs and obs:content()
    local source=exact and (attempt and attempt.unsent and 'system' or 'provider') or 'unknown'
    local capture=(obs and (obs.invalid or obs.truncated) or status=='interrupted') and 'incomplete' or 'complete'
    return {identity=ctx.identity,requested_model=ctx.requested_model,attempt=attempt,usage=exact,
        event=attempt and require('core.usage_event').new(ctx,attempt,exact,status,ngx.now()),
        audit=ctx.audit_prepared and {request_id=ctx.request_id,attempt_id=attempt and attempt.audit_started and attempt.attempt_id or nil,
            status=status,http_status=code,final=final,usage=exact or json.null,usage_source=source,
            capture_state=capture,response=content,tool_calls=tools_from(content),
            payload_state=obs and obs.truncated and 'truncated' or nil,
            received_bytes=obs and obs.bytes or 0,captured_bytes=obs and math.min(obs.bytes,config.get().audit.response_max_bytes_per_attempt) or 0} or nil}
end
local function record_attempt(ctx,attempt,exact,status,code,obs)
    local event=require('core.usage_event').new(ctx,attempt,exact,status,ngx.now())
    event.event='attempt.finished'; event.http_status=code
    event.cost_micro_usd=exact and require('core.usage_event').cost(attempt.candidate,exact) or json.null
    metrics.log(event)
    local labels={model=ctx.requested_model,provider=attempt.candidate.provider,status=status}
    metrics.inc('gateway_attempts_total',labels)
    metrics.observe('gateway_attempt_seconds',{model=ctx.requested_model,provider=attempt.candidate.provider},ngx.now()-attempt.started_at)
    if obs and obs.first_content_at then metrics.observe('gateway_ttft_seconds',{model=ctx.requested_model},obs.first_content_at-attempt.started_at) end
end
function M.access(retry)
    local cfg=config.get(); local ctx
    if retry then
        ctx=context.get()
        if not ctx or not ctx.current then return errors.exit(502,'upstream_unavailable') end
        local status=tonumber(ngx.var.upstream_status and ngx.var.upstream_status:match('(%d+)$')) or 502
        local allowed=require('plugins.fallback').allowed({status=status,sent=ngx.var.upstream_bytes_sent,
            received=ngx.var.upstream_bytes_received,header_time=ngx.var.upstream_header_time,
            headers_sent=ngx.headers_sent,count=#ctx.attempts,max_attempts=cfg.request.max_attempts,
            has_candidate=ctx.candidates[#ctx.attempts+1]~=nil,now=ngx.now(),deadline=ctx.deadline})
        if ngx.var.upstream_bytes_sent=='0' and ngx.var.upstream_bytes_received=='0' and ngx.var.upstream_header_time=='-' then
            ctx.current.unsent=true; context.save(ctx)
        end
        if not cfg.plugins.fallback or not allowed then return errors.exit(status,status==504 and 'upstream_timeout' or 'upstream_unavailable') end
        local previous=ctx.current; previous.unsent=true
        record_attempt(ctx,previous,zero,'failed',status)
        -- Release proven-unsent reservations before trying the next backend.
        local ok=settlement.process(job(ctx,previous,zero,'failed',status,false))
        if not ok then settlement.enqueue(job(ctx,previous,zero,'failed',status,false)) end
        ctx.current=nil; ctx.fallback_count=ctx.fallback_count+1; context.save(ctx)
        metrics.inc('gateway_fallback_total',{model=ctx.requested_model})
    else
        ctx=authenticate('chat:write'); if not ctx then return end
        if ngx.req.get_method()~='POST' then return errors.exit(405,'method_not_allowed') end
        local body,err=read_json(cfg.request.max_bytes)
        if not body then return errors.exit(err=='body_too_large' and 413 or 400,err) end
        local route=ngx.var.request_uri:match('^/models/([%w_-]+)/v1/chat/completions[?]?')
        if route then
            if not cfg.models[route] then return errors.exit(404,'model_not_found') end
            if body.model~=nil and body.model~=route then return errors.exit(400,'model_path_conflict') end
            body.model=route
        end
        local value; value,err=unified.validate(body,cfg)
        if not value then return errors.exit(400,err) end
        for k,v in pairs(value) do ctx[k]=v end
        ctx.deadline=ctx.started_at+cfg.request.total_timeout_ms/1000
        local headers=ngx.req.get_headers()
        for _,key in ipairs({'x-trace-id','x-parent-request-id','x-conversation-id'}) do
            local v=headers[key]
            if v and (type(v)~='string' or #v>200 or not v:match('^[%w_:%.%-]+$')) then return errors.exit(400,'invalid_trace_header') end
        end
        ctx.trace_id=headers['x-trace-id'] or context.id('trace_'); ctx.trace_provided=headers['x-trace-id']~=nil
        ctx.parent_request_id=headers['x-parent-request-id']; ctx.conversation_id=headers['x-conversation-id']
        ngx.var.gw_body=assert(json.encode(body)); context.save(ctx)
        local prepared,status,code=audit.prepare(ctx,body)
        if not prepared then return errors.exit(status,code) end
        ctx.audit_prepared=audit.enabled(); context.save(ctx)
    end
    return M.start_attempt(ctx)
end
function M.start_attempt(ctx)
    local cfg=config.get()
    if ngx.now()>=ctx.deadline then return errors.exit(504,'request_timeout') end
    local candidate=ctx.candidates[#ctx.attempts+1]
    local attempt={attempt_id=context.id('attempt_'),candidate=candidate,started_at=ngx.now(),unsent=true}
    ctx.attempts[#ctx.attempts+1]=attempt.attempt_id; ctx.current=attempt; context.save(ctx)
    if ctx.transition then
        local from=ctx.transition
        metrics.log({event='fallback.selected',reason=from.reason,error_code=from.error_code,
            request_id=ctx.request_id,trace_id=ctx.trace_id,from_attempt_id=from.attempt_id,to_attempt_id=attempt.attempt_id,
            from_provider=from.provider,to_provider=candidate.provider,from_model=from.model,to_model=candidate.model,
            from_key_ref=cfg.providers[from.provider].key_env,to_key_ref=cfg.providers[candidate.provider].key_env})
        ctx.transition=nil
    end
    if cfg.plugins.usage_collector then
        if not budget.begin(ctx,attempt) then attempt.keys=nil; context.save(ctx); return errors.exit(503,'accounting_unavailable') end
    end
    local body=unified.adapt(assert(json.decode(ngx.var.gw_body)),candidate,ctx.output_tokens)
    local started,status,code=audit.attempt(ctx,attempt,body)
    if not started then return errors.exit(status,code) end
    attempt.audit_started=audit.enabled(); context.save(ctx)
    if attempt.keys then
        local ok,err=budget.reserve(ctx,attempt); context.save(ctx)
        if not ok then return errors.exit(err=='budget_exceeded' and 429 or 503,err or 'accounting_unavailable') end
    end
    local provider=cfg.providers[candidate.provider]
    ctx.ip=resolve(provider.host)
    if not ctx.ip then return errors.exit(502,'upstream_dns_error') end
    ctx.port=provider.port
    ngx.var.gw_scheme=provider.scheme; ngx.var.gw_path=provider.path
    ngx.var.gw_host=provider.authority; ngx.var.gw_sni=provider.host; ngx.var.gw_auth='Bearer '..provider.key
    ngx.req.set_body_data(assert(json.encode(body)))
    if attempt.keys and not budget.dispatch(attempt) then return errors.exit(503,'accounting_unavailable') end
    attempt.unsent=false; ctx.dispatched=true; context.save(ctx)
    ngx.ctx.observer=observer.new(ctx.stream,{json_max=cfg.usage.json_max_bytes,event_max=cfg.usage.sse_event_max_bytes,
        capture_max=cfg.audit.response_max_bytes_per_attempt})
    return true
end
function M.entry()
    local cfg=config.get()
    if cfg.plugins.fallback and cfg.fallback.on_key_quota_exhausted then return ngx.exec('@quota_proxy') end
    return M.access(false)
end
function M.relay_access()
    local cfg=config.get(); local provider=cfg.providers[ngx.var.http_x_relay_provider or '']
    local deadline=tonumber(ngx.var.http_x_relay_deadline)
    if not provider or not deadline then return ngx.exit(400) end
    local ip=resolve(provider.host); if not ip then return ngx.exit(502) end
    ngx.ctx.gateway={ip=ip,port=provider.port,deadline=deadline}
    ngx.var.gw_scheme=provider.scheme; ngx.var.gw_path=provider.path
    ngx.var.gw_host=provider.authority; ngx.var.gw_sni=provider.host; ngx.var.gw_auth='Bearer '..provider.key
end
function M.quota_proxy()
    if not M.access(false) then return end
    local cfg=config.get(); local ctx=context.get(); local fallback=require 'plugins.fallback'
    while true do
        local attempt=ctx.current
        local res=require('core.relay_client').open(attempt.candidate.provider,ngx.req.get_body_data(),ctx.deadline,cfg.request.read_timeout_ms)
        if not res then return errors.exit(502,'upstream_unavailable') end
        local next_candidate=ctx.candidates[#ctx.attempts+1]
        if (res.status==502 or res.status==504) and res.headers['x-relay-sent']=='0' and res.headers['x-relay-received']=='0'
            and res.headers['x-relay-header-time']=='-' then attempt.unsent=true end
        local room=#ctx.attempts<cfg.request.max_attempts and next_candidate~=nil and ngx.now()<ctx.deadline
        local connect_retry=room and fallback.allowed({status=res.status,sent=res.headers['x-relay-sent'],
            received=res.headers['x-relay-received'],header_time=res.headers['x-relay-header-time'],headers_sent=ngx.headers_sent,
            count=#ctx.attempts,max_attempts=cfg.request.max_attempts,has_candidate=true,now=ngx.now(),deadline=ctx.deadline})
        local parts={}; local bytes=0; local read_error
        if res.status==402 or res.status==429 or connect_retry then
            while bytes<=65536 do
                local data,err=res:next()
                if not data then read_error=err; break end
                parts[#parts+1]=data; bytes=bytes+#data
            end
        end
        local prefix=table.concat(parts)
        local error_code=not read_error and res.done and fallback.quota_error(res.status,res.headers,prefix,
            cfg.providers[attempt.candidate.provider].quota_exhaustion_codes)
        local different_key=room and cfg.providers[next_candidate.provider].key~=cfg.providers[attempt.candidate.provider].key
        if (connect_retry or (room and different_key and error_code)) and not ngx.headers_sent and ngx.now()<ctx.deadline then
            res:close()
            local previous_observer=observer.new(false,{json_max=cfg.usage.json_max_bytes,event_max=cfg.usage.sse_event_max_bytes,
                capture_max=cfg.audit.response_max_bytes_per_attempt})
            previous_observer:feed(prefix,true,ngx.now())
            attempt.unsent=connect_retry or false
            local exact=connect_retry and zero or previous_observer.usage
            record_attempt(ctx,attempt,exact,'failed',res.status,previous_observer)
            local pending=job(ctx,attempt,exact,'failed',res.status,false,previous_observer)
            if not settlement.process(pending) then settlement.enqueue(pending) end
            ctx.transition={attempt_id=attempt.attempt_id,provider=attempt.candidate.provider,model=attempt.candidate.model,
                reason=connect_retry and 'connect_failure' or 'quota_exhausted',error_code=error_code}
            ctx.current=nil; ctx.fallback_count=ctx.fallback_count+1; ngx.ctx.observer=nil; context.save(ctx)
            metrics.inc('gateway_fallback_total',{model=ctx.requested_model})
            if not M.start_attempt(ctx) then return end
        else
            if res.status>=400 then
                ngx.ctx.observer=observer.new(false,{json_max=cfg.usage.json_max_bytes,event_max=cfg.usage.sse_event_max_bytes,
                    capture_max=cfg.audit.response_max_bytes_per_attempt})
            end
            ngx.status=res.status
            for _,name in ipairs({'content-type','cache-control','retry-after'}) do ngx.header[name]=res.headers[name] end
            -- Preserve a known length so a truncated non-stream response cannot appear complete.
            if res.headers['content-length'] then ngx.header.content_length=res.headers['content-length'] end
            if #prefix>0 then ngx.print(prefix); ngx.flush(true) end
            while not read_error do
                local data,err=res:next()
                if not data then read_error=err; break end
                ngx.print(data)
                local ok=ngx.flush(true); if not ok then read_error='client_closed'; break end
            end
            res:close()
            if read_error then ctx.deadline_exceeded=true; return ngx.exit(ngx.ERROR) end
            return
        end
    end
end
function M.terminal()
    local ctx=context.get()
    local function last(value) return value and value:match('([^,%s:]+)$') end
    local status=tonumber(last(ngx.var.upstream_status)) or 502
    if ctx and ctx.current and last(ngx.var.upstream_bytes_sent)=='0' and last(ngx.var.upstream_bytes_received)=='0'
        and last(ngx.var.upstream_header_time)=='-' then ctx.current.unsent=true; context.save(ctx) end
    return errors.exit(status,status==504 and 'upstream_timeout' or 'upstream_unavailable')
end
function M.balance()
    local ctx=context.get(); if not ctx then return ngx.exit(502) end
    local remaining=ctx.deadline-ngx.now(); if remaining<=0 then return ngx.exit(504) end
    local b=require 'ngx.balancer'; local cfg=config.get().request
    assert(b.set_current_peer(ctx.ip,ctx.port))
    b.set_timeouts(math.min(remaining,cfg.connect_timeout_ms/1000),math.min(remaining,cfg.read_timeout_ms/1000),math.min(remaining,cfg.read_timeout_ms/1000))
end
function M.headers()
    local ctx=context.get(); if not ctx then return end
    -- Correlation headers are protocol features, independent of metrics enablement.
    ngx.header['X-Request-ID']=ctx.request_id; ngx.header['X-Trace-ID']=ctx.trace_id
    if ctx.current then ngx.header['X-Attempt-ID']=ctx.current.attempt_id end
    M.plugins:run('headers',ctx)
end
function M.body()
    local ctx=context.get(); local obs=ngx.ctx.observer
    if not ctx or not obs or ctx.gateway_error then return end
    if ngx.now()>ctx.deadline then ctx.deadline_exceeded=true; ngx.arg[1]=nil; ngx.arg[2]=true end
    obs:feed(ngx.arg[1],ngx.arg[2],ngx.now())
end
function M.log()
    local ctx=context.get(); if not ctx or not ctx.identity or not ctx.requested_model then return end
    local attempt=ctx.current; local obs=ngx.ctx.observer
    local code=ngx.status; local status=code<400 and 'completed' or 'failed'
    if ctx.deadline_exceeded or ngx.var.request_completion~='OK' or (ctx.stream and obs and not obs.done) then status='interrupted' end
    local exact=obs and obs.usage
    if attempt and attempt.unsent then exact=zero end
    if attempt then record_attempt(ctx,attempt,exact,status,code,obs) end
    settlement.enqueue(job(ctx,attempt,exact,status,code,true,obs))
    metrics.inc('gateway_requests_total',{model=ctx.requested_model,status=status})
    metrics.observe('gateway_request_seconds',{model=ctx.requested_model},ngx.now()-ctx.started_at)
    if not exact then metrics.inc('gateway_unknown_usage_total',{model=ctx.requested_model}) end
    metrics.log({event='request.finished',request_id=ctx.request_id,trace_id=ctx.trace_id,model=ctx.requested_model,
        status=status,http_status=code,fallback_count=ctx.fallback_count,elapsed_ms=math.floor((ngx.now()-ctx.started_at)*1000)})
end
function M.api()
    local uri=ngx.var.uri; local method=ngx.req.get_method()
    local scope=uri=='/v1/usage' and 'usage:read' or (uri=='/v1/audit/events' and 'audit:write' or 'audit:read')
    local ctx=authenticate(scope); if not ctx then return end
    if uri=='/v1/usage' then
        if method~='GET' then return errors.exit(405,'method_not_allowed') end
        if next(ngx.req.get_uri_args()) then return errors.exit(400,'invalid_query') end
        if not config.get().plugins.user_quota then return errors.exit(404,'not_found') end
        local value=require('plugins.user_quota').query(ctx.identity)
        if not value then return errors.exit(503,'accounting_unavailable') end
        return respond(value)
    end
    if not audit.enabled() then return errors.exit(404,'not_found') end
    local value,status,code
    if uri=='/v1/audit/events' then
        if method~='POST' then return errors.exit(405,'method_not_allowed') end
        local body,err=read_json(config.get().audit.tool_event_max_bytes)
        if not body then return errors.exit(err=='body_too_large' and 413 or 400,err) end
        value,status,code=audit.call('tool',ctx.identity,body)
    else
        if method~='GET' then return errors.exit(405,'method_not_allowed') end
        local args,err=ngx.req.get_uri_args(20)
        if err then return errors.exit(400,'invalid_query') end
        for _,v in pairs(args) do if type(v)~='string' then return errors.exit(400,'invalid_query') end end
        local id=uri:match('^/v1/audit/requests/([%w_%-]+)$')
        local trace=uri:match('^/v1/audit/traces/([%w_%-]+)$')
        local resource=uri=='/v1/audit/requests' and 'requests' or (id and 'request' or (trace and 'trace'))
        if not resource then return errors.exit(404,'not_found') end
        value,status,code=audit.call('query',ctx.identity,{resource=resource,id=id or trace,params=args})
    end
    if not value then return errors.exit(status,code) end
    return respond(value,status)
end
function M.ready()
    local client=require('core.redis_client').connect()
    if not client then return errors.exit(503,'accounting_unavailable') end
    local ok=client:ping(); require('core.redis_client').close(client)
    if not ok then return errors.exit(503,'accounting_unavailable') end
    if audit.enabled() then
        local h=require('core.internal_http').new(); h:set_timeout(1000)
        local res=h:request_uri(config.get().audit.base_url..'/healthz')
        if not res or res.status~=200 then return errors.exit(503,'audit_unavailable') end
        local counts=json.decode(res.body)
        if counts then
            metrics.set('gateway_audit_pending_requests',nil,tonumber(counts.pending_requests) or 0)
            metrics.set('gateway_audit_incomplete_requests',nil,tonumber(counts.incomplete_requests) or 0)
        end
    end
    ngx.say('ok')
end
return M
