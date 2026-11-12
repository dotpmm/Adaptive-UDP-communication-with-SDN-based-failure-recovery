#!/usr/bin/env python3
"""Mininet-free test of the dashboard backend (simulation mode).

Starts `dashboard/app.py --sim`, then drives it through the HTTP API exactly like the UI:
  1. dynamic mode : break s2-s4  -> controller detects, path moves to s1>s3>s5>s4, no loss
  2. double fault : break s3-s5  -> UNREACHABLE (path None), restore -> traffic resumes
  3. static mode  : break s2-s4  -> controller stays unaware, traffic black-holed
Run:  python3 tests/test_dashboard_sim.py      (about 25 s)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8097
B = 'http://127.0.0.1:%d' % PORT


def get(p):
    return json.load(urllib.request.urlopen(B + p))


def post(p, d=None):
    r = urllib.request.Request(B + p, json.dumps(d or {}).encode(),
                               {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(r))


def check(name, cond, extra=''):
    print('%-62s %s %s' % (name, 'PASS' if cond else 'FAIL', extra))
    return bool(cond)


def main():
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'dashboard', 'app.py'), '--sim',
                             '--port', str(PORT), '--out', '/tmp/dash_test'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = []
    try:
        for _ in range(40):
            try:
                get('/api/state')
                break
            except OSError:
                time.sleep(0.25)
        time.sleep(2)
        s = get('/api/state')
        ok.append(check('baseline: path s1>s2>s4, traffic flowing', s['paths']['1'] == ['s1', 's2', 's4']
                        and s['clients']['1']['acked'] > 50))
        post('/api/link', {'link': 's2-s4', 'action': 'down'})
        time.sleep(2.5)
        s = get('/api/state')
        i = s['incidents'][-1]
        ok.append(check('dynamic: reroute via s3-s5', s['paths']['1'] == ['s1', 's3', 's5', 's4']))
        ok.append(check('dynamic: detect < 200 ms', i['detect_ms'] is not None and i['detect_ms'] < 200,
                        '(%s ms)' % i['detect_ms']))
        ok.append(check('dynamic: recovery < 1 s', i['recovery_ms'] is not None and i['recovery_ms'] < 1000,
                        '(%s ms)' % i['recovery_ms']))
        ok.append(check('dynamic+app: zero lost messages', s['clients']['1']['lost'] == 0))
        post('/api/link', {'link': 's3-s5', 'action': 'down'})
        time.sleep(1.5)
        s = get('/api/state')
        ok.append(check('double fault: no path (UNREACHABLE)', s['paths']['1'] is None))
        post('/api/restore')
        time.sleep(4)
        s = get('/api/state')
        ok.append(check('restored: primary path again', s['paths']['1'] == ['s1', 's2', 's4']))
        ok.append(check('app resumed after double fault', s['clients']['1']['state'] == 'NORMAL'))
        post('/api/mode', {'mode': 'static'})
        post('/api/traffic', {'action': 'start', 'recovery': False, 'rate': 50, 'clients': 1})
        time.sleep(2)
        post('/api/link', {'link': 's2-s4', 'action': 'down'})
        time.sleep(2.5)
        s = get('/api/state')
        ok.append(check('static baseline: controller unaware, path unchanged',
                        s['paths']['1'] == ['s1', 's2', 's4'] and s['links']['s2-s4']['ctl']))
        ok.append(check('static baseline: messages lost', s['clients']['1']['lost'] > 20,
                        '(%d lost)' % s['clients']['1']['lost']))
        post('/api/restore')
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print('PASS' if all(ok) else 'FAIL')
    sys.exit(0 if all(ok) else 1)


if __name__ == '__main__':
    main()
