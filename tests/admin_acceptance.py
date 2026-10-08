"""Destructive fault injection ONLY for ai-gateway-admin-test, never the dev project."""
import concurrent.futures
import http.cookiejar
import json
import secrets
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get('ADMIN_TEST_URL', 'https://localhost:18443')
GATEWAY = os.environ.get('GATEWAY_TEST_URL', 'http://localhost:18081')
ENV = dict(line.split('=', 1) for line in (ROOT / '.env').read_text().splitlines() if line and not line.startswith('#'))
PASSWORD = (ROOT / '.admin/secrets/ADMIN_PASSWORD').read_text().strip()
cookies = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()), urllib.request.HTTPCookieProcessor(cookies))
csrf = ''


def call(path, body=None, method=None):
    req = urllib.request.Request(ADMIN + '/admin/api/v1' + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ('POST' if body is not None else 'GET'),
                                 headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf})
    try:
        with client.open(req, timeout=95) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def docker(*args):
    command = (['wsl.exe', '-d', 'Ubuntu-L4', '-u', 'root', '--'] if os.name == 'nt' else []) + ['docker', *args]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise RuntimeError('test-container operation failed: ' + args[0])
    return result.stdout


def container(service):
    return 'ai-gateway-admin-test-' + service + '-1'


def control(mode='normal', delay=0.03):
    data = json.dumps({'mode': mode, 'delay': delay}).encode()
    code = "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:8000/control',data=" + repr(data) + ",headers={'Content-Type':'application/json'})).read()"
    docker('exec', container('mock-a'), 'python', '-c', code)


