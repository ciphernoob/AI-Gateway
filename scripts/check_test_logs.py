"""Check isolated test logs for known test credentials and body fields."""
import json
import sys
from pathlib import Path
from tests.helpers import test_environment

raw = Path(sys.argv[1]).read_text(encoding='utf-8')
assert not any(secret in raw for secret in test_environment().values()), 'credential found in logs'
events = []
switches = []
decoder = json.JSONDecoder()
for line in raw.splitlines():
    if '{' not in line:
        continue
    try:
        event, _ = decoder.raw_decode(line[line.index('{'):])
    except ValueError:
        continue
    if isinstance(event, dict) and event.get('event') in ('attempt.finished', 'request.finished'):
        assert not {'messages', 'content', 'arguments', 'tool_calls', 'response', 'request_body'} & event.keys()
        if event['event'] == 'attempt.finished':
            assert {'request_id', 'trace_id', 'attempt_id', 'usage_source', 'usage_status', 'model', 'actual_model'} <= event.keys()
        events.append(event)
    if isinstance(event, dict) and event.get('event') == 'fallback.selected':
        assert {'reason','request_id','trace_id','from_attempt_id','to_attempt_id','from_key_ref','to_key_ref','from_model','to_model'} <= event.keys()
        assert event['from_attempt_id'] != event['to_attempt_id']
        assert event['reason'] in ('quota_exhausted','connect_failure')
        assert not {'key','api_key','message','body','authorization'} & event.keys()
        switches.append(event)
assert events, 'no structured business logs found'
print('PASS structured business log fields and known-credential/content exclusion:', len(events), 'events')
if switches: print('PASS safe correlated fallback logs:',len(switches),'switches')
