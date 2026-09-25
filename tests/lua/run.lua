local json=require 'cjson.safe'
local observer=require 'core.response_observer'
local usage=require 'core.usage_event'
local fallback=require 'plugins.fallback'
local budget=require 'plugins.budget'
local config=require 'core.config'
local identity=require 'plugins.identity'
local unified=require 'plugins.unified_api'
local limits={json_max=4096,event_max=1024,capture_max=4096}
local error_headers={['content-type']='application/json; charset=utf-8'}
local quota_body='{"error":{"code":"insufficient_quota"}}'
assert(fallback.quota_error(429,error_headers,quota_body,{'insufficient_quota'}))
assert(fallback.quota_error(402,error_headers,quota_body,{'insufficient_quota'}))
for _,status in ipairs({200,401,500,502}) do assert(not fallback.quota_error(status,error_headers,quota_body,{'insufficient_quota'})) end
assert(not fallback.quota_error(429,error_headers,'{broken',{'insufficient_quota'}))
assert(not fallback.quota_error(429,error_headers,'{"error":{"message":"insufficient_quota"}}',{'insufficient_quota'}))
assert(not fallback.quota_error(429,error_headers,'{"error":{"code":"insufficient_quota"},"choices":[{"index":0}]}',{'insufficient_quota'}))
local function sse(value) return 'data: '..assert(json.encode(value))..'\r\n\r\n' end
local function make() return observer.new(true,limits) end
local u={prompt_tokens=12,completion_tokens=5,total_tokens=17,prompt_tokens_details={cached_tokens=4}}
local stream=sse({choices={{index=0,delta={role='assistant'}}},usage=json.null})..
    sse({choices={{index=0,delta={content='你好'}}}})..sse({usage=u})..sse({usage=u})..'data: [DONE]\r\n\r\n'
local o=make()
for i=1,#stream do o:feed(stream:sub(i,i),false,i) end
o:feed('',true,#stream+1)
assert(o.done and o.usage.total_tokens==17 and o.first_content_at>1)
assert(o:content().choices[1].message.content=='你好')
local multi=make(); multi:feed('data: {"usage":\ndata: {"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n',true,1)
assert(multi.usage.total_tokens==3)
local clipped=observer.new(true,{json_max=100,event_max=1024,capture_max=1})
clipped:feed(stream,true,1); assert(clipped.truncated and clipped.usage.total_tokens==17)
local large=observer.new(false,{json_max=3,event_max=10,capture_max=3}); large:feed('{"usage":{}}',true,1)
assert(large.invalid and large.usage==nil)
for _,bad in ipairs({{prompt_tokens=-1,completion_tokens=0,total_tokens=-1},
    {prompt_tokens=1,completion_tokens=1,total_tokens=3},{prompt_tokens=1.5,completion_tokens=0,total_tokens=1.5},
    {prompt_tokens=9007199254740992,completion_tokens=0,total_tokens=9007199254740992}}) do assert(not usage.validate(bad)) end
assert(usage.validate({prompt_tokens=0,completion_tokens=0,total_tokens=0}))
local eventctx={request_id='req_1',trace_id='trace_1',identity={user_id='user_1'},requested_model='logical'}
local eventattempt={attempt_id='attempt_1',candidate={model='real',provider='p',price_version='v1'},started_at=1,unsent=false}
local event=usage.new(eventctx,eventattempt,u,'completed',2); assert(usage.validate_event(event))
event=usage.new(eventctx,eventattempt,nil,'interrupted',2); assert(usage.validate_event(event) and event.total_tokens==json.null)
eventattempt.unsent=true
event=usage.new(eventctx,eventattempt,{prompt_tokens=0,completion_tokens=0,total_tokens=0},'failed',2)
assert(usage.validate_event(event) and event.usage_source=='system')
event.total_tokens=1; assert(not usage.validate_event(event))
local cfg=config.load()
local who=identity.authenticate('Bearer gw-test-user1-key-0000000000000001',cfg.api_keys,'chat:write')
assert(who and who.user_id=='user_001' and who.agent_id=='agent_001')
local _,status=identity.authenticate('Bearer invalid',cfg.api_keys,'chat:write'); assert(status==401)
cfg.api_keys[1].disabled=true
_,status=identity.authenticate('Bearer gw-test-user1-key-0000000000000001',cfg.api_keys,'chat:write'); assert(status==401)
cfg.api_keys[1].disabled=false
_,status=identity.authenticate('Bearer gw-test-user2-key-0000000000000002',cfg.api_keys,'audit:write'); assert(status==403)
local duplicate=json.decode(json.encode(cfg.api_keys[2])); duplicate.user_id='user_001'
assert(identity.authenticate('Bearer gw-test-user2-key-0000000000000002',{duplicate},'chat:write').user_id=='user_001')
local request={model='balanced',messages={{role='user',content='hi'}},stream=true}
assert(unified.validate(request,cfg))
cfg.models.balanced.candidates[1].capabilities={}
assert(#unified.validate(request,cfg).candidates==1)
cfg.models.balanced.candidates[2].capabilities={}
assert(not unified.validate(request,cfg))
assert(not unified.validate({model='balanced',messages={{role='forged',content='hi'}}},cfg))
assert(not unified.validate({model='balanced',messages={{role='user',content='hi'}},n=1.5},cfg))
local candidate={input_rate=1,output_rate=1,context_tokens=100}
assert(usage.cost(candidate,{prompt_tokens=1,completion_tokens=0,total_tokens=1})==1)
local day,month=budget.periods(1790784000) -- 2026-10-01 00:00 Asia/Shanghai
assert(day=='2026-10-01' and month=='2026-10')
local state={status=502,sent='0',received='0',header_time='-',headers_sent=false,count=1,max_attempts=2,has_candidate=true,now=1,deadline=2}
assert(fallback.allowed(state))
for key,value in pairs({sent='1',received='1',header_time='0.1',headers_sent=true,count=2,has_candidate=false,now=3,status=429}) do
    local copy={}; for k,v in pairs(state) do copy[k]=v end; copy[key]=value; assert(not fallback.allowed(copy))
end
print('PASS Lua SSE/UTF-8/multiline/limits/usage/rounding/period/identity/capabilities/fallback checks')
local original_log=ngx.log
local captured
ngx.log=function(_,value) captured=value end
require('plugins.observability').log({event='request.finished',trace_id=cfg.known_secrets[1]})
ngx.log=original_log
assert(captured and not captured:find(cfg.known_secrets[1],1,true) and captured:find('[REDACTED]',1,true))
print('PASS credential redaction in correlation metadata')
