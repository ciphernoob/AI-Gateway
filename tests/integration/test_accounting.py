import concurrent.futures
import json
import os
import time
import unittest
import uuid
from tests.helpers import ROOT, Redis


@unittest.skipUnless(os.environ.get('RUN_INTEGRATION'), 'requires real Redis')
class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.prefix = 'test:' + uuid.uuid4().hex + ':'
        self.now = int(time.time())
        self.script = (ROOT / 'redis/accounting.lua').read_text()
        self.client = self.connect()

    def connect(self):
        return Redis(password=os.environ['REDIS_PASSWORD'])

    def tearDown(self):
        keys = self.client.call('KEYS', self.prefix + '*')
        if keys:
            self.client.call('DEL', *keys)
        self.client.close()

    def keys(self, attempt, model='coding'):
        return [self.prefix + 'attempt:' + str(attempt), self.prefix + 'pending',
                self.prefix + 'user:u:model:' + model + ':day', self.prefix + 'user:u:model:' + model + ':month']

    def operation(self, client, keys, op, **kwargs):
        return client.call('EVAL', self.script, len(keys), *keys, json.dumps(dict(op=op, now=self.now, **kwargs)))

    def begin(self, client, attempt, model='coding', **updates):
        keys = self.keys(attempt, model)
        meta = dict(started_at=self.now, deadline=self.now + 40, day_expiry=self.now + 100,
                    month_expiry=self.now + 1000, dedupe_expiry=self.now + 2000, replay_max_days=7,
                    logical_model=model, keys=keys)
        meta.update(updates)
        self.assertEqual(['created'], self.operation(client, keys, 'begin', meta=meta, max_pending=1000))
        return keys

    def test_concurrent_provider_usage_is_idempotent_and_model_scoped(self):
        def work(index):
            c = self.connect()
            try:
                keys = self.begin(c, index, 'coding')
                self.assertEqual(['dispatching'], self.operation(c, keys, 'dispatch'))
                usage = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
                self.assertEqual(['applied'], self.operation(c, keys, 'settle', usage=usage))
                self.assertEqual(['duplicate'], self.operation(c, keys, 'settle', usage=usage))
            finally:
                c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(work, range(100)))
        self.assertEqual('1700', self.client.call('HGET', self.keys(0, 'coding')[2], 'total_tokens'))
        self.assertIsNone(self.client.call('HGET', self.keys(0, 'balanced')[2], 'total_tokens'))
        self.assertEqual(0, self.client.call('ZCARD', self.prefix + 'pending'))

    def test_unknown_is_not_replaced_by_unobserved_values(self):
        keys = self.begin(self.client, 1)
        self.operation(self.client, keys, 'dispatch')
        self.assertEqual(['applied'], self.operation(self.client, keys, 'settle', usage=None))
        self.assertEqual('1', self.client.call('HGET', keys[2], 'unknown_attempts'))
        usage = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        self.assertEqual(['conflict'], self.operation(self.client, keys, 'settle', usage=usage))
        self.assertIsNone(self.client.call('HGET', keys[2], 'total_tokens'))

    def test_invalid_usage_and_overflow_do_not_partially_write(self):
        keys = self.begin(self.client, 1)
        self.operation(self.client, keys, 'dispatch')
        self.assertEqual(['invalid_usage'], self.operation(self.client, keys, 'settle', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=3)))
        self.client.call('HSET', keys[3], 'total_tokens', str(2**53 - 1))
        with self.assertRaisesRegex(RuntimeError, 'invalid_integer'):
            self.operation(self.client, keys, 'settle', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        self.assertIsNone(self.client.call('HGET', keys[2], 'total_tokens'))
        self.assertEqual('pending', self.client.call('HGET', keys[0], 'usage_state'))

    def test_unsent_attempt_closes_without_zero_or_unknown(self):
        keys = self.begin(self.client, 1)
        self.assertEqual(['unsent'], self.operation(self.client, keys, 'unsent'))
        self.assertEqual('0', self.client.call('HGET', keys[2], 'pending_attempts'))
        self.assertIsNone(self.client.call('HGET', keys[2], 'total_tokens'))
        self.assertIsNone(self.client.call('HGET', keys[2], 'unknown_attempts'))
        self.assertEqual(['duplicate'], self.operation(self.client, keys, 'unsent'))

    def test_recovery_claim_distinguishes_unsent_and_unknown(self):
        keys = self.begin(self.client, 1, deadline=self.now)
        self.assertEqual(['unsent'], self.operation(self.client, keys, 'claim'))
        self.assertIsNone(self.client.call('HGET', keys[2], 'unknown_attempts'))
        keys = self.begin(self.client, 2, deadline=self.now)
        self.operation(self.client, keys, 'dispatch')
        self.assertEqual(['unknown'], self.operation(self.client, keys, 'claim'))
        self.assertEqual(['applied'], self.operation(self.client, keys, 'settle', usage=None))
        self.assertEqual('1', self.client.call('HGET', keys[2], 'unknown_attempts'))
