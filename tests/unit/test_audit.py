import json
import tempfile
import unittest
from pathlib import Path
from scripts.validate_config import load
from services.audit_store.store import Store, AuditError
from tests.helpers import ROOT, test_environment


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / 'audit.db')
        self.cfg = load(ROOT / 'config/gateway.example.yaml', test_environment())
        self.now = 1800000000
        self.store = Store(self.path, self.cfg, lambda: self.now)
        self.identity = {'user_id': 'user_001', 'agent_id': 'agent_001'}
        self.prepare()

    def tearDown(self):
        self.store.close(); self.directory.cleanup()

    def prepare(self, request='req_1', trace='trace_1', provided=False):
        return self.store.prepare(self.identity, {'request_id': request, 'trace_id': trace, 'trace_provided': provided,
            'requested_model': 'balanced', 'deadline': self.now + 30,
            'request': {'messages': [{'content': 'hello ' + self.cfg['known_secrets'][0]}], 'password': 'hidden'}})

    def finish(self):
        self.store.attempt(self.identity, {'request_id': 'req_1', 'attempt_id': 'attempt_1', 'request': {'model': 'backend'}})
        return self.store.finish(self.identity, {'request_id': 'req_1', 'attempt_id': 'attempt_1', 'status': 'completed',
            'capture_state': 'complete', 'http_status': 200, 'final': True, 'tool_calls': [{'id': 'call_1', 'name': 'weather'}],
            'response': {'arguments': '{"password":"hidden"}'}})

    def event(self, **updates):
        value = {'event_id': 'event_1', 'tool_execution_id': 'execution_1', 'type': 'tool.completed',
            'trace_id': 'trace_1', 'request_id': 'req_1', 'attempt_id': 'attempt_1', 'tool_call_id': 'call_1',
            'occurred_at': '2026-09-24T00:00:00Z', 'result': {'temperature': 20}}
        value.update(updates); return value

    def test_redaction_persistence_and_expiry(self):
        self.finish()
        result = self.store.detail('user_001', 'req_1', {'include_content': 'true'})
        encoded = json.dumps(result)
        self.assertNotIn(self.cfg['known_secrets'][0], encoded)
        self.assertNotIn('hidden', encoded)
        self.assertIn('[REDACTED]', encoded)
        self.store.close(); self.store = Store(self.path, self.cfg, lambda: self.now)
        self.assertEqual('complete', self.store.detail('user_001', 'req_1', {})['request']['capture_state'])
        self.now += 8 * 86400; self.store.maintain()
        result = self.store.detail('user_001', 'req_1', {'include_content': 'true'})
        self.assertIn('expired', [e['payload_state'] for e in result['events']])
        self.assertFalse(any('payload' in e for e in result['events']))
        self.finish()  # duplicate must not resurrect expired content
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM payloads WHERE data IS NOT NULL').fetchone()[0])
        self.now += 23 * 86400; self.store.maintain()
        with self.assertRaises(AuditError): self.store.detail('user_001', 'req_1', {})

    def test_tools_idempotency_conflict_out_of_order_and_ownership(self):
        with self.assertRaises(AuditError) as error: self.store.tool_event(self.identity, self.event())
        self.assertEqual('audit_pending', error.exception.code)
        self.finish()
        self.assertEqual(201, self.store.tool_event(self.identity, self.event())[1])
        self.assertEqual(200, self.store.tool_event(self.identity, self.event())[1])
        with self.assertRaises(AuditError): self.store.tool_event(self.identity, self.event(result='different'))
        self.store.tool_event(self.identity, self.event(event_id='start', type='tool.started'))
        self.store.tool_event(self.identity, self.event(event_id='retry', tool_execution_id='execution_2'))
        self.store.tool_event(self.identity, self.event(event_id='failed', tool_execution_id='execution_3', type='tool.failed', error={'code': 'timeout'}))
        detail = self.store.detail('user_001', 'req_1', {})
        self.assertEqual(3, len(detail['tool_calls'][0]['executions']))
        for bad in [dict(self.identity, user_id='user_002'), dict(self.identity, agent_id='agent_002')]:
            with self.assertRaises(AuditError) as error: self.store.tool_event(bad, self.event())
            self.assertEqual(404, error.exception.status)
        for field in ['user_id', 'agent_id', 'source']:
            with self.assertRaises(AuditError): self.store.tool_event(self.identity, self.event(**{field: 'forged'}))
        self.now += 8 * 86400
        with self.assertRaises(AuditError) as error: self.store.tool_event(self.identity, self.event(event_id='late'))
        self.assertEqual(410, error.exception.status)

    def test_cursor_trace_isolation_and_pending_gap(self):
        self.prepare('req_2', provided=True)
        page = self.store.list_requests('user_001', {'limit': '1'})
        self.assertIsNotNone(page['next_cursor'])
        page2 = self.store.list_requests('user_001', {'limit': '1', 'cursor': page['next_cursor']})
        self.assertNotEqual(page['data'][0]['id'], page2['data'][0]['id'])
        with self.assertRaises(AuditError): self.store.list_requests('user_002', {'limit': '1', 'cursor': page['next_cursor']})
        with self.assertRaises(AuditError): self.store.detail('user_002', 'req_1', {})
        page = self.store.detail('user_001', 'req_1', {'limit': '1'})
        self.assertEqual(1, len(page['events']))
        self.now += 60; self.store.maintain()
        self.assertEqual('incomplete', self.store.detail('user_001', 'req_1', {})['request']['capture_state'])
        self.finish()
        self.assertEqual('complete', self.store.detail('user_001', 'req_1', {})['request']['capture_state'])

    def test_metadata_policy_and_integrity(self):
        self.cfg['audit']['mode'] = 'metadata_only'
        self.prepare('req_meta', 'trace_meta')
        value = self.store.detail('user_001', 'req_meta', {'include_content': 'true'})
        self.assertNotIn('payload', value['events'][0])
        self.store.db.execute("UPDATE payloads SET data='{}'")
        with self.assertRaises(AuditError) as error: self.store.detail('user_001', 'req_1', {'include_content': 'true'})
        self.assertEqual('payload_integrity_error', error.exception.code)

    def test_trace_owner_and_parent_validation(self):
        for trace in ['trace_1', 'trace_missing']:
            with self.assertRaises(AuditError) as error:
                self.store.prepare({'user_id': 'user_002'}, {'request_id': 'req_other', 'trace_id': trace, 'trace_provided': True})
            self.assertEqual(404, error.exception.status)
        self.prepare('req_2', 'trace_2')
        with self.assertRaises(AuditError) as error:
            self.store.prepare(self.identity, {'request_id': 'req_3', 'trace_id': 'trace_2', 'trace_provided': True, 'parent_request_id': 'req_1'})
        self.assertEqual(400, error.exception.status)
