#!/usr/bin/env python3
"""Live web dashboard for the Adaptive Communication Network (SDN + UDP).

  Real demo (Mininet + Ryu, needs root):
      sudo env "PATH=$PATH" python3 dashboard/app.py
  UI preview without Mininet/Ryu (software-emulated network, any OS):
      python3 dashboard/app.py --sim
  then open http://localhost:8080

What it does
  * starts Ryu + Mininet topology + the UDP server (h2) + UDP clients (h1, h3)
  * live charts: RTT, delivered throughput, per-link utilisation, loss/retx counters
  * buttons to break / restore links, run scenarios, switch SDN dynamic <-> static
    baseline, switch application recovery on/off
  * measures every failure (detect / reroute / recovery time) and can run an automatic
    baseline-vs-adaptive benchmark
Pure standard library - no pip packages needed.
"""
import argparse
import csv
import heapq
import json
import os
import random
import re
import select
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'src')
PY = sys.executable

ISL = ['s1-s2', 's2-s4', 's1-s3', 's3-s5', 's4-s5']        # canonical (sorted) names
SCENARIOS = {       # name -> (duration s, [(t, action, link)])
    'single': (26, [(6, 'down', 's2-s4'), (16, 'up', 's2-s4')]),
    'double': (32, [(6, 'down', 's2-s4'), (12, 'down', 's3-s5'),
                    (18, 'up', 's3-s5'), (24, 'up', 's2-s4')]),
    'flap': (26, [(6, 'down', 's2-s4'), (10, 'up', 's2-s4'),
                  (14, 'down', 's2-s4'), (18, 'up', 's2-s4')]),
}
CONFIGS = [('static', False), ('static', True), ('dynamic', False), ('dynamic', True)]


def canon(link):
    a, b = re.split(r'[-:]', link)
    return '-'.join(sorted([a, b]))


def hops(path):
    return [canon('%s-%s' % (x, y)) for x, y in zip(path, path[1:])] if path else []


