"""Operator-only reconciliation using persisted pricing, never current model prices."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from tests.helpers import Redis


def reconcile(config, attempt_id, usage, evidence):
    if not attempt_id.startswith('attempt_') or not evidence or len(evidence) > 200:
        raise ValueError('attempt ID and a short evidence reference are required')
    if set(usage) != {'prompt_tokens', 'completion_tokens', 'total_tokens'}:
        raise ValueError('three token counts are required')
    if any(type(n) is not int or not 0 <= n < 2**53 for n in usage.values()) or usage['prompt_tokens'] + usage['completion_tokens'] != usage['total_tokens']:
        raise ValueError('invalid token counts')
    for secret in config['known_secrets']:
        if secret in evidence:
            raise ValueError('evidence reference contains a credential')
    r = Redis(config['redis']['host'], config['redis']['port'], config['redis']['password'])
    try:
        raw = r.call('HGET', 'usage:attempt:' + attempt_id, 'meta')
        if not raw:
            raise ValueError('attempt not found')
        meta = json.loads(raw); price = meta['candidate']
        product = usage['prompt_tokens'] * price['input_rate'] + usage['completion_tokens'] * price['output_rate']
        if product >= 2**53:
            raise ValueError('cost overflow')
        cost = (product + 999999) // 1000000
        identity = {'user_id': meta['user_id'], 'agent_id': meta.get('agent_id')}
        data = {'request_id': meta['request_id'], 'attempt_id': attempt_id, 'usage': usage, 'cost': cost, 'evidence': evidence}
        event_id = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

        def record(stage):
            if not config['plugins']['audit']:
                return
            payload = dict(data, stage=stage, reconciliation_id=event_id)
            request = Request(config['audit']['base_url'] + '/internal/reconcile',
                              data=json.dumps({'identity': identity, 'data': payload}).encode(),
                              headers={'Authorization': 'Bearer ' + config['audit']['token'], 'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError('audit reconciliation failed')
        record('prepared')
        script = (Path(__file__).resolve().parents[1] / 'redis/accounting.lua').read_text()
        results = []
        for op, value in [('quota', {'usage': usage}), ('budget', {'cost': cost})]:
            args = dict(op=op, now=int(time.time()), reconcile=True, evidence=evidence, **value)
            result = r.call('EVAL', script, len(meta['keys']), *meta['keys'], json.dumps(args))
            if result[0] not in ('applied', 'duplicate', 'disabled'):
                raise RuntimeError('reconciliation consumer rejected: ' + op + '/' + result[0])
            results.append(result[0])
        record('applied')
        return {'attempt_id': attempt_id, 'quota': results[0], 'budget': results[1], 'cost_micro_usd': cost}
    finally:
        r.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='/runtime/config.json')
    p.add_argument('--attempt-id', required=True)
    p.add_argument('--usage-file', required=True, help='JSON containing exact provider or verified zero token counts')
    p.add_argument('--evidence', required=True, help='operator ticket/provider invoice reference; no prompt or credentials')
    args = p.parse_args()
    try:
        result = reconcile(json.loads(Path(args.config).read_text()), args.attempt_id,
                           json.loads(Path(args.usage_file).read_text()), args.evidence)
    except Exception:
        p.exit(1, 'Reconciliation not completed. Preserve evidence and retry the same input after resolving the dependency/conflict.\n')
    print(json.dumps(result))


if __name__ == '__main__': main()
