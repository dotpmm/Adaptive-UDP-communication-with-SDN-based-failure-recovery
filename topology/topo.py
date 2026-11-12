#!/usr/bin/env python3
"""
Mininet topology for the adaptive UDP / SDN project.

Hosts
    h1 10.0.0.1  UDP client
    h2 10.0.0.2  UDP server
    h3 10.0.0.3  second UDP client

Switches s1-s5 (OpenFlow 1.3, controlled by a remote Ryu controller)

Primary path : h1 - s1 - s2 - s4 - h2
Backup path  : h1 - s1 - s3 - s5 - s4 - h2

Usage (start the Ryu controller first):

    sudo python3 topology/topo.py --cli      # interactive Mininet prompt
    sudo python3 topology/topo.py            # pingall, then exit

experiments/run_experiment.py imports build_network() from this file.
"""

import argparse
import os
import sys

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 6653

# The link the experiments fail to force traffic onto the backup path.
PRIMARY_LINK = ("s2", "s4")


def build_network(controller_ip=CONTROLLER_HOST,
                  controller_port=CONTROLLER_PORT):
    """Create (but do not start) the Mininet network.

    Mininet is imported here so this module can be imported, and --help
    run, on a machine without Mininet installed.
    """
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
        ip=controller_ip,
        port=controller_port,
    )

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")

    switches = {
        name: net.addSwitch(name, protocols="OpenFlow13")
        for name in ("s1", "s2", "s3", "s4", "s5")
    }
    s1, s2, s3, s4, s5 = (
        switches[name] for name in ("s1", "s2", "s3", "s4", "s5")
    )

    # Links are added in this order on purpose: it gives the port numbers
    # seen in the D1 screenshots (s1-eth1 = h1, s1-eth2 = h3,
    # s1-eth3 = towards s2, s1-eth4 = towards s3). All links are 10 Mbit/s.
    net.addLink(h1, s1, bw=10, delay="1ms")
    net.addLink(h2, s4, bw=10, delay="1ms")
    net.addLink(h3, s1, bw=10, delay="1ms")

    # Primary path: s1 - s2 - s4 (RTT about 12 ms end to end)
    net.addLink(s1, s2, bw=10, delay="2ms")
    net.addLink(s1, s3, bw=10, delay="2ms")
    net.addLink(s2, s4, bw=10, delay="2ms")

    # Backup path: s1 - s3 - s5 - s4. The s3-s5 link is deliberately slow
    # (10 ms) so the latency change after a failover is visible
    # (RTT about 32 ms end to end).
    net.addLink(s3, s5, bw=10, delay="10ms")
    net.addLink(s5, s4, bw=10, delay="2ms")

    return net


def main():
    parser = argparse.ArgumentParser(
        description="Start the adaptive UDP / SDN Mininet topology"
    )
    parser.add_argument("--cli", action="store_true",
                        help="open the Mininet CLI instead of just pingall")
    parser.add_argument("--controller-ip", default=CONTROLLER_HOST)
    parser.add_argument("--controller-port", type=int,
                        default=CONTROLLER_PORT)
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: sudo python3 topology/topo.py --cli")

    from mininet.cli import CLI
    from mininet.log import setLogLevel

    setLogLevel("info")

    net = build_network(args.controller_ip, args.controller_port)
    net.start()
    try:
        if args.cli:
            CLI(net)
        else:
            # Allow LLDP discovery to finish before testing.
            import time
            time.sleep(5)
            net.pingAll()
    finally:
        net.stop()


if __name__ == "__main__":
    main()