# ===================================================================== backends
class SimBackend:
    """Software emulation of the 5-switch topology + the controller logic.
    Real client/server processes run over localhost through a UDP proxy that applies
    link state, path delay and the (emulated) controller's routing table."""
    name = 'simulation'
    server_ip, server_port, proxy_port = '127.0.0.1', 5000, 6000
    LAT = {'s1-s2': 2, 's2-s4': 2, 's1-s3': 2, 's3-s5': 10, 's4-s5': 2}
    PORT = {('s1', 's2'): 3, ('s1', 's3'): 4, ('s2', 's1'): 1, ('s2', 's4'): 2, ('s3', 's1'): 1,
            ('s3', 's5'): 2, ('s4', 's2'): 2, ('s4', 's5'): 3, ('s5', 's3'): 1, ('s5', 's4'): 2}
    HOSTSW = {'10.0.0.1': ('s1', 1), '10.0.0.3': ('s1', 2), '10.0.0.2': ('s4', 1)}

    def __init__(self, mode, outdir):
        self.mode, self.outdir = mode, outdir
        self.lk = threading.RLock()
        self.phys = {l: True for l in ISL}
        self.ctl = set(ISL)
        self.ev, self._evn = [], 0
        self.bytes = {l: 0 for l in ISL}
        self.running = True
        self.route = self._bfs_path(self.ctl)

    def _emit(self, name, detail=''):
        self.ev.append((time.time(), name, detail))

    # -- emulated controller -------------------------------------------------
    def _nexthops(self, links, dst):
        adj = {}
        for l in links:
            a, b = l.split('-')
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        dist, q = {dst: 0}, [dst]
        for u in q:
            for v in sorted(adj.get(u, ())):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    q.append(v)
        return {u: sorted(v for v in adj[u] if dist.get(v, 99) == dist[u] - 1)[0]
                for u in dist if u != dst}

    def _bfs_path(self, links, src='s1', dst='s4'):
        nxt, p = self._nexthops(links, dst), [src]
        while p[-1] != dst:
            if p[-1] not in nxt:
                return None
            p.append(nxt[p[-1]])
        return p

    def _rebuild(self, reason):
        t0 = time.time()
        self.route = self._bfs_path(self.ctl)
        p = '>'.join(self.route) if self.route else 'UNREACHABLE'
        self._emit('routes_updated', 'reason=%s flows=%d took_ms=%.2f paths=[10.0.0.1->10.0.0.2:%s]'
                   % (reason, 15, (time.time() - t0) * 1000 + 0.3, p))

    def _detect_down(self, link):
        with self.lk:
            if self.phys[link] or link not in self.ctl or self.mode != 'dynamic':
                return
            self.ctl.discard(link)
            self._emit('link_down_detected', '%s via port_status' % link)
            self._rebuild('link_down')

    def _link_up_ctl(self, link):          # LLDP rediscovery is slower than port events
        with self.lk:
            if not self.phys[link]:
                return
            self.ctl.add(link)
            self._emit('link_up', link)
            if self.mode == 'dynamic':
                self._rebuild('link_up')

    # -- backend API -----------------------------------------------------------
    def start(self):
        threading.Thread(target=self._proxy, daemon=True).start()
        time.sleep(0.3)

    def stop(self):
        self.running = False

    def set_link(self, link, up):
        link = canon(link)
        with self.lk:
            self.phys[link] = up
            a = link.split('-')[0]
            self._emit('port_status', '%s port=%d down=%s' % (a, self.PORT[(a, link.split('-')[1])], not up))
            if not up:
                if self.mode == 'dynamic':
                    threading.Timer(0.02 + random.random() * 0.02, self._detect_down, [link]).start()
                else:
                    self._emit('link_down_ignored_static', '%s via port_status' % link)
            elif self.mode == 'dynamic':
                threading.Timer(1.2, self._link_up_ctl, [link]).start()
            else:
                self.ctl.add(link)

    def set_mode(self, mode):
        with self.lk:
            if mode == self.mode:
                return
            self.mode = mode
            self._emit('mode_change', 'mode=%s' % mode)
            if mode == 'dynamic':
                self.ctl = {l for l in ISL if self.phys[l]}
                self._rebuild('mode_change')

    def app_event(self, text):
        with self.lk:
            self._emit('app_event', text)
            if self.mode == 'dynamic':
                gone = [l for l in self.ctl if not self.phys[l]]
                for l in gone:
                    self.ctl.discard(l)
                    self._emit('link_down_detected', '%s via port_audit' % l)
                if gone:
                    self._rebuild('link_down')

    def poll(self):
        with self.lk:
            evs, self._evn = self.ev[self._evn:], len(self.ev)
            r = self.route
            return evs, {'mode': self.mode, 'ctl_links': sorted(self.ctl),
                         'paths': {'1': r, '2': r}, 'link_bytes': dict(self.bytes),
                         'bytes_ts': time.time()}

    def flows(self, sw):
        with self.lk:
            out = ['(simulated flow table - run without --sim on Mininet for real OVS output)']
            for ip, (hs, hp) in self.HOSTSW.items():
                nxt = self._nexthops(self.ctl, hs)
                if sw == hs:
                    port = hp
                elif sw in nxt:
                    port = self.PORT[(sw, nxt[sw])]
                else:
                    out.append(' priority=10,ip,nw_dst=%s actions=drop' % ip)
                    continue
                out.append(' priority=10,ip,nw_dst=%s actions=output:%d' % (ip, port))
            out.append(' priority=0 actions=CONTROLLER:65535')
            return '\n'.join(out)

    def spawn(self, name, argv):
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)

    # -- UDP proxy (the emulated data plane) ----------------------------------
    def _oneway(self, route):
        ms = 2.0 + sum(self.LAT[h] for h in hops(route)) + random.random() * 1.0
        return ms / 1000.0

    def _usable(self, route):
        return route is not None and all(self.phys[h] for h in hops(route))

    def _proxy(self):
        p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        p.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        p.bind(('127.0.0.1', self.proxy_port))
        q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        clients, heap, n = {}, [], 0
        while self.running:
            r, _, _ = select.select([p, q], [], [], 0.003)
            for s in r:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    continue
                with self.lk:
                    route = self.route
                    if not self._usable(route):
                        continue                   # black hole
                    for h in hops(route):
                        self.bytes[h] += len(data) + 42
                    d = self._oneway(route)
                if s is p:
                    clients[data[1]] = addr
                    dest, sock = ('127.0.0.1', self.server_port), q
                else:
                    dest, sock = clients.get(data[1]), p
                    if dest is None:
                        continue
                n += 1
                heapq.heappush(heap, (time.time() + d, n, sock, dest, data))
            now = time.time()
            while heap and heap[0][0] <= now:
                _, _, sock, dest, data = heapq.heappop(heap)
                try:
                    sock.sendto(data, dest)
                except OSError:
                    pass


