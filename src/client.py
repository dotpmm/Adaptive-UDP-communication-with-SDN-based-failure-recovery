#!/usr/bin/env python3
"""UDP client with application-level failure recovery.

The application generates one message every 1/rate seconds.

NORMAL state
  - every DATA is tracked until its ACK arrives
  - per-packet RTO retransmission (isolated loss), at most --max-retries times
  - OUTAGE DETECTION: if packets are pending and no ACK has been seen for
    --outage-timeout seconds the client switches to RECOVERING
RECOVERING state
  - new messages are buffered in a backlog (no retransmission storm)
  - a small PROBE datagram is sent every --probe-interval seconds
  - the first PROBE_ACK means the network path works again -> all unacked and
    buffered messages are re-sent in order and the client returns to NORMAL
With --recovery off the client is a plain fire-and-forget UDP sender
(used as a baseline).
"""
import argparse
import csv
import json
import signal
import socket
import threading
import time

from protocol import MSG_ACK, MSG_DATA, MSG_PROBE, MSG_PROBE_ACK, pack, unpack


class Client:
    def __init__(self, a):
        self.a = a
        self.dst = (a.server, a.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.05)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.recs = {}        # seq -> record
        self.pending = {}     # seq -> record (sent, not yet ACKed)
        self.backlog = []     # seqs generated during an outage, not yet sent
        self.state = 'NORMAL'
        self.last_ack = time.time()
        self.last_probe = 0.0
        self.outage_start = None
        self.recoveries = []
        self.probes_sent = 0
        self.gave_up = 0
        self.retx = 0
        self.acked = 0
        self.ackbuf = []      # (recv_ts, send_ts, rtt_ms, e2e_ms) not yet reported (--live)
        self.halt = threading.Event()
        self._plock = threading.Lock()

    def emit(self, obj):
        """One JSON line on stdout for the dashboard (only with --live)."""
        if self.a.live:
            with self._plock:
                print(json.dumps(obj), flush=True)

    # ---- helpers (call with self.lock held) ----
    def _tx(self, seq, rec):
        now = time.time()
        rec['attempts'] += 1
        if rec['attempts'] > 1:
            self.retx += 1
        if rec['first_tx'] is None:
            rec['first_tx'] = now
        rec['last_tx'] = now
        try:
            self.sock.sendto(pack(MSG_DATA, self.a.id, rec['attempts'], seq, now,
                                  self.a.size), self.dst)
        except OSError:
            pass  # e.g. network unreachable; the timers will retry

    def _finish_recovery(self, now):
        self.recoveries.append({'start': self.outage_start, 'end': now,
                                'duration_s': now - self.outage_start})
        self.state = 'NORMAL'
        self.emit({'k': 'e', 'cid': self.a.id, 't': now, 'ev': 'recovery_complete',
                   'dur': now - self.outage_start})
        self.last_ack = now
        for s in self.backlog:
            self.pending[s] = self.recs[s]
        self.backlog = []
        for s in sorted(self.pending):
            self._tx(s, self.pending[s])

    # ---- threads ----
    def rx_loop(self):
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            now = time.time()
            try:
                t, cid, att, seq, ts = unpack(data)
            except ValueError:
                continue
            if cid != self.a.id:
                continue
            with self.lock:
                if t == MSG_ACK:
                    rec = self.recs.get(seq)
                    if rec is not None and rec['ack_ts'] is None:
                        rec['ack_ts'] = now
                        rec['rtt'] = now - ts
                        self.acked += 1
                        self.pending.pop(seq, None)
                        if self.a.live:
                            self.ackbuf.append((now, ts, rec['rtt'] * 1000,
                                                (now - rec['gen_ts']) * 1000))
                    self.last_ack = now
                elif t == MSG_PROBE_ACK:
                    self.last_ack = now
                    if self.state == 'RECOVERING':
                        self._finish_recovery(now)

    def timer_loop(self):
        a = self.a
        while not self.stop.is_set():
            time.sleep(0.01)
            if not a.recovery:
                continue
            now = time.time()
            with self.lock:
                if self.state == 'NORMAL':
                    if (self.pending and now - self.last_ack > a.outage_timeout and
                            now - min(r['first_tx'] for r in self.pending.values())
                            > a.outage_timeout):
                        self.state = 'RECOVERING'
                        self.outage_start = now
                        self.last_probe = 0.0
                        self.emit({'k': 'e', 'cid': a.id, 't': now, 'ev': 'outage_detected'})
                        continue
                    for seq, r in list(self.pending.items()):
                        if now - r['last_tx'] >= a.rto:
                            if r['attempts'] > a.max_retries:
                                self.pending.pop(seq)      # give up -> lost
                                self.gave_up += 1
                            else:
                                self._tx(seq, r)
                else:  # RECOVERING
                    if now - self.last_probe >= a.probe_interval:
                        self.last_probe = now
                        self.probes_sent += 1
                        try:
                            self.sock.sendto(pack(MSG_PROBE, a.id, 1, 0, now), self.dst)
                        except OSError:
                            pass

    def live_loop(self):
        """Every 200 ms print a status line (counters + the ACKs seen since last line)."""
        while not self.stop.is_set():
            time.sleep(0.2)
            now = time.time()
            with self.lock:
                acks, self.ackbuf = self.ackbuf, []
                if self.a.recovery:
                    lost = self.gave_up
                else:   # plain UDP never retries: unACKed for > 1 s counts as lost
                    lost = sum(1 for r in self.pending.values()
                               if r['first_tx'] and now - r['first_tx'] > 1.0)
                msg = {'k': 's', 'cid': self.a.id, 't': now, 'state': self.state,
                       'sent': len(self.recs), 'acked': self.acked, 'pending': len(self.pending),
                       'backlog': len(self.backlog), 'lost': lost,
                       'retx': self.retx,
                       'probes': self.probes_sent, 'recoveries': len(self.recoveries),
                       'acks': [[round(x, 4) if i < 2 else round(x, 2) for i, x in enumerate(t)]
                                for t in acks]}
            self.emit(msg)

    # ---- main ----
    def run(self):
        a = self.a
        signal.signal(signal.SIGTERM, lambda *_: self.halt.set())
        threads = [threading.Thread(target=self.rx_loop, daemon=True),
                   threading.Thread(target=self.timer_loop, daemon=True)]
        if a.live:
            threads.append(threading.Thread(target=self.live_loop, daemon=True))
        for t in threads:
            t.start()
        interval, t0 = 1.0 / a.rate, time.time()
        n = int(a.duration * a.rate) if a.duration > 0 else None   # duration 0 = until SIGTERM
        i = -1
        while not self.halt.is_set() and (n is None or i + 1 < n):
            i += 1
            delay = t0 + i * interval - time.time()
            if delay > 0:
                time.sleep(delay)
            seq = i + 1
            rec = {'gen_ts': time.time(), 'first_tx': None, 'last_tx': None,
                   'attempts': 0, 'ack_ts': None, 'rtt': None}
            with self.lock:
                self.recs[seq] = rec
                if self.state == 'RECOVERING':
                    self.backlog.append(seq)
                else:
                    self.pending[seq] = rec
                    self._tx(seq, rec)
        end = time.time() + (0 if self.halt.is_set() else a.drain)
        while time.time() < end:
            with self.lock:
                if not self.pending and not self.backlog:
                    break
            time.sleep(0.05)
        self.stop.set()
        for t in threads:
            t.join(timeout=1)
        self.report()

    def report(self):
        a = self.a
        rows, rtts, e2es, lost, retx = [], [], [], 0, 0
        for seq in sorted(self.recs):
            r = self.recs[seq]
            ok = r['ack_ts'] is not None
            rtt = r['rtt'] * 1000 if ok else ''
            e2e = (r['ack_ts'] - r['gen_ts']) * 1000 if ok else ''
            if ok:
                rtts.append(rtt)
                e2es.append(e2e)
            else:
                lost += 1
            retx += max(0, r['attempts'] - 1)
            f = lambda v: '' if v is None else '%.6f' % v
            rows.append([seq, f(r['gen_ts']), f(r['first_tx']), f(r['ack_ts']),
                         r['attempts'], rtt if rtt == '' else '%.3f' % rtt,
                         e2e if e2e == '' else '%.3f' % e2e, 'ok' if ok else 'lost'])
        with open(a.log, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['seq', 'gen_ts', 'first_tx', 'ack_ts', 'attempts',
                        'rtt_ms', 'e2e_ms', 'status'])
            w.writerows(rows)
        total = len(self.recs)
        summ = {'client_id': a.id, 'sent': total, 'acked': total - lost,
                'lost': lost, 'loss_pct': 100.0 * lost / max(1, total),
                'retransmissions': retx, 'probes_sent': self.probes_sent,
                'recoveries': self.recoveries,
                'avg_rtt_ms': sum(rtts) / len(rtts) if rtts else None,
                'max_e2e_ms': max(e2es) if e2es else None}
        with open(a.log.replace('.csv', '') + '.summary.json', 'w') as fh:
            json.dump(summ, fh, indent=2)
        print('[client %d] sent=%d acked=%d lost=%d (%.2f%%) retx=%d recoveries=%d'
              % (a.id, total, total - lost, lost, summ['loss_pct'], retx,
                 len(self.recoveries)), flush=True)
        self.emit(dict(summ, k='final', cid=a.id, t=time.time()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', required=True)
    ap.add_argument('--port', type=int, default=5000)
    ap.add_argument('--id', type=int, default=1)
    ap.add_argument('--rate', type=float, default=50, help='messages per second')
    ap.add_argument('--duration', type=float, default=30, help='seconds; 0 = run until SIGTERM')
    ap.add_argument('--size', type=int, default=64, help='payload bytes')
    ap.add_argument('--recovery', choices=['on', 'off'], default='on')
    ap.add_argument('--rto', type=float, default=0.15)
    ap.add_argument('--max-retries', type=int, default=5)
    ap.add_argument('--outage-timeout', type=float, default=0.4)
    ap.add_argument('--probe-interval', type=float, default=0.1)
    ap.add_argument('--drain', type=float, default=4.0)
    ap.add_argument('--log', default='client.csv')
    ap.add_argument('--live', action='store_true',
                    help='print JSON status lines on stdout (used by the dashboard)')
    a = ap.parse_args()
    a.recovery = (a.recovery == 'on')
    Client(a).run()


if __name__ == '__main__':
    main()
