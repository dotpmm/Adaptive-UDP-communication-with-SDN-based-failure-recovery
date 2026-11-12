#!/usr/bin/env python3
"""Mininet-free integration test of the socket layer.

A UDP proxy sits between client and server and silently drops everything
during a blackout window (simulating a dead path). We then check that
  * recovery ON  -> client detects the outage, probes, resends: 0 lost packets
  * recovery OFF -> packets sent during the blackout are lost
Run:  python3 tests/test_local_recovery.py
"""
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
PY = sys.executable


def proxy(stop, blackout, t0):
    p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p.bind(('127.0.0.1', 6000))
    q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client = None
    while not stop.is_set():
        r, _, _ = select.select([p, q], [], [], 0.1)
        now = time.time() - t0
        drop = blackout[0] <= now < blackout[1]
        for s in r:
            data, addr = s.recvfrom(2048)
            if drop:
                continue
            if s is p:
                client = addr
                q.sendto(data, ('127.0.0.1', 5000))
            elif client:
                p.sendto(data, client)
    p.close()
    q.close()


def run_case(recovery):
    d = tempfile.mkdtemp()
    srv = subprocess.Popen([PY, os.path.join(SRC, 'server.py'), '--port', '5000',
                            '--log', os.path.join(d, 's.csv')],
                           stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    stop, t0 = threading.Event(), time.time()
    th = threading.Thread(target=proxy, args=(stop, (2.0, 4.0), t0), daemon=True)
    th.start()
    log = os.path.join(d, 'c.csv')
    subprocess.run([PY, os.path.join(SRC, 'client.py'), '--server', '127.0.0.1',
                    '--port', '6000', '--rate', '50', '--duration', '8',
                    '--recovery', recovery, '--log', log, '--drain', '3'],
                   stdout=subprocess.DEVNULL, check=True)
    stop.set()
    srv.terminate()
    srv.wait()
    time.sleep(0.3)
    return json.load(open(log.replace('.csv', '') + '.summary.json'))


if __name__ == '__main__':
    on = run_case('on')
    off = run_case('off')
    print('recovery ON : lost=%d retx=%d recoveries=%d' %
          (on['lost'], on['retransmissions'], len(on['recoveries'])))
    print('recovery OFF: lost=%d' % off['lost'])
    ok = on['lost'] == 0 and len(on['recoveries']) >= 1 and off['lost'] > 50
    print('PASS' if ok else 'FAIL')
    sys.exit(0 if ok else 1)