class MininetBackend:
    name = 'mininet'
    server_ip, server_port = '10.0.0.2', 5000
    HOST = {'server': 'h2', 1: 'h1', 2: 'h3'}
    CLIENT_IP = {'1': '10.0.0.1', '2': '10.0.0.3'}

    def __init__(self, mode, outdir, ryu='ryu-manager', warmup=6.0):
        self.mode, self.outdir, self.ryu, self.warmup = mode, outdir, ryu, warmup
        self.net = self.ctl = None
        self.ctrl = {'mode': mode, 'app_seq': 0, 'app_event': ''}
        self.cfile = os.path.join(outdir, 'control.json')
        self.sfile = os.path.join(outdir, 'state.json')
        self.efile = os.path.join(outdir, 'controller_events.csv')
        self._evn = 0
        self._prev = None
        self.lk = threading.RLock()

    def _write_ctrl(self):
        tmp = self.cfile + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.ctrl, f)
        os.replace(tmp, self.cfile)

    def start(self):
        sys.path.insert(0, os.path.join(ROOT, 'topology'))
        from mininet.log import setLogLevel
        from topo import build_net
        setLogLevel('warning')
        os.makedirs(self.outdir, exist_ok=True)
        self._write_ctrl()
        env = dict(os.environ, ROUTING_MODE=self.mode, RESULTS_DIR=self.outdir,
                   CONTROL_FILE=self.cfile, STATE_FILE=self.sfile)
        self.ctl = subprocess.Popen(
            [self.ryu, '--observe-links', os.path.join(ROOT, 'controller', 'adaptive_router.py')],
            env=env, stdout=open(os.path.join(self.outdir, 'ryu.log'), 'w'), stderr=subprocess.STDOUT)
        end = time.time() + 25
        while time.time() < end:
            try:
                socket.create_connection(('127.0.0.1', 6653), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError('Ryu did not start, see %s/ryu.log' % self.outdir)
        self.net = build_net()
        self.net.start()
        self.net.waitConnected(timeout=15)
        time.sleep(self.warmup)                       # LLDP discovery
        hs = self.net.get('h1', 'h2', 'h3')
        if self.net.ping(list(hs), timeout='2') > 0:  # teach the controller the hosts
            self.net.ping(list(hs), timeout='2')

    def stop(self):
        if self.net:
            self.net.stop()
        if self.ctl:
            self.ctl.terminate()
            try:
                self.ctl.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ctl.kill()

    def set_link(self, link, up):
        a, b = canon(link).split('-')
        with self.lk:
            self.net.configLinkStatus(a, b, 'up' if up else 'down')

    def set_mode(self, mode):
        self.mode = self.ctrl['mode'] = mode
        self._write_ctrl()

    def app_event(self, text):
        self.ctrl['app_seq'] += 1
        self.ctrl['app_event'] = text
        self._write_ctrl()

    def poll(self):
        evs = []
        try:
            with open(self.efile, newline='') as f:
                rows = list(csv.reader(f))[1:]
            evs = [(float(r[0]), r[1], r[2]) for r in rows[self._evn:] if len(r) >= 3]
            self._evn = len(rows)
        except (OSError, ValueError):
            pass
        st = {'mode': self.mode, 'ctl_links': [], 'paths': {}, 'link_bytes': {}, 'bytes_ts': 0}
        try:
            with open(self.sfile) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return evs, st
        st['mode'] = s.get('mode', self.mode)
        st['ctl_links'] = sorted({canon('s%d-s%d' % (a, b)) for a, _, b, _ in s.get('links', [])})
        for cid, ip in self.CLIENT_IP.items():
            p = s.get('paths', {}).get('%s->10.0.0.2' % ip)
            st['paths'][cid] = ['s%d' % x for x in p] if p else None
        lb = {}
        for a, pa, b, pb in s.get('links', []):          # both ends of the same link
            c = s['ports'].get('%d:%d' % (a, pa))
            if c:
                lb[canon('s%d-s%d' % (a, b))] = c[0] + c[1]
        st['link_bytes'], st['bytes_ts'] = lb, s.get('ports_ts', 0)
        return evs, st

    def flows(self, sw):
        try:
            out = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', sw],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception as e:
            return 'ovs-ofctl failed: %s' % e
        out = re.sub(r'cookie=\S+ duration=\S+ table=\d+, n_packets=(\d+), n_bytes=(\d+), ',
                     r'pkts=\1 bytes=\2 ', out)
        return re.sub(r'(idle_age|hard_age)=\d+,? ?', '', out).strip()

    def spawn(self, name, argv):
        node = self.net.get(self.HOST[name])
        return node.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, bufsize=1)


# ======================================================================== hub
class Hub:
    def __init__(self, backend, outdir):
        self.b, self.outdir = backend, outdir
        self.lock = threading.RLock()
        self.mode, self.recovery, self.rate, self.nclients = backend.mode, True, 50, 2
        self.samples = {1: deque(maxlen=15000), 2: deque(maxlen=15000)}
        self.cstat, self.srv, self.finals = {}, {}, {}
        self.events, self.incidents, self.bench = [], [], {'running': False, 'rows': [], 'msg': ''}
        self.links_up = {l: True for l in ISL}
        self.net = {'ctl_links': list(ISL), 'paths': {}}
        self.rates = deque(maxlen=1200)
        self._prevb = None
        self.procs, self.run_id = {}, 0
        self.scn = {'running': False, 'name': '', 't0': 0, 'msg': ''}
        self.abort = threading.Event()
        self.t_start = time.time()
        os.makedirs(outdir, exist_ok=True)

    # ---- events / logging
    def log(self, src, ev, detail='', ts=None):
        with self.lock:
            self.events.append({'i': len(self.events), 'ts': ts or time.time(), 'src': src,
                                'ev': ev, 'detail': detail})

    # ---- processes
    def _reader(self, name, proc, rid):
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith('{'):
                if line and ('Traceback' in line or 'Error' in line):
                    self.log('sys', '%s' % name, line[:160])
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                if rid != self.run_id and name != 'server':
                    continue
                k = m.get('k')
                if k == 's':
                    cid = m['cid']
                    self.cstat[cid] = m
                    self.samples.setdefault(cid, deque(maxlen=15000)).extend(m['acks'])
                elif k == 'e':
                    ev = {'outage_detected': 'application detected outage -> RECOVERING (probing)',
                          'recovery_complete': 'probe answered -> resending backlog (%.2fs outage)'
                          % m.get('dur', 0)}[m['ev']]
                    self.log('app', 'client%d' % m['cid'], ev, m['t'])
                    if m['ev'] == 'outage_detected':
                        self.b.app_event('client%d outage_detected' % m['cid'])
                elif k == 'final':
                    self.finals[m['cid']] = m
                elif k == 'srv':
                    self.srv = m

    def start_server(self):
        p = self.b.spawn('server', [PY, os.path.join(SRC, 'server.py'), '--bind', '0.0.0.0',
                                    '--port', str(self.b.server_port), '--live', '--log',
                                    os.path.join(self.outdir, 'server_log.csv')])
        self.procs['server'] = p
        threading.Thread(target=self._reader, args=('server', p, 0), daemon=True).start()
        time.sleep(0.7)

    def traffic_running(self):
        return any(p.poll() is None for k, p in self.procs.items() if k != 'server')

    def stop_traffic(self):
        for k, p in list(self.procs.items()):
            if k != 'server' and p.poll() is None:
                p.terminate()
        for k, p in list(self.procs.items()):
            if k != 'server':
                try:
                    p.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    p.kill()

    def start_traffic(self, duration=0, rate=None, recovery=None, nclients=None):
        self.stop_traffic()
        with self.lock:
            self.rate = rate or self.rate
            self.recovery = self.recovery if recovery is None else recovery
            self.nclients = nclients or self.nclients
            self.run_id += 1
            rid = self.run_id
            self.finals, self.cstat = {}, {}
            self.samples = {1: deque(maxlen=15000), 2: deque(maxlen=15000)}
        for cid in range(1, self.nclients + 1):
            argv = [PY, os.path.join(SRC, 'client.py'), '--server', self.b.server_ip,
                    '--port', str(getattr(self.b, 'proxy_port', self.b.server_port)),
                    '--id', str(cid), '--rate', str(self.rate), '--duration', str(duration),
                    '--recovery', 'on' if self.recovery else 'off', '--live', '--drain', '4',
                    '--log', os.path.join(self.outdir, 'client%d.csv' % cid)]
            p = self.b.spawn(cid, argv)
            self.procs[cid] = p
            threading.Thread(target=self._reader, args=('client%d' % cid, p, rid), daemon=True).start()
        self.log('user', 'traffic_start', '%d client(s), %g msg/s, app recovery %s%s' % (
            self.nclients, self.rate, 'ON' if self.recovery else 'OFF',
            '' if not duration else ', %gs' % duration))

    # ---- link / mode actions
    def apply_link(self, link, action, src='user'):
        link = canon(link)
        up = action == 'up'
        with self.lock:
            if self.links_up[link] == up:
                return
            self.links_up[link] = up
        t = time.time()
        self.b.set_link(link, up)
        self.log(src, 'link_%s' % action, link, t)
        if not up:
            path = self.net['paths'].get('1')
            with self.lock:
                self.incidents.append({'id': len(self.incidents) + 1, 't': t, 'link': link,
                                       'mode': self.mode, 'recovery': self.recovery,
                                       'path_before': path, 'path_after': None,
                                       'on_path': link in hops(path) if path else True,
                                       'detect_ms': None, 'reroute_ms': None,
                                       'recovery_ms': None, 'done': False})

    def set_mode(self, mode):
        self.mode = mode
        self.b.set_mode(mode)
        self.log('user', 'mode', 'controller -> %s' % ('SDN dynamic rerouting' if mode == 'dynamic'
                                                        else 'STATIC baseline (no rerouting)'))

    def restore_all(self):
        for l in ISL:
            self.apply_link(l, 'up')

    # ---- scenarios
    def run_steps(self, steps, label):
        t0 = time.time()
        for t, act, link in steps:
            while time.time() - t0 < t:
                if self.abort.wait(0.05):
                    return False
            self.apply_link(link, act, 'scenario')
            with self.lock:
                self.scn['msg'] = '%s: t=%.0fs %s %s' % (label, t, act, link)
        return True

    def run_scenario(self, name):
        if self.scn['running'] or self.bench['running']:
            return
        dur, steps = SCENARIOS[name]
        self.abort.clear()

        def job():
            self.scn.update(running=True, name=name, t0=time.time(), msg='starting')
            self.log('user', 'scenario', name)
            if not self.traffic_running():
                self.start_traffic(duration=dur)
            ok = self.run_steps(steps, name)
            if ok:
                time.sleep(3)
            self.scn.update(running=False, msg='finished' if ok else 'aborted')
        threading.Thread(target=job, daemon=True).start()

    def stop_scenario(self):
        self.abort.set()
        self.restore_all()

    # ---- metrics
    def metrics(self, t_fail, t_up, cid=1):
        """Per-failure metrics from live data (same definitions as analysis/analyze.py)."""
        with self.lock:
            s = list(self.samples.get(cid, []))
            ev = list(self.events)
        post = [a for a in s if a[1] > t_fail]
        rec = (min(a[0] for a in post) - t_fail) * 1000 if post else None
        det = rer = None
        for e in ev:
            if e['src'] != 'ctl' or e['ts'] < t_fail - 0.05:
                continue
            if e['ev'] == 'link_down_detected' and det is None:
                det = (e['ts'] - t_fail) * 1000
            if e['ev'] == 'routes_updated' and 'reason=link_down' in e['detail'] and rer is None:
                rer = (e['ts'] - t_fail) * 1000
        return rec, det, rer

    def update_incidents(self):
        with self.lock:
            for inc in self.incidents:
                if inc['done']:
                    continue
                rec, det, rer = self.metrics(inc['t'], None)
                inc['detect_ms'], inc['reroute_ms'] = det, rer
                if not inc['on_path']:
                    inc['recovery_ms'], inc['done'] = 0.0, True
                elif rec is not None:
                    inc['recovery_ms'], inc['done'] = rec, True
                if inc['done'] or time.time() - inc['t'] > 1.0:
                    inc['path_after'] = self.net['paths'].get('1')

    # ---- benchmark (baseline vs adaptive, all 4 configs)
    def run_benchmark(self, scen='single'):
        if self.scn['running'] or self.bench['running']:
            return
        self.abort.clear()
        dur, steps = SCENARIOS[scen]

        def job():
            self.bench.update(running=True, rows=[], msg='starting', scenario=scen)
            self.log('user', 'benchmark', 'scenario=%s, 4 configurations' % scen)
            for i, (mode, rec) in enumerate(CONFIGS):
                if self.abort.is_set():
                    break
                label = '%s + %s' % ('SDN reroute' if mode == 'dynamic' else 'static routing',
                                      'app recovery' if rec else 'plain UDP')
                self.bench['msg'] = 'run %d/4: %s' % (i + 1, label)
                self.stop_traffic()
                self.restore_all()
                self.set_mode(mode)
                self.recovery = rec
                time.sleep(3.0)                      # LLDP re-discovery / mode switch
                self.start_traffic(duration=dur, recovery=rec)
                t0 = time.time()
                if not self.run_steps(steps, 'bench'):
                    break
                end = t0 + dur + 12          # plain-UDP clients still drain for 4 s
                while time.time() < end and len(self.finals) < self.nclients:
                    time.sleep(0.2)
                self.bench['rows'].append(self._bench_row(label, mode, rec, t0, steps))
            self.restore_all()
            self.bench.update(running=False, msg='finished' if not self.abort.is_set() else 'aborted')
        threading.Thread(target=job, daemon=True).start()

    def _bench_row(self, label, mode, rec, t0, steps):
        with self.lock:      # use the ACTUAL injection times, not the nominal schedule
            ev = [e for e in self.events if e['src'] == 'scenario' and e['ts'] >= t0 - 0.1]
        t_fail = next(e['ts'] for e in ev if e['ev'] == 'link_down')
        t_up = next(e['ts'] for e in ev if e['ev'] == 'link_up')
        recov, det, rer = self.metrics(t_fail, t_up)
        with self.lock:
            s = list(self.samples[1])
            fin = list(self.finals.values())
        sent = sum(f['sent'] for f in fin)
        lost = sum(f['lost'] for f in fin)
        pre = [a[2] for a in s if a[1] < t_fail - 0.3]
        bk = [a[2] for a in s if t_fail + (recov or 0) / 1000 + 1 < a[1] < t_up - 0.5]
        e2e = sorted(a[3] for a in s if a[0] > t_fail)
        mean = lambda v: round(sum(v) / len(v), 1) if v else None
        return {'label': label, 'mode': mode, 'recovery': rec,
                'recovery_ms': None if recov is None else round(recov, 1),
                'detect_ms': None if det is None else round(det, 1),
                'reroute_ms': None if rer is None else round(rer, 1),
                'loss_pct': round(100.0 * lost / sent, 2) if sent else None,
                'retx': sum(f['retransmissions'] for f in fin),
                'rtt_pre': mean(pre), 'rtt_backup': mean(bk),
                'e2e_p95': round(e2e[int(0.95 * (len(e2e) - 1))], 1) if e2e else None}

    # ---- poller
    def poll_loop(self):
        while True:
            time.sleep(0.4)
            try:
                evs, st = self.b.poll()
            except Exception as e:
                self.log('sys', 'poll_error', str(e))
                continue
            for ts, name, detail in evs:
                self.log('ctl', name, detail, ts)
            with self.lock:
                self.net = st
                lb, bts = st.get('link_bytes') or {}, st.get('bytes_ts') or 0
                if lb and self._prevb and bts > self._prevb[0]:
                    dt = bts - self._prevb[0]
                    self.rates.append([bts, {l: round(max(0, lb.get(l, 0) - self._prevb[1].get(l, 0))
                                                      * 8 / dt / 1000, 1) for l in ISL}])
                if lb:
                    if not self._prevb or bts > self._prevb[0]:
                        self._prevb = (bts, lb)
            self.update_incidents()

    def snapshot(self, since, ev_since):
        now = time.time()
        since = max(since, now - 120)
        with self.lock:
            ctl = set(self.net.get('ctl_links', []))
            return {
                'now': now, 'backend': self.b.name, 'mode': self.mode, 'recovery': self.recovery,
                'rate': self.rate, 'nclients': self.nclients, 'traffic': self.traffic_running(),
                'ctl_mode': self.net.get('mode'),
                'links': {l: {'up': self.links_up[l], 'ctl': l in ctl} for l in ISL},
                'paths': self.net.get('paths', {}),
                'clients': {str(k): {x: v[x] for x in v if x != 'acks'} for k, v in self.cstat.items()},
                'server': self.srv,
                'samples': {str(c): [a for a in d if a[0] > since] for c, d in self.samples.items()},
                'rates': [r for r in self.rates if r[0] > since],
                'events': self.events[ev_since:], 'incidents': self.incidents[-12:],
                'scenario': self.scn, 'bench': self.bench}


def load_results():
    """results/summary_by_config.csv (written by analysis/analyze.py) -> nested dict."""
    path = os.path.join(ROOT, 'results', 'summary_by_config.csv')
    try:
        rows = list(csv.reader(open(path)))
    except OSError:
        return {}
    names, stats = rows[0], rows[1]
    out = {}
    for r in rows[3:]:
        for i in range(2, len(r)):
            if r[i] != '' and stats[i] == 'mean':
                out.setdefault(r[0], {}).setdefault(r[1], {})[names[i]] = float(r[i])
    return out


# ====================================================================== http
def make_handler(hub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200, ctype='application/json'):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ('/', '/index.html'):
                return self._send(open(os.path.join(HERE, 'index.html'), 'rb').read(), ctype='text/html')
            if u.path == '/api/state':
                return self._send(hub.snapshot(float(q.get('since', 0)), int(q.get('ev', 0))))
            if u.path == '/api/flows':
                return self._send({'sw': q.get('sw', 's1'), 'text': hub.b.flows(q.get('sw', 's1'))})
            if u.path == '/api/results':
                return self._send(load_results())
            if u.path == '/api/export':
                return self._send({'incidents': hub.incidents, 'benchmark': hub.bench,
                                   'events': hub.events})
            self._send({'error': 'not found'}, 404)

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            try:
                d = json.loads(self.rfile.read(n) or b'{}')
            except ValueError:
                d = {}
            p = urlparse(self.path).path
            try:
                if p == '/api/link':
                    if d.get('action') == 'toggle':
                        d['action'] = 'up' if not hub.links_up[canon(d['link'])] else 'down'
                    hub.apply_link(d['link'], d['action'])
                elif p == '/api/restore':
                    hub.restore_all()
                elif p == '/api/mode':
                    hub.set_mode(d['mode'])
                elif p == '/api/traffic':
                    if d.get('action') == 'stop':
                        hub.stop_traffic()
                        hub.log('user', 'traffic_stop')
                    else:
                        hub.start_traffic(0, float(d.get('rate', hub.rate)),
                                          bool(d.get('recovery', hub.recovery)),
                                          int(d.get('clients', hub.nclients)))
                elif p == '/api/scenario':
                    hub.run_scenario(d['name']) if d['name'] != 'stop' else hub.stop_scenario()
                elif p == '/api/benchmark':
                    hub.run_benchmark(d.get('scenario', 'single'))
                else:
                    return self._send({'error': 'not found'}, 404)
                self._send({'ok': True})
            except Exception as e:
                self._send({'ok': False, 'error': str(e)}, 400)
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sim', action='store_true', help='emulated network (no Mininet/Ryu needed)')
    ap.add_argument('--mode', choices=['dynamic', 'static'], default='dynamic')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--ryu', default='ryu-manager')
    ap.add_argument('--warmup', type=float, default=6.0)
    ap.add_argument('--no-traffic', action='store_true', help='do not auto-start the UDP clients')
    ap.add_argument('--out', default=os.path.join(ROOT, 'results', 'dashboard'))
    a = ap.parse_args()

    if not a.sim and hasattr(os, 'geteuid') and os.geteuid() != 0:
        sys.exit('Mininet needs root: sudo env "PATH=$PATH" python3 dashboard/app.py   (or use --sim)')
    out = os.path.abspath(a.out)
    backend = SimBackend(a.mode, out) if a.sim else MininetBackend(a.mode, out, a.ryu, a.warmup)
    hub = Hub(backend, out)
    print('[dashboard] starting %s backend ...' % backend.name, flush=True)
    backend.start()
    hub.start_server()
    threading.Thread(target=hub.poll_loop, daemon=True).start()
    if not a.no_traffic:
        hub.start_traffic(0)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(hub))
    print('[dashboard] ready -> http://localhost:%d' % a.port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print('\n[dashboard] shutting down ...', flush=True)
        hub.abort.set()
        hub.stop_traffic()
        if hub.procs.get('server'):
            hub.procs['server'].terminate()
        backend.stop()


if __name__ == '__main__':
    main()
