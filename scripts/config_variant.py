"""Test-only runtime variants. Run only in a disposable Compose test project."""
import copy
import sys
import yaml
from scripts.validate_config import validate, write_snapshot


def main():
    variant = sys.argv[1]
    with open('/config/gateway.yaml') as file:
        raw = yaml.safe_load(file)
    if variant.startswith('key_quota'):
        raw['fallback'] = {'on_key_quota_exhausted': variant != 'key_quota_off'}
        if variant == 'key_quota_same': raw['providers']['mock_b']['key_env'] = 'PROVIDER_KEY_A'
        if variant == 'key_quota_single': raw['request']['max_attempts'] = 1
        if variant == 'key_quota_custom': raw['providers']['mock_a']['quota_exhaustion_codes'] = ['credits_empty']
    elif variant in ('fallback', 'both_down', 'single_attempt'):
        raw['providers']['mock_a']['base_url'] = 'http://mock-a:1'
        if variant == 'both_down': raw['providers']['mock_b']['base_url'] = 'http://mock-b:1'
        if variant == 'single_attempt': raw['request']['max_attempts'] = 1
    elif variant == 'quota_exceeded':
        raw['users']['user_001']['daily_token_limit'] = 0
    elif variant == 'metrics_off':
        raw['plugins']['observability'] = False
    elif variant != 'normal':
        raise ValueError('unknown test variant')
    write_snapshot(validate(raw), '/runtime/config.json')
    print('Test variant written:', variant)


if __name__ == '__main__': main()
