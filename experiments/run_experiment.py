#!/usr/bin/env python3
"""
Run one Mininet + Ryu experiment and save its logs.

For one run this script:
  1. starts the Ryu controller (controller/adaptive_router.py),
  2. builds the 3-host / 5-switch topology in Mininet,
  3. runs the UDP server on h2 and the UDP client on h1,
  4. optionally takes the primary link (s2-s4) down and back up,
  5. writes client.csv, server.csv, client_summary.json,
     controller_events.csv and events.json into one results directory
     that analysis/analyze.py can read.

Needs root (Mininet) and Ryu. Run from anywhere:

    sudo -E python3 experiments/run_experiment.py --recovery on
    sudo -E python3 experiments/run_experiment.py --recovery off --no-failure

Use experiments/run_all.sh to run all four combinations.
"""

import argparse
import csv
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
CONTROLLER_APP = PROJECT_ROOT / "controller" / "adaptive_router.py"

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 6653
SERVER_IP = "10.0.0.2"
SERVER_PORT = 5000

PRIMARY_LINK = ("s2", "s4")


def real_user_home():
    """Home directory of the user who ran sudo (or the current user)."""
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def find_ryu_manager(explicit):
    """Locate ryu-manager: --ryu, $RYU_MANAGER, PATH, then ~/ryu-env."""
    candidates = [explicit, os.environ.get("RYU_MANAGER")]
    for candidate in candidates:
        if candidate:
            return str(candidate)

    on_path = shutil.which("ryu-manager")
    if on_path:
        return on_path

    default = real_user_home() / "ryu-env" / "bin" / "ryu-manager"
    if default.exists():
        return str(default)

    raise FileNotFoundError(
        "ryu-manager not found. Pass --ryu, set RYU_MANAGER, or create "
        "~/ryu-env as described in the README."
    )


