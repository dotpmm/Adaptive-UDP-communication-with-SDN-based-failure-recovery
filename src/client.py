
import argparse
import csv
import json
import os
import socket
import threading
import time
from collections import OrderedDict

from protocol import (
    ACK,
    DATA,
    PROBE,
    PROBE_ACK,
    encode_message,
    decode_message,
)

ACK_TIMEOUT = 0.15
OUTAGE_TIMEOUT = 0.4
PROBE_INTERVAL = 0.1
MAX_RETRIES = 5
SEND_INTERVAL = 0.02

STOP = threading.Event()


def main():
    parser = argparse.ArgumentParser(description="UDP reliability client")
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--rate", type=float, default=50.0)
    parser.add_argument("--recovery", choices=["on", "off"], default="on")
    parser.add_argument("--log", default="client.csv")
    parser.add_argument("--summary", default="/tmp/c.summary.json")
    args = parser.parse_args()

    if not 0 <= args.client_id <= 255:
        parser.error("--client-id must be between 0 and 255")
    if args.duration <= 0 or args.rate <= 0:
        parser.error("--duration and --rate must be positive")

    server_address = (args.server, args.port)
    recovery_enabled = args.recovery == "on"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(server_address)
    sock.settimeout(0.05)

    # seq -> {timestamp, attempt, last_sent, retries}
    outstanding = OrderedDict()
    pending = OrderedDict()
    # Re-entrant: send_data() takes the lock and is also called from code
    # that already holds it (retransmit loop, resume-after-outage).
    lock = threading.RLock()

    rows = []
    stats = {
        "sent": 0,
        "acknowledged": 0,
        "retransmissions": 0,
        "lost": 0,
        "probes": 0,
        "rtt_ms": [],
    }

    next_seq = 1
    recovering = False
    last_ack_time = time.monotonic()
    last_probe_time = 0.0
    start_time = time.monotonic()
    next_send_time = start_time
    end_time = start_time + args.duration

    def send_data(seq, record, retransmission=False):
        now = time.monotonic()
        timestamp_ns = record["timestamp_ns"]

        packet = encode_message(
            DATA,
            args.client_id,
            record["attempt"],
            seq,
            timestamp_ns,
        )

        sock.send(packet)
        record["last_sent"] = now

        if retransmission:
            stats["retransmissions"] += 1

        with lock:
            rows.append({
                "time": time.time(),
                "event": "RETRANSMIT" if retransmission else "SEND",
                "seq": seq,
                "attempt": record["attempt"],
                "rtt_ms": "",
            })

    def send_probe():
        timestamp_ns = time.monotonic_ns()
        packet = encode_message(
            PROBE, args.client_id, 1, 0, timestamp_ns
        )
        try:
            sock.send(packet)
            stats["probes"] += 1
        except OSError as exc:
            print(f"Probe send failed: {exc}")

    def receive_loop():
        nonlocal last_ack_time, recovering

        while not STOP.is_set():
            try:
                packet = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            message = decode_message(packet)
            if message is None:
                continue
            if message["client_id"] != args.client_id:
                continue

            now = time.monotonic()

            if message["type"] == PROBE_ACK:
                with lock:
                    last_ack_time = now
                    if recovering:
                        recovering = False
                        print("Server reachable; resuming transmission")

                        for seq, record in list(pending.items()):
                            pending.pop(seq)
                            outstanding[seq] = record
                            try:
                                send_data(seq, record)
                            except OSError as exc:
                                print(f"Resume send failed: {exc}")
                                pending[seq] = record
                                outstanding.pop(seq, None)
                                break

                continue

            if message["type"] != ACK:
                continue

            seq = message["seq"]

            with lock:
                record = outstanding.pop(seq, None)

                if record is None:
                    # Ignore duplicate or late ACKs.
                    continue

                rtt_ms = (
                    time.monotonic_ns() - message["timestamp_ns"]
                ) / 1_000_000

                stats["acknowledged"] += 1
                stats["rtt_ms"].append(rtt_ms)
                last_ack_time = now

                rows.append({
                    "time": time.time(),
                    "event": "ACK",
                    "seq": seq,
                    "attempt": message["attempt"],
                    "rtt_ms": round(rtt_ms, 3),
                })

    receiver = threading.Thread(target=receive_loop, daemon=True)
    receiver.start()

    print(
        f"Sending UDP to {args.server}:{args.port}, "
        f"duration={args.duration}s, recovery={args.recovery}"
    )

    try:
        while time.monotonic() < end_time:
            now = time.monotonic()

            # Detect an outage only when there are unacknowledged messages.
            with lock:
                if (
                    recovery_enabled
                    and outstanding
                    and now - last_ack_time >= OUTAGE_TIMEOUT
                    and not recovering
                ):
                    recovering = True
                    print("No ACK received; entering recovery mode")

            # Generate messages at the configured rate.
            if now >= next_send_time:
                seq = next_seq
                next_seq += 1

                record = {
                    "timestamp_ns": time.monotonic_ns(),
                    "attempt": 1,
                    "last_sent": 0.0,
                    "retries": 0,
                }

                # "sent" counts every message the application generated,
                # including ones buffered during an outage and delivered
                # later, so acknowledged can never exceed sent.
                stats["sent"] += 1

                if recovery_enabled and recovering:
                    pending[seq] = record
                else:
                    if recovery_enabled:
                        outstanding[seq] = record

                    try:
                        send_data(seq, record)
                    except OSError as exc:
                        print(f"Send failed for seq={seq}: {exc}")

                if not recovery_enabled:
                    # Fire-and-forget mode: no ACK tracking or retransmission.
                    pass

                next_send_time += 1.0 / args.rate

                # Avoid an unbounded catch-up burst if the process is delayed.
                if next_send_time < now:
                    next_send_time = now + 1.0 / args.rate

            if recovery_enabled:
                now = time.monotonic()

                if recovering:
                    if now - last_probe_time >= PROBE_INTERVAL:
                        send_probe()
                        last_probe_time = now
                else:
                    with lock:
                        for seq, record in list(outstanding.items()):
                            if now - record["last_sent"] < ACK_TIMEOUT:
                                continue

                            if record["retries"] >= MAX_RETRIES:
                                outstanding.pop(seq, None)
                                stats["lost"] += 1
                                rows.append({
                                    "time": time.time(),
                                    "event": "GIVE_UP",
                                    "seq": seq,
                                    "attempt": record["attempt"],
                                    "rtt_ms": "",
                                })
                                continue

                            record["retries"] += 1
                            record["attempt"] += 1
                            try:
                                send_data(seq, record, retransmission=True)
                            except OSError as exc:
                                print(f"Retransmission failed: {exc}")

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("Interrupted by user")

    finally:
        STOP.set()
        sock.close()
        receiver.join(timeout=1.0)

        # Count messages that remain outstanding or buffered at shutdown.
        stats["lost"] += len(outstanding) + len(pending)

        os.makedirs(
            os.path.dirname(os.path.abspath(args.log)),
            exist_ok=True,
        )
        with open(args.log, "w", newline="", encoding="utf-8") as file:
            columns = ["time", "event", "seq", "attempt", "rtt_ms"]
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

        rtts = stats["rtt_ms"]
        summary = {
            "server": args.server,
            "port": args.port,
            "client_id": args.client_id,
            "duration_seconds": round(time.monotonic() - start_time, 3),
            "recovery_enabled": recovery_enabled,
            "sent": stats["sent"],
            "acknowledged": stats["acknowledged"],
            "retransmissions": stats["retransmissions"],
            "lost": stats["lost"],
            "probes": stats["probes"],
            "average_rtt_ms": round(sum(rtts) / len(rtts), 3) if rtts else None,
            "max_rtt_ms": round(max(rtts), 3) if rtts else None,
        }

        os.makedirs(
            os.path.dirname(os.path.abspath(args.summary)),
            exist_ok=True,
        )
        with open(args.summary, "w", encoding="utf-8") as file:
            json.dump(summary, file, indent=2)

        print("\nClient summary:")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()