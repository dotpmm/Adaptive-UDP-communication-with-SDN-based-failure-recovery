#!/usr/bin/env python3
"""Run ONE experiment: controller + Mininet + UDP server/clients + failure schedule.

Must be run as root (Mininet).  Example:
  sudo env "PATH=$PATH" python3 experiments/run_experiment.py \
       --mode dynamic --recovery on --duration 30 \
       --events "8:down:s2-s4,20:up:s2-s4" --out results/single/dynamic-app/run1

--mode dynamic|static   controller behaviour (static = no rerouting baseline)
--recovery on|off       application-level recovery in the client
--events                comma list  <t_seconds>:<down|up>:<swA>-<swB>
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'topology'))

from mininet.log import setLogLevel   # noqa: E402
from topo import build_net            # noqa: E402


def parse_events(s):
    ev = []
    for item in filter(None, s.split(',')):
        t, act, link = item.split(':')
        x, y = link.split('-')
        ev.append((float(t), act, x, y))
    return sorted(ev)


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['dynamic', 'static'], default='dynamic')
    ap.add_argument('--recovery', choices=['on', 'off'], default='on')
    ap.add_argument('--events', default='8:down:s2-s4,20:up:s2-s4')
    ap.add_argument('--duration', type=float, default=30)
    ap.add_argument('--rate', type=float, default=50)
    ap.add_argument('--clients', type=int, default=2, choices=[1, 2])
    ap.add_argument('--warmup', type=float, default=6, help='seconds for LLDP discovery')
    ap.add_argument('--ryu', default='ryu-manager')
    ap.add_argument('--external-controller', action='store_true',
                    help='do not start Ryu (you started it yourself)')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    setLogLevel('warning')
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    events = parse_events(a.events)
    ctl = None
    net = None
    try:
        if not a.external_controller:
            env = dict(os.environ, ROUTING_MODE=a.mode, RESULTS_DIR=out)
            ctl = subprocess.Popen(
                [a.ryu, '--observe-links',
                 os.path.join(ROOT, 'controller', 'adaptive_router.py')],
                env=env, stdout=open(os.path.join(out, 'ryu.log'), 'w'),
                stderr=subprocess.STDOUT)
            if not wait_port(6653):
                raise RuntimeError('Ryu did not start (see %s/ryu.log)' % out)

        net = build_net()
        net.start()
        net.waitConnected(timeout=15)
        time.sleep(a.warmup)
        h1, h2, h3 = net.get('h1', 'h2', 'h3')
        if net.ping([h1, h2, h3], timeout='2') > 0:
            net.ping([h1, h2, h3], timeout='2')     # first round teaches the controller

        py = sys.executable
        srv = h2.popen([py, os.path.join(ROOT, 'src', 'server.py'), '--port', '5000',
                        '--log', os.path.join(out, 'server_log.csv')],
                       stdout=open(os.path.join(out, 'server.out'), 'w'),
                       stderr=subprocess.STDOUT)
        time.sleep(1)
        procs = []
        for idx, h in enumerate([h1, h3][:a.clients], start=1):
            procs.append(h.popen(
                [py, os.path.join(ROOT, 'src', 'client.py'), '--server', h2.IP(),
                 '--id', str(idx), '--rate', str(a.rate), '--duration', str(a.duration),
                 '--recovery', a.recovery, '--log', os.path.join(out, 'client%d.csv' % idx)],
                stdout=open(os.path.join(out, 'client%d.out' % idx), 'w'),
                stderr=subprocess.STDOUT))
        t_start = time.time()

        applied = []
        for t, act, x, y in events:
            time.sleep(max(0, t_start + t - time.time()))
            ts = time.time()
            net.configLinkStatus(x, y, act)
            applied.append({'t_rel': t, 'ts': ts, 'action': act, 'link': '%s-%s' % (x, y)})
            print('[%.2fs] link %s-%s %s' % (ts - t_start, x, y, act), flush=True)

        for p in procs:
            p.wait(timeout=a.duration + 40)
        srv.send_signal(signal.SIGTERM)
        srv.wait(timeout=10)

        with open(os.path.join(out, 'meta.json'), 'w') as f:
            json.dump({'mode': a.mode, 'recovery': a.recovery, 'rate': a.rate,
                       'duration': a.duration, 'clients': a.clients,
                       't_start': t_start, 'events': applied}, f, indent=2)
        print('done ->', out)
    finally:
        if net:
            net.stop()
        if ctl:
            ctl.terminate()
            try:
                ctl.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ctl.kill()


if __name__ == '__main__':
    main()