def chat(stream=False, key=None, content='admin fault acceptance', trace=None):
    body = {'model': 'balanced', 'messages': [{'role': 'user', 'content': content}], 'stream': stream, 'max_tokens': 16}
    headers = {'Authorization': 'Bearer ' + (key or ENV['GATEWAY_KEY_USER1']), 'Content-Type': 'application/json'}
    if trace:
        headers['X-Trace-ID'] = trace
    req = urllib.request.Request(GATEWAY + '/models/balanced/v1/chat/completions', data=json.dumps(body).encode(),
                                 headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as exc:
        return exc


def draft():
    status, result = call('/draft')
    assert status == 200
    return result


def settings(enabled):
    d = draft()
    status, result = call('/settings', {'revision': d['revision'], 'value': {'public_url': d['document']['public_url'],
        'fallback': {'on_key_quota_exhausted': enabled}}}, 'PUT')
    assert status == 200, result
    return result['revision']


def publish(version=None):
    return call('/publish', {'revision': draft()['revision'], 'version': version or ''})


def main():
    global csrf
    status, result = call('/login', {'username': 'admin', 'password': PASSWORD})
    assert status == 200
    csrf = result['csrf']
    original = call('/overview')[1]['config_revision']
    original_fallback = draft()['document']['config']['fallback']['on_key_quota_exhausted']
    original_user = draft()['document']['config']['users']['user_002']
    try:
        # Rotate an unreferenced provider credential and ensure both values are
        # removed from content; no live provider password is changed.
        old_secret, new_secret = secrets.token_hex(24), secrets.token_hex(24)
        for value in [old_secret, new_secret]:
            assert call('/providers/rotation_probe', {'revision': draft()['revision'],
                'value': {'base_url': 'http://mock-b:8000'}, 'secret': value}, 'PUT')[0] == 200
            assert publish()[0] == 200
        with chat(content=old_secret + ' ' + new_secret) as res:
            assert res.status == 200
            rotation_id = res.headers['X-Request-ID']
            res.read()
        time.sleep(1)
        status, detail = call('/audit/requests/' + rotation_id + '?include_content=true')
        assert status == 200
        assert old_secret not in json.dumps(detail) and new_secret not in json.dumps(detail)
        status, events = call('/logs?request_id=' + rotation_id)
        assert status == 200 and ENV['GATEWAY_KEY_USER1'] not in json.dumps(events)
        with chat(trace=ENV['GATEWAY_KEY_USER1']) as res:
            assert res.status == 404  # Only an existing owned trace can be reused.
            trace_request = res.headers['X-Request-ID']
            res.read()
        time.sleep(0.2)
        status, events = call('/logs?request_id=' + trace_request)
        assert status == 200 and events['data'] and ENV['GATEWAY_KEY_USER1'] not in json.dumps(events)
        assert call('/providers/rotation_probe', {'revision': draft()['revision'], 'value': {}}, 'DELETE')[0] == 200
        print('PASS credential rotation redacts old/new content and credential-bearing trace metadata', flush=True)

        assert call('/users/user_002', {'revision': draft()['revision'], 'value': {
            'daily_token_limit': 0, 'monthly_token_limit': 0, 'disabled': False}}, 'PUT')[0] == 200
        assert publish()[0] == 200
        with chat(key=ENV['GATEWAY_KEY_USER2']) as res:
            assert res.status == 200
            res.read()
        status, issued = call('/keys', {'revision': draft()['revision'], 'value': {'user_id': 'user_002', 'scopes': ['chat:write']}})
        assert status == 200
        reference = draft()['document']['config']['api_keys'][-1]['key_env']
        assert publish()[0] == 200
        with chat(key=issued['key']) as res:
            assert res.status == 200
            res.read()
        assert call('/keys/' + reference, {'revision': draft()['revision'], 'value': {}}, 'DELETE')[0] == 200
        assert publish()[0] == 200
        with chat(key=issued['key']) as res:
            assert res.status == 401
        assert call('/users/user_002', {'revision': draft()['revision'], 'value': original_user}, 'PUT')[0] == 200
        print('PASS zero Token allowance is statistical; Gateway Key issue/revoke takes effect on publish', flush=True)

        settings(False); assert publish()[0] == 200
        control('quota')
        with chat() as res:
            assert res.status == 429
        settings(True); assert publish()[0] == 200
        with chat() as res:
            assert res.status == 200
            rid = res.headers['X-Request-ID']
            assert json.load(res)['model'] == 'mock-model-b'
        time.sleep(1)
        status, events = call('/logs?request_id=' + rid)
        assert status == 200 and any(x['event'] == 'fallback.selected' for x in events['data'])
        assert ENV['PROVIDER_KEY_A'] not in json.dumps(events)
        status, usage = call('/usage?user_id=user_001')
        assert status == 200 and usage['data'][0]['daily']['unknown_attempts'] > 0
        assert usage['data'][0]['daily']['remaining_is_exact'] is False
        status, expired = call('/usage?user_id=user_001&day=2020-01-01&month=2020-01')
        assert status == 200 and expired['data'][0]['daily']['available'] is False
        print('PASS managed quota switch and correlated persistent log', flush=True)

        control('normal', 0.5)
        with chat(True) as stream:
            assert stream.status == 200
            first = stream.readline()
            assert first.startswith(b'data:')
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(publish)
                competing = pool.submit(publish)
                rest = stream.read()
                assert b'[DONE]' in first + rest
                assert sorted([future.result()[0], competing.result()[0]]) == [200, 409]
        print('PASS in-flight SSE survives graceful publication; concurrent publish rejected', flush=True)
        control()

        active = call('/overview')[1]['config_revision']
        docker('stop', container('audit-store'))
        assert call('/audit/requests')[0] == 503
        assert publish()[0] == 503
        docker('start', container('audit-store'))
        time.sleep(2)
        assert call('/overview')[1]['config_revision'] == active
        print('PASS audit outage blocks publication without changing active configuration', flush=True)

        docker('stop', container('redis'))
        assert call('/usage')[0] == 503
        docker('start', container('redis'))
        time.sleep(2)
        assert call('/usage')[0] == 200
        print('PASS Redis outage is explicit, not zero usage', flush=True)

        # Inject a journaled publication whose target is already running, then
        # restart to test crash recovery without outputting private snapshots.
        code = "import sqlite3; c=sqlite3.connect('/data/admin.db'); c.execute(\"UPDATE versions SET status='publishing' WHERE status='active'\"); c.commit()"
        docker('exec', container('admin'), 'python', '-c', code)
        docker('restart', container('admin'))
        for _ in range(30):
            try:
                status, result = call('/overview')
                if status == 200 and result['config_revision'] == active:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise AssertionError('publication recovery did not finish')
        print('PASS restart reconciles journal with actual active revision', flush=True)

        # A syntax failure occurs after snapshot replacement but before reload.
        # The agent must restore the old file and remain recoverable.
        docker('exec', container('gateway'), 'sh', '-c', 'cp /app/nginx/nginx.conf /app/run/nginx.acceptance.conf && printf "\\nINVALID_DIRECTIVE;\\n" >> /app/nginx/nginx.conf')
        try:
            assert publish()[0] == 503
        finally:
            docker('exec', container('gateway'), 'sh', '-c', 'cp /app/run/nginx.acceptance.conf /app/nginx/nginx.conf')
        docker('restart', container('admin'))
        time.sleep(3)
        assert call('/overview')[1]['config_revision'] == active
        print('PASS failed reload retains previous runtime and active version', flush=True)

        before = call('/usage?user_id=user_001')[1]['data'][0]['daily']['total_tokens']
        status, result = publish(original)
        assert status == 200, result
        assert call('/usage?user_id=user_001')[1]['data'][0]['daily']['total_tokens'] >= before
        print('PASS rollback preserves accounting data', flush=True)
    finally:
        for service in ['redis', 'audit-store']:
            docker('start', container(service))
        control()
        assert call('/users/user_002', {'revision': draft()['revision'], 'value': original_user}, 'PUT')[0] == 200
        settings(original_fallback)
    print('All managed publication fault checks passed', flush=True)


if __name__ == '__main__':
    main()
