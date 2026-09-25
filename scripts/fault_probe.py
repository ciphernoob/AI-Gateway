"""Assertions for a deliberately modified, isolated test stack."""
import os
import sys
from tests.helpers import http, eventually


def main():
    case = sys.argv[1]
    key = os.environ['GATEWAY_KEY_USER1']
    base = 'http://gateway:8080'
    http('http://mock-a:8000/control', {'mode': 'normal'})
    http('http://mock-b:8000/control', {'mode': 'normal'})
    status, headers, value = http(base + '/v1/chat/completions', {'model': 'balanced', 'messages': [{'role': 'user', 'content': 'fault probe'}]}, key)
    expected = {'fallback': 200, 'both_down': 502, 'single_attempt': 502, 'budget_denied': 429,
                'quota_exceeded': 200, 'metrics_off': 200, 'audit_down': 503, 'redis_down': 503}[case]
    assert status == expected, (case, status, value)
    if status >= 400:
        assert isinstance(value, dict) and 'error' in value, (case, 'gateway error must be JSON')
    primary = http('http://mock-a:8000/control')[2]['requests']
    backup = http('http://mock-b:8000/control')[2]['requests']
    if case in ('both_down', 'single_attempt', 'budget_denied', 'audit_down', 'redis_down'):
        assert not primary and not backup
    if case == 'fallback':
        assert not primary and len(backup) == 1
        assert backup[0]['body']['model'] == 'mock-model-b' and backup[0]['credential_ok']
        assert backup[0]['host'] == 'mock-b:8000'
    if case not in ('audit_down', 'redis_down'):
        detail = eventually(lambda: http(base + '/v1/audit/requests/' + headers['X-Request-ID'] + '?include_content=true', key=key),
                            lambda v: v[0] == 200 and v[2]['request']['capture_state'] != 'pending')[2]
        attempts = [e for e in detail['events'] if e['type'] == 'attempt.finished']
        if case == 'fallback':
            assert len(attempts) == 2
            assert attempts[0]['meta']['usage']['total_tokens'] == 0
            assert attempts[0]['meta']['usage_source'] == 'system'
            assert attempts[1]['meta']['usage']['total_tokens'] == 17
            assert attempts[0]['attempt_id'] != attempts[1]['attempt_id']
        if case == 'both_down':
            assert len(attempts) == 2
            assert all(a['meta']['usage']['total_tokens'] == 0 for a in attempts)
        if case == 'quota_exceeded':
            usage = http(base + '/v1/usage', key=key)[2]
            assert usage['enforcement'] is False and usage['daily']['remaining_tokens'] == 0
    else:
        assert http(base + '/readyz')[0] == 503
        assert http(base + '/livez')[0] == 200
    print('PASS', case)


if __name__ == '__main__': main()
