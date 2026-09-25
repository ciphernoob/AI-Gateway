import tempfile
import json
import unittest
from pathlib import Path
import yaml
from scripts.validate_config import load, validate, ConfigError
from services.audit_store.store import Store, AuditError
from tests.helpers import ROOT, test_environment


class AdminTests(unittest.TestCase):
    def test_disabled_users_and_providers(self):
        raw = yaml.safe_load((ROOT / 'config/gateway.example.yaml').read_text())
        raw['users']['user_001']['disabled'] = True
        raw['api_keys'][1]['agent_id'] = ''
        raw['providers']['mock_a']['disabled'] = True
        cfg = validate(raw, test_environment())
        self.assertTrue(cfg['api_keys'][0]['disabled'])
        self.assertNotIn('agent_id', cfg['api_keys'][1])
        self.assertEqual(['mock_b'], [c['provider'] for c in cfg['models']['balanced']['candidates']])
        raw['providers']['mock_b']['disabled'] = True
        with self.assertRaises(ConfigError):
            validate(raw, test_environment())

    def test_admin_query_and_redaction_survive_restart(self):
        cfg = load(ROOT / 'config/gateway.example.yaml', test_environment())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.db'
            store = Store(path, cfg, revision_dir=directory)
            key = 'new-provider-secret-long-value'
            (Path(directory) / 'v1.json').write_text(json.dumps({'revision': 'v1', 'known_secrets': [key]}))
            store.sync_admin_redaction('v1')
            self.assertEqual('[REDACTED]', store.redact(key))
            for uid in ['user_001', 'user_002']:
                store.prepare({'user_id': uid, 'agent_id': ''}, {'request_id': 'r_' + uid,
                    'trace_id': 't_' + uid, 'trace_provided': False, 'requested_model': 'balanced',
                    'deadline': store.clock() + 30, 'request': {'messages': [{'content': key}]}})
            result = store.admin_query({'resource': 'requests', 'params': {'limit': '1'}})
            self.assertTrue(result['has_more'])
            self.assertEqual(1, len(result['data']))
            with self.assertRaises(AuditError):
                store.detail('user_001', 'r_user_002', {})
            result = store.admin_query({'resource': 'request', 'id': 'r_user_002', 'params': {'include_content': 'true'}})
            self.assertNotIn(key, str(result))
            self.assertGreater(store.db.execute('SELECT COUNT(*) FROM admin_reads').fetchone()[0], 0)
            self.assertNotIn(key, '\n'.join(store.db.iterdump()))
            store.close()
            store = Store(path, cfg, revision_dir=directory)
            self.assertEqual('[REDACTED]', store.redact(key))
            store.close()

    def test_redaction_reference_retirement_and_legacy_migration(self):
        cfg = load(ROOT / 'config/gateway.example.yaml', test_environment())
        with tempfile.TemporaryDirectory() as directory:
            now = [1800000000]
            path = Path(directory) / 'audit.db'
            old, new = 'retired-credential-value', 'current-credential-value'
            for revision, value in [('v1', old), ('v2', new)]:
                (Path(directory) / (revision + '.json')).write_text(json.dumps({'revision': revision, 'known_secrets': [value]}))
            store = Store(path, cfg, lambda: now[0], directory)
            store.db.execute('CREATE TABLE admin_redaction(value TEXT PRIMARY KEY, expires REAL NOT NULL)')
            store.db.execute('INSERT INTO admin_redaction VALUES(?,?)', (old, 253402300799))
            store.close()
            store = Store(path, cfg, lambda: now[0], directory)
            self.assertEqual('[REDACTED]', store.redact(old))
            self.assertNotIn(old, '\n'.join(store.db.iterdump()))
            store.sync_admin_redaction('v2')
            self.assertEqual('[REDACTED]', store.redact(old))
            now[0] += (cfg['audit']['content_retention_days'] + 2) * 86400
            store.refresh_admin_redaction()
            self.assertEqual(old, store.redact(old))
            self.assertEqual('[REDACTED]', store.redact(new))
            with self.assertRaises(AuditError):
                store.sync_admin_redaction('../v1')
            store.close()
