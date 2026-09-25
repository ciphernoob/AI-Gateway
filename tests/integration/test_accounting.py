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
        if keys: self.client.call('DEL', *keys)
        self.client.close()

    def keys(self, attempt):
        return [self.prefix + x for x in ['attempt:' + str(attempt), 'pending', 'day', 'month', 'reservation:' + str(attempt), 'global', 'agent', 'model']]

    def operation(self, client, keys, op, **kwargs):
        return client.call('EVAL', self.script, len(keys), *keys, json.dumps(dict(op=op, now=self.now, **kwargs)))

    def begin(self, client, attempt, limit=1000000, **updates):
        keys = self.keys(attempt)
        meta = dict(started_at=self.now, deadline=self.now + 40, quota_enabled=True, budget_enabled=True,
                    dimensions=[6, 7, 8], limits=[limit] * 3, day_expiry=self.now + 100,
                    month_expiry=self.now + 1000, dedupe_expiry=self.now + 2000, replay_max_days=7)
        meta.update(updates)
        self.assertEqual(['created'], self.operation(client, keys, 'begin', meta=meta, max_pending=1000))
        return keys

    def test_100_concurrent_events_and_duplicate_consumers(self):
        def work(index):
            c = self.connect()
            try:
                keys = self.begin(c, index)
                self.operation(c, keys, 'reserve', amount=100)
                self.operation(c, keys, 'dispatch')
                usage = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
                self.assertEqual(['applied'], self.operation(c, keys, 'quota', usage=usage))
                self.assertEqual(['duplicate'], self.operation(c, keys, 'quota', usage=usage))
                self.assertEqual('applied', self.operation(c, keys, 'budget', cost=60)[0])
                self.assertEqual(['duplicate'], self.operation(c, keys, 'budget', cost=60))
            finally: c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(work, range(100)))
        self.assertEqual('1700', self.client.call('HGET', self.prefix + 'day', 'total_tokens'))
        self.assertEqual('1700', self.client.call('HGET', self.prefix + 'month', 'total_tokens'))
        self.assertEqual('6000', self.client.call('HGET', self.prefix + 'global', 'spent'))
        self.assertEqual('0', self.client.call('HGET', self.prefix + 'global', 'reserved'))
        self.assertEqual(0, self.client.call('ZCARD', self.prefix + 'pending'))

    def test_100_contenders_only_ten_reservations(self):
        def work(index):
            c = self.connect()
            try:
                keys = self.begin(c, index, limit=1000)
                return self.operation(c, keys, 'reserve', amount=100)[0]
            finally: c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            values = list(pool.map(work, range(100)))
        self.assertEqual(10, values.count('reserved'))
        self.assertEqual(90, values.count('denied'))
        for ledger in ['global', 'agent', 'model']:
            self.assertEqual('1000', self.client.call('HGET', self.prefix + ledger, 'reserved'))

    def test_unknown_reconciliation_conflict_and_overrun(self):
        keys = self.begin(self.client, 1)
        self.operation(self.client, keys, 'reserve', amount=100)
        self.operation(self.client, keys, 'dispatch')
        self.operation(self.client, keys, 'quota', usage=None)
        self.operation(self.client, keys, 'budget', cost=None)
        self.assertEqual('100', self.client.call('HGET', keys[5], 'reserved'))
        self.assertEqual('1', self.client.call('HGET', keys[2], 'unknown_attempts'))
        self.assertEqual(-1, self.client.call('TTL', keys[0]))
        usage = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        self.operation(self.client, keys, 'quota', usage=usage, reconcile=True, evidence='ticket-1')
        self.assertEqual(['applied', 'overrun'], self.operation(self.client, keys, 'budget', cost=150, reconcile=True))
        self.assertEqual('0', self.client.call('HGET', keys[2], 'unknown_attempts'))
        self.assertEqual('150', self.client.call('HGET', keys[5], 'spent'))
        self.assertEqual(['conflict'], self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2)))

    def test_preflight_type_and_integer_errors_do_not_partial_write(self):
        keys = self.begin(self.client, 1)
        self.client.call('SET', keys[7], 'wrong-type')
        with self.assertRaisesRegex(RuntimeError, 'invalid_key_type'):
            self.operation(self.client, keys, 'reserve', amount=100)
        self.assertIsNone(self.client.call('HGET', keys[5], 'reserved'))
        self.client.call('DEL', keys[7])
        self.client.call('HSET', keys[3], 'total_tokens', str(2**53 - 1))
        with self.assertRaisesRegex(RuntimeError, 'invalid_integer'):
            self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        self.assertIsNone(self.client.call('HGET', keys[2], 'total_tokens'))
        self.assertEqual('pending', self.client.call('HGET', keys[0], 'quota_state'))

    def test_recovery_claim_dispatch_race_and_expired_period(self):
        keys = self.begin(self.client, 1, deadline=self.now)
        self.operation(self.client, keys, 'reserve', amount=100)
        self.assertEqual(['unsent'], self.operation(self.client, keys, 'claim'))
        self.assertEqual(['closed'], self.operation(self.client, keys, 'dispatch'))
        self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0))
        self.operation(self.client, keys, 'budget', cost=0)
        keys = self.begin(self.client, 2, deadline=self.now)
        self.operation(self.client, keys, 'reserve', amount=100)
        self.operation(self.client, keys, 'dispatch')
        self.assertEqual(['unknown'], self.operation(self.client, keys, 'claim'))
        self.operation(self.client, keys, 'quota', usage=None)
        self.now += 200
        self.client.call('DEL', keys[2])  # simulate expired daily bucket with controlled clock
        self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2), reconcile=True)
        self.assertEqual(0, self.client.call('EXISTS', keys[2]))
        self.assertEqual('2', self.client.call('HGET', keys[3], 'total_tokens'))

    def test_replay_ttl_and_script_flush(self):
        keys = self.begin(self.client, 1)
        self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0))
        self.operation(self.client, keys, 'budget', cost=0)
        before = self.client.call('EXPIRETIME', keys[0])
        self.client.call('SCRIPT', 'FLUSH')  # client uses EVAL, not a stale SHA cache
        self.operation(self.client, keys, 'quota', usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0))
        self.assertEqual(before, self.client.call('EXPIRETIME', keys[0]))
        self.now += 8 * 86400
        self.assertEqual(['too_old'], self.operation(self.client, keys, 'quota', usage=None))

    def test_partial_consumer_failure_replays_without_double_charge(self):
        keys = self.begin(self.client, 1)
        self.operation(self.client, keys, 'reserve', amount=100)
        exact = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        self.operation(self.client, keys, 'quota', usage=exact)
        self.client.call('HSET', keys[7], 'reserved', '-1')
        with self.assertRaisesRegex(RuntimeError, 'invalid_integer'):
            self.operation(self.client, keys, 'budget', cost=60)
        self.assertIsNone(self.client.call('HGET', keys[5], 'spent'))
        self.client.call('HSET', keys[7], 'reserved', '100')
        self.assertEqual(['duplicate'], self.operation(self.client, keys, 'quota', usage=exact))
        self.assertEqual('applied', self.operation(self.client, keys, 'budget', cost=60)[0])
        self.assertEqual('17', self.client.call('HGET', keys[2], 'total_tokens'))
        self.assertEqual('60', self.client.call('HGET', keys[5], 'spent'))
