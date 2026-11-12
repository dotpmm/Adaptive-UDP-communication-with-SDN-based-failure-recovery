#!/usr/bin/env python3
"""UDP server: receives DATA / PROBE datagrams, answers with ACK / PROBE_ACK.

Concurrency model: one receiver thread (blocking recvfrom) feeds a queue that
is drained by N worker threads. Duplicate DATA (retransmissions) are detected
per (client_id, seq) and re-ACKed but flagged in the log.
Every received datagram is logged with its arrival time -> used to measure
service interruption on the server side.
"""
import argparse
import csv
import json
import queue
import signal
import socket
import threading
import time

from protocol import (MSG_ACK, MSG_DATA, MSG_PROBE, MSG_PROBE_ACK, pack, unpack)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=5000)
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--log', default='server_log.csv')
    ap.add_argument('--live', action='store_true', help='JSON stats lines on stdout')
    a = ap.parse_args()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((a.bind, a.port))
    sock.settimeout(0.5)

    q = queue.Queue()
    seen, seen_lock, rows = {}, threading.Lock(), []
    cnt = {'data': 0, 'dup': 0, 'probe': 0}
    per = {}

    def worker():
        while not (stop.is_set() and q.empty()):
            try:
                data, addr, ts = q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                t, cid, att, seq, sts = unpack(data)
            except ValueError:
                continue  # malformed datagram: ignore
            dup = 0
            if t == MSG_DATA:
                with seen_lock:
                    s = seen.setdefault(cid, set())
                    dup = int(seq in s)
                    s.add(seq)
                reply = pack(MSG_ACK, cid, att, seq, sts)
            elif t == MSG_PROBE:
                reply = pack(MSG_PROBE_ACK, cid, att, seq, sts)
            else:
                continue
            try:
                sock.sendto(reply, addr)
            except OSError:
                pass
            rows.append(('%.6f' % ts, cid, seq, att, t, dup))
            if t == MSG_DATA:
                cnt['data'] += 1
                cnt['dup'] += dup
                per[cid] = per.get(cid, 0) + 1
            else:
                cnt['probe'] += 1

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for w in workers:
        w.start()
    print('[server] listening on %s:%d' % (a.bind, a.port), flush=True)

    def live():
        while not stop.is_set():
            time.sleep(0.5)
            print(json.dumps({'k': 'srv', 't': time.time(), 'data': cnt['data'],
                              'dup': cnt['dup'], 'probe': cnt['probe'],
                              'per': dict(per)}), flush=True)
    if a.live:
        threading.Thread(target=live, daemon=True).start()

    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        q.put((data, addr, time.time()))

    for w in workers:
        w.join(timeout=2)
    with open(a.log, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['recv_ts', 'client_id', 'seq', 'attempt', 'msg_type', 'dup'])
        w.writerows(sorted(rows))
    print('[server] stopped, %d datagrams logged' % len(rows), flush=True)


if __name__ == '__main__':
    main()
