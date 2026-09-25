import json
import os
import time
import unittest
import runpy
from unittest.mock import patch
from pathlib import Path
from scripts.reconcile import reconcile
from urllib.request import Request, urlopen
from tests.helpers import http, eventually

BASE = 'http://gateway:8080'


@unittest.skipUnless(os.environ.get('RUN_INTEGRATION'), 'requires Compose stack')
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.key = os.environ['GATEWAY_KEY_USER1']
        self.other = os.environ['GATEWAY_KEY_USER2']
        # Prior tests can return before their asynchronous settlement is visible.
        # Establish a stable baseline before asserting a precise token delta.
        eventually(lambda: http(BASE + '/v1/usage', key=self.key)[2],
                   lambda value: value['daily']['pending_attempts'] == 0)
        for name in ['mock-a', 'mock-b']:
            http(f'http://{name}:8000/control', {'mode': 'normal', 'reset': True, 'delay': .01})

    def chat(self, **updates):
        body = {'model': 'balanced', 'messages': [{'role': 'user', 'content': 'hello 世界'}]}
        body.update(updates)
        return http(BASE + '/v1/chat/completions', body, self.key)

    def detail(self, request_id):
        return eventually(lambda: http(BASE + '/v1/audit/requests/' + request_id + '?include_content=true', key=self.key),
                          lambda result: result[0] == 200 and result[2]['request']['capture_state'] != 'pending')[2]

    def test_health_auth_validation_and_private_metrics(self):
        self.assertEqual(200, http(BASE + '/readyz')[0])
        self.assertEqual(200, http(BASE + '/livez')[0])
        self.assertEqual(401, http(BASE + '/v1/usage')[0])
        self.assertEqual(400, http(BASE + '/v1/usage?user_id=user_002', key=self.key)[0])
        self.assertEqual(404, http(BASE + '/metrics')[0])
        self.assertEqual(400, self.chat(model='nonexistent')[0])
        self.assertEqual(400, self.chat(n=0)[0])
        self.assertEqual(400, self.chat(max_tokens=100000)[0])
        self.assertEqual(413, self.chat(messages=[{'role': 'user', 'content': 'x' * 1100000}])[0])
        self.assertEqual(403, http(BASE + '/v1/audit/events', {}, self.other)[0])
        self.assertEqual(401, http('http://audit-store:8001/internal/prepare', {})[0])

    def test_forwarding_usage_identity_audit_and_redaction(self):
        before = http(BASE + '/v1/usage', key=self.key)[2]['daily']['total_tokens']
        status, headers, body = self.chat(user_id='user_002', agent_id='forged', skip_audit=True,
            messages=[{'role': 'user', 'content': 'secret ' + self.key}])
        self.assertEqual(200, status, body)
        self.assertEqual(17, body['usage']['total_tokens'])
        rid = headers['X-Request-ID']
        detail = self.detail(rid)
        self.assertEqual('user_001', detail['request']['user_id'])
        self.assertNotIn(self.key, json.dumps(detail))
        self.assertTrue(any(e['type'] == 'attempt.finished' and 'payload' in e for e in detail['events']))
        eventually(lambda: http(BASE + '/v1/usage', key=self.key)[2], lambda v: v['daily']['total_tokens'] == before + 17)
        self.assertEqual(404, http(BASE + '/v1/audit/requests/' + rid, key=self.other)[0])
        recorded = http('http://mock-a:8000/control')[2]['requests']
        self.assertEqual(1, len(recorded))
        self.assertEqual('mock-model-a', recorded[0]['body']['model'])
        self.assertTrue(recorded[0]['credential_ok'])
        self.assertFalse(recorded[0]['gateway_header_present'])
        self.assertNotIn('user_id', recorded[0]['body'])
        self.assertEqual(200, self.chat(model='local')[0])
        self.assertEqual(1, len(http('http://mock-b:8000/control')[2]['requests']))

    def test_tool_audit_agent_reports_and_multiturn_trace(self):
        status, headers, body = self.chat(tools=[{'type': 'function', 'function': {'name': 'weather', 'parameters': {'type': 'object'}}}])
        self.assertEqual(200, status)
        rid, trace, attempt = (headers[k] for k in ['X-Request-ID', 'X-Trace-ID', 'X-Attempt-ID'])
        detail = self.detail(rid)
        self.assertEqual(2, len(detail['tool_calls']))
        self.assertTrue(all(t['execution_state'] == 'not_reported' for t in detail['tool_calls']))
        event = {'event_id': rid + ':done', 'tool_execution_id': rid + ':exec', 'type': 'tool.completed',
                 'request_id': rid, 'trace_id': trace, 'attempt_id': attempt, 'tool_call_id': 'call_001',
                 'occurred_at': '2026-09-24T00:00:00Z', 'result': {'temperature': 20, 'api_key': 'private'}}
        self.assertEqual(201, http(BASE + '/v1/audit/events', event, self.key)[0])
        self.assertEqual(200, http(BASE + '/v1/audit/events', event, self.key)[0])
        self.assertEqual(409, http(BASE + '/v1/audit/events', dict(event, result='changed'), self.key)[0])
        request = Request(BASE + '/v1/chat/completions', data=json.dumps({'model': 'balanced', 'messages': [
            {'role': 'user', 'content': 'weather'}, body['choices'][0]['message'],
            {'role': 'tool', 'tool_call_id': 'call_001', 'content': '20 degrees'}]}).encode(), headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json', 'X-Trace-ID': trace, 'X-Parent-Request-ID': rid})
        with urlopen(request) as response:
            self.assertEqual(trace, response.headers['X-Trace-ID'])
            self.assertIn('Hello', json.load(response)['choices'][0]['message']['content'])
        traces = http(BASE + '/v1/audit/traces/' + trace, key=self.key)[2]
        self.assertEqual(2, len(traces['data']))

    def test_streaming_is_incremental_and_tools_reassembled(self):
        http('http://mock-a:8000/control', {'delay': .04})
        request = Request(BASE + '/v1/chat/completions', data=json.dumps({'model': 'balanced', 'stream': True,
            'messages': [{'role': 'user', 'content': 'tools'}],
            'tools': [{'type': 'function', 'function': {'name': 'weather'}}]}).encode(),
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        start = time.monotonic()
        with urlopen(request, timeout=12) as response:
            rid = response.headers['X-Request-ID']; first = response.readline(); first_at = time.monotonic()
            rest = response.read(); end = time.monotonic()
        self.assertIn(b'data:', first)
        self.assertGreater(end - first_at, .1)
        self.assertIn(b'[DONE]', rest)
        detail = self.detail(rid)
        finished = next(e for e in detail['events'] if e['type'] == 'attempt.finished')
        self.assertEqual(17, finished['meta']['usage']['total_tokens'])
        tools = finished['payload']['choices'][0]['message']['tool_calls']
        self.assertEqual(2, len(tools))
        self.assertEqual('北京', json.loads(tools[0]['function']['arguments'])['city'])

    def test_provider_errors_never_retry_and_are_metered(self):
        for mode in ['429', '500', '502']:
            with self.subTest(mode=mode):
                http('http://mock-a:8000/control', {'mode': mode, 'reset': True})
                status, headers, body = self.chat()
                self.assertEqual(int(mode), status)
                self.assertEqual(0, len(http('http://mock-b:8000/control')[2]['requests']))
                detail = self.detail(headers['X-Request-ID'])
                finished = next(e for e in detail['events'] if e['type'] == 'attempt.finished')
                self.assertEqual(4, finished['meta']['usage']['total_tokens'])

    def test_missing_usage_and_interruption_remain_unknown(self):
        for mode, stream in [('no_usage', False), ('bad_usage', False), ('interrupt', True)]:
            with self.subTest(mode=mode):
                http('http://mock-a:8000/control', {'mode': mode})
                status, headers, body = self.chat(stream=stream)
                self.assertEqual(200, status)
                detail = self.detail(headers['X-Request-ID'])
                finished = next(e for e in detail['events'] if e['type'] == 'attempt.finished')
                self.assertIsNone(finished['meta']['usage'])
                self.assertEqual('unknown', finished['meta']['usage_source'])
                if stream: self.assertEqual('interrupted', detail['request']['status'])
                self.assertEqual(0, len(http('http://mock-b:8000/control')[2]['requests']))

    def test_post_send_timeout_does_not_fallback(self):
        http('http://mock-a:8000/control', {'mode': 'timeout'})
        status, headers, _ = self.chat()
        self.assertEqual(504, status)
        self.assertEqual(0, len(http('http://mock-b:8000/control')[2]['requests']))
        detail = self.detail(headers['X-Request-ID'])
        finished = next(e for e in detail['events'] if e['type'] == 'attempt.finished')
        self.assertIsNone(finished['meta']['usage'])

    def test_metrics_no_identity_labels(self):
        self.chat()
        value = http('http://gateway:9090/metrics')[2]
        self.assertIn('gateway_requests_total', value)
        for forbidden in ['user_id=', 'agent_id=', 'request_id=', 'attempt_id=', 'trace_id=', self.key]:
            self.assertNotIn(forbidden, value)

    def test_operator_reconciliation_is_audited_and_idempotent(self):
        http('http://mock-a:8000/control', {'mode': 'no_usage'})
        status, headers, _ = self.chat()
        self.assertEqual(200, status)
        self.detail(headers['X-Request-ID'])
        cfg = json.loads(Path('/runtime/config.json').read_text())
        exact = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        first = reconcile(cfg, headers['X-Attempt-ID'], exact, 'mock-provider-invoice-001')
        self.assertEqual('applied', first['quota'])
        second = reconcile(cfg, headers['X-Attempt-ID'], exact, 'mock-provider-invoice-001')
        self.assertEqual('duplicate', second['quota'])
        self.assertEqual('duplicate', second['budget'])
        detail = self.detail(headers['X-Request-ID'])
        receipts = [e for e in detail['events'] if e['type'] == 'usage.reconciliation']
        self.assertEqual(2, len(receipts))
        self.assertEqual('operator_reconciled', receipts[0]['source'])

    def test_documented_agent_example(self):
        with patch.dict(os.environ, {'GATEWAY_URL': BASE, 'GATEWAY_API_KEY': self.key}):
            runpy.run_path('examples/agent.py', run_name='__main__')
        recorded = http('http://mock-a:8000/control')[2]['requests']
        self.assertEqual(2, len(recorded))
        self.assertTrue(any(m['role'] == 'tool' for m in recorded[1]['body']['messages']))
