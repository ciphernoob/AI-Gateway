"""Managed deployments never overwrite a previously published runtime on restart."""
from pathlib import Path
from scripts.validate_config import load, write_snapshot

if __name__ == '__main__':
    target = Path('/runtime/config.json')
    if not target.exists():
        config = load('/config/gateway.yaml')
        config['revision'] = 'bootstrap'
        write_snapshot(config, target)
    print('Managed runtime ready')