def wait_for_port(host, port, timeout):
    """Wait until something accepts TCP connections on host:port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def build_network():
    """Create the Mininet network (imported lazily so --help works anywhere)."""
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        build=False,
    )

    net.addController(
        "c0",
        controller=RemoteController,
        ip=CONTROLLER_HOST,
        port=CONTROLLER_PORT,
    )

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")

    switches = {
        name: net.addSwitch(name, protocols="OpenFlow13")
        for name in ("s1", "s2", "s3", "s4", "s5")
    }
    s1, s2, s3, s4, s5 = (switches[n] for n in ("s1", "s2", "s3", "s4", "s5"))

    # Hosts
    net.addLink(h1, s1, bw=100, delay="1ms")
    net.addLink(h3, s1, bw=100, delay="1ms")
    net.addLink(h2, s4, bw=100, delay="1ms")

    # Primary path: h1 - s1 - s2 - s4 - h2
    net.addLink(s1, s2, bw=50, delay="5ms")
    net.addLink(s2, s4, bw=50, delay="5ms")

    # Backup path: h1 - s1 - s3 - s5 - s4 - h2
    net.addLink(s1, s3, bw=50, delay="5ms")
    net.addLink(s3, s5, bw=50, delay="5ms")
    net.addLink(s5, s4, bw=50, delay="5ms")

    return net


def stop_process(process, timeout=5):
    """Stop a subprocess politely (SIGINT), then forcefully."""
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_controller_events(path, run_start):
    """Controller CSV rows at or after run_start, as events.json entries."""
    events = []
    if not path.exists():
        return events

    with open(path, newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            try:
                timestamp = float(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if timestamp < run_start:
                continue
            events.append({
                "event": "controller_" + row.get("event", ""),
                "time_seconds": round(timestamp - run_start, 4),
                "details": row.get("details", ""),
            })
    return events


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run one Mininet + Ryu UDP failure-recovery experiment"
    )
    parser.add_argument("--duration", type=float, default=45.0,
                        help="client run time in seconds")
    parser.add_argument("--rate", type=float, default=50.0,
                        help="client send rate in messages/second")
    parser.add_argument("--recovery", choices=["on", "off"], default="on",
                        help="application-level recovery on the client")
    parser.add_argument("--failure-after", type=float, default=15.0,
                        help="seconds after client start to fail s2-s4")
    parser.add_argument("--failure-duration", type=float, default=8.0,
                        help="seconds the primary link stays down")
    parser.add_argument("--no-failure", action="store_true",
                        help="run without failing any link")
    parser.add_argument("--results-dir", default=str(PROJECT_ROOT / "results"),
                        help="parent directory for run folders")
    parser.add_argument("--name", default=None,
                        help="run folder name (default: generated)")
    parser.add_argument("--ryu", default=None, help="path to ryu-manager")
    args = parser.parse_args()

    if args.duration <= 0 or args.rate <= 0:
        parser.error("--duration and --rate must be positive")
    if not args.no_failure:
        if args.failure_after < 0 or args.failure_duration <= 0:
            parser.error("--failure-after must be >= 0 and "
                         "--failure-duration must be positive")
        if args.failure_after + args.failure_duration >= args.duration:
            parser.error("the failure must end before --duration; "
                         "increase --duration or shorten the failure")
    return args


def main():
    args = parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root. Run with: sudo -E python3 "
                 "experiments/run_experiment.py ...")

    ryu_manager = find_ryu_manager(args.ryu)

    failure_label = "nofailure" if args.no_failure else "failure"
    run_name = args.name or (
        f"{time.strftime('%Y%m%d_%H%M%S')}"
        f"_recovery-{args.recovery}_{failure_label}"
    )
    run_dir = Path(args.results_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Run directory: {run_dir}")

    # Clear leftovers from earlier crashed runs.
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

    events = []
    controller = None
    server = None
    client = None
    net = None
    run_start = time.time()

    try:
        # The controller writes controller_events.csv into its working
        # directory, so run it inside the results folder.
        controller_log = open(run_dir / "controller.log", "w")
        controller = subprocess.Popen(
            [ryu_manager, "--observe-links", str(CONTROLLER_APP)],
            cwd=str(run_dir),
            stdout=controller_log,
            stderr=subprocess.STDOUT,
        )

        if not wait_for_port(CONTROLLER_HOST, CONTROLLER_PORT, 20):
            raise RuntimeError(
                "Ryu did not start listening on "
                f"{CONTROLLER_HOST}:{CONTROLLER_PORT}; see controller.log"
            )

        net = build_network()
        net.start()

        # Give LLDP time to discover links, then let the controller learn
        # every host before traffic starts.
        time.sleep(5)
        loss = net.pingAll(timeout=2)
        if loss >= 100:
            time.sleep(3)
            loss = net.pingAll(timeout=2)
        print(f"Baseline pingall loss: {loss:.0f}%")
        if loss >= 100:
            raise RuntimeError("Hosts cannot reach each other; "
                               "check controller.log")

        h1, h2 = net.get("h1", "h2")

        server = h2.popen(
            [sys.executable, str(SRC_DIR / "server.py"),
             "--port", str(SERVER_PORT),
             "--log", str(run_dir / "server.csv")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)

        run_start = time.time()
        client_start = time.monotonic()
        client = h1.popen(
            [sys.executable, str(SRC_DIR / "client.py"),
             "--server", SERVER_IP,
             "--port", str(SERVER_PORT),
             "--duration", str(args.duration),
             "--rate", str(args.rate),
             "--recovery", args.recovery,
             "--client-id", "1",
             "--log", str(run_dir / "client.csv"),
             "--summary", str(run_dir / "client_summary.json")],
            stdout=open(run_dir / "client.out", "w"),
            stderr=subprocess.STDOUT,
        )
        events.append({"event": "client_started", "time_seconds": 0.0})
        print(f"Client started (recovery={args.recovery}, "
              f"duration={args.duration}s)")

        def elapsed():
            return round(time.monotonic() - client_start, 4)

        def sleep_until(offset):
            remaining = offset - (time.monotonic() - client_start)
            if remaining > 0:
                time.sleep(remaining)

        if not args.no_failure:
            sleep_until(args.failure_after)
            net.configLinkStatus(*PRIMARY_LINK, "down")
            events.append({"event": "primary_link_down",
                           "time_seconds": elapsed()})
            print(f"[{elapsed():6.2f}s] {PRIMARY_LINK[0]}-{PRIMARY_LINK[1]} down")

            sleep_until(args.failure_after + args.failure_duration)
            net.configLinkStatus(*PRIMARY_LINK, "up")
            events.append({"event": "primary_link_restored",
                           "time_seconds": elapsed()})
            print(f"[{elapsed():6.2f}s] {PRIMARY_LINK[0]}-{PRIMARY_LINK[1]} up")

        # The client exits on its own once --duration has passed.
        try:
            client.wait(timeout=max(10.0, args.duration))
        except subprocess.TimeoutExpired:
            print("Client did not exit in time; stopping it")
            stop_process(client)
        events.append({"event": "client_finished", "time_seconds": elapsed()})

    finally:
        # Server must get SIGINT so it flushes server.csv.
        stop_process(server)
        if client is not None and client.poll() is None:
            stop_process(client)
        if net is not None:
            net.stop()
        stop_process(controller)

        events.extend(
            read_controller_events(run_dir / "controller_events.csv",
                                   run_start)
        )
        events.sort(key=lambda item: item["time_seconds"])
        with open(run_dir / "events.json", "w", encoding="utf-8") as file:
            json.dump(events, file, indent=2)

        subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    print(f"Done. Results in {run_dir}")
    summary = run_dir / "client_summary.json"
    if summary.exists():
        print(summary.read_text())


if __name__ == "__main__":
    main()
