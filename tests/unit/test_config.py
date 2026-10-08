import copy
import tempfile
import unittest
from pathlib import Path
import yaml
from scripts.validate_config import ConfigError, UniqueLoader, load, validate, write_snapshot
from tests.helpers import ROOT, test_environment


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load((ROOT / 'config/gateway.example.yaml').read_text())
        self.env = test_environment()

    def test_valid_private_snapshot(self):
        cfg = validate(self.raw, self.env)
        self.assertNotIn('key', cfg['api_keys'][0])
        self.assertEqual('mock-a:8000', cfg['providers']['mock_a']['authority'])
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'config.json'
            write_snapshot(cfg, p)
            self.assertTrue(p.exists())

    def test_quota_switch_defaults_and_validation(self):
        self.raw.pop('fallback', None)
        self.assertFalse(validate(self.raw,self.env)['fallback']['on_key_quota_exhausted'])
        self.raw['fallback']={'on_key_quota_exhausted':True}
        self.assertTrue(validate(self.raw,self.env)['fallback']['on_key_quota_exhausted'])
        self.raw['fallback']['on_key_quota_exhausted']='true'
        with self.assertRaises(ConfigError):validate(self.raw,self.env)
        self.raw['fallback']['on_key_quota_exhausted']=True
        self.raw['providers']['mock_a']['quota_exhaustion_codes']=['bad code']
        with self.assertRaises(ConfigError):validate(self.raw,self.env)

    def test_invalid_configuration_matrix(self):
        mutations = [
            lambda c: c['models']['balanced']['candidates'][0].update(input_rate=-1),
            lambda c: c['request'].update(max_attempts=3),
            lambda c: c.update(timezone='UTC'),
            lambda c: c['providers']['mock_a'].update(base_url='https://user:secret@host'),
            lambda c: c['api_keys'].append(copy.deepcopy(c['api_keys'][0])),
            lambda c: c['plugins'].update(identity=False),
            lambda c: c['plugins'].update(usage_collector=False),
            lambda c: c['audit'].update(sample_rate=2),
            lambda c: c['audit'].update(content_retention_days=31),
            lambda c: c['audit'].update(queue_max_bytes=0),
            lambda c: c['api_keys'][1].update(scopes=['audit:write']),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                raw = copy.deepcopy(self.raw); mutation(raw)
                with self.assertRaises(ConfigError):
                    validate(raw, self.env)
        with self.assertRaises(ConfigError) as error:
            validate(self.raw, {})
        self.assertNotIn(self.env['GATEWAY_KEY_USER1'], str(error.exception))

    def test_duplicate_yaml_and_unsafe_tags(self):
        with self.assertRaises(ConfigError):
            yaml.load('a: 1\na: 2', Loader=UniqueLoader)
        with self.assertRaises(yaml.YAMLError):
            yaml.load('!!python/object/apply:os.system [echo unsafe]', Loader=UniqueLoader)

    def test_independent_optional_plugins_and_multiple_keys(self):
        for flag in ['user_quota', 'fallback', 'observability', 'audit']:
            raw = copy.deepcopy(self.raw); raw['plugins'][flag] = False
            if flag == 'audit': raw['audit']['mode'] = 'off'
            validate(raw, self.env)
        self.raw['api_keys'][1]['user_id'] = 'user_001'
        validate(self.raw, self.env)
