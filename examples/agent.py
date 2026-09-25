"""A minimal Agent: execute locally once, retry event delivery with the same ID."""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.environ.get('GATEWAY_URL', 'http://localhost:8080')


def post(path, body, **extra_headers):
    request = Request(BASE + path, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['GATEWAY_API_KEY'], **extra_headers})
    with urlopen(request, timeout=35) as response:
        return dict(response.headers), json.load(response)


def report(event):
    # Retrying delivery must never rerun the tool. Keep IDs and contents stable.
    for retry in range(5):
        try:
            return post('/v1/audit/events', event)
        except HTTPError as error:
            if error.code != 409 and error.code < 500: raise
            if retry == 4: raise
        except OSError:
            if retry == 4: raise
        time.sleep(min(2**retry, 4))


def main():
    messages = [{'role': 'user', 'content': 'What is the weather and current time?'}]
    headers, response = post('/v1/chat/completions', {'model': 'balanced', 'messages': messages,
        'tools': [{'type': 'function', 'function': {'name': 'weather', 'parameters': {'type': 'object'}}},
                  {'type': 'function', 'function': {'name': 'clock', 'parameters': {'type': 'object'}}}]})
    message = response['choices'][0]['message']; messages.append(message)
    for call in message.get('tool_calls', []):
        execution = uuid.uuid4().hex
        common = {'request_id': headers['X-Request-ID'], 'attempt_id': headers['X-Attempt-ID'],
                  'trace_id': headers['X-Trace-ID'], 'tool_call_id': call['id'], 'tool_execution_id': execution}
        def event(kind, **data):
            return dict(common, event_id=execution + ':' + kind, type='tool.' + kind,
                        occurred_at=datetime.now(timezone.utc).isoformat(), **data)
        report(event('started', arguments=call['function']['arguments']))
        try:
            # Replace these deterministic demo tools with the application's tools.
            name = call['function']['name']
            if name == 'weather': result = {'temperature': 20, 'unit': 'C', 'demo': True}
            elif name == 'clock': result = {'utc': datetime.now(timezone.utc).isoformat()}
            else: raise ValueError('unsupported tool')
        except Exception:
            result = {'error': 'tool_failed'}
            report(event('failed', error=result))
        else:
            report(event('completed', result=result))
        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
    _, answer = post('/v1/chat/completions', {'model': 'balanced', 'messages': messages},
                     **{'X-Trace-ID': headers['X-Trace-ID'], 'X-Parent-Request-ID': headers['X-Request-ID']})
    print(answer['choices'][0]['message']['content'])
    print('Trace:', headers['X-Trace-ID'])


if __name__ == '__main__': main()
