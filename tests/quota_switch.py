"""Real gateway cases; invoked with an explicit test configuration variant."""
import json
import os
import sys
import time
from urllib.request import Request, urlopen
from tests.helpers import http, eventually

BASE='http://gateway:8080'
KEY=os.environ['GATEWAY_KEY_USER1']


def invoke(mode, stream=False, backup='normal'):
    for name, m in [('mock-a',mode),('mock-b',backup)]:
        http(f'http://{name}:8000/control', {'mode':m,'reset':True,'delay':.02})
    result=http(BASE+'/v1/chat/completions', {'model':'balanced','stream':stream,'messages':[{'role':'user','content':'quota test'}]},KEY)
    counts=[len(http(f'http://{name}:8000/control')[2]['requests']) for name in ['mock-a','mock-b']]
    return result,counts


def detail(headers):
    return eventually(lambda:http(BASE+'/v1/audit/requests/'+headers['X-Request-ID']+'?include_content=true',key=KEY),
        lambda r:r[0]==200 and r[2]['request']['capture_state']!='pending')[2]


def main():
    variant=sys.argv[1]
    if variant in ('key_quota_off','key_quota_same','key_quota_single'):
        result,counts=invoke('quota')
        assert result[0]==429 and counts==[1,0],(result,counts)
        assert result[2]['error']['code']=='insufficient_quota'
    elif variant=='key_quota_budget':
        result,counts=invoke('quota_usage')
        assert result[0]==429 and result[2]['error']['code']=='budget_exceeded' and counts==[1,0],(result,counts)
    elif variant=='key_quota_custom':
        result,counts=invoke('custom_quota')
        assert result[0]==200 and counts==[1,1]
    else:
        for mode in ['quota','quota_402','quota_usage']:
            result,counts=invoke(mode)
            status,headers,body=result
            assert status==200 and counts==[1,1],(mode,result,counts)
            assert body['model']=='mock-model-b'
            audit=detail(headers)
            attempts=[e for e in audit['events'] if e['type']=='attempt.finished']
            assert len(attempts)==2 and attempts[0]['attempt_id']!=attempts[1]['attempt_id']
            assert attempts[0]['meta']['usage_source']==('provider' if mode=='quota_usage' else 'unknown')
            assert attempts[0]['meta']['usage']==({'prompt_tokens':3,'completion_tokens':1,'total_tokens':4} if mode=='quota_usage' else None)
            assert attempts[1]['meta']['usage']['total_tokens']==17
            assert 'mock quota refusal' in json.dumps(attempts[0]['payload'])
        result,counts=invoke('quota',stream=True)
        assert result[0]==200 and '[DONE]' in result[2] and 'mock quota refusal' not in result[2] and counts==[1,1]
        for mode,expected in [('rate_limit',429),('auth_error',401),('500',500),('502',502),('quota_large',429),('custom_quota',429)]:
            result,counts=invoke(mode)
            assert result[0]==expected and counts==[1,0],(mode,result[0],counts)
        result,counts=invoke('quota',backup='quota')
        assert result[0]==429 and counts==[1,1]
        result,counts=invoke('interrupt',stream=True)
        assert result[0]==200 and '[DONE]' not in result[2] and counts==[1,0]
        # Normal successful streaming must remain incremental with the option enabled.
        http('http://mock-a:8000/control',{'mode':'normal','reset':True,'delay':.1})
        request=Request(BASE+'/v1/chat/completions',data=json.dumps({'model':'balanced','stream':True,
            'messages':[{'role':'user','content':'stream test'}]}).encode(),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'})
        with urlopen(request,timeout=12) as response:
            first=response.readline(); first_at=time.monotonic(); rest=response.read()
        assert first.startswith(b'data:') and b'[DONE]' in rest and time.monotonic()-first_at>.1
    print('PASS',variant)


if __name__=='__main__':main()
