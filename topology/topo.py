#!/usr/bin/env python3
"""Mininet topology with two disjoint paths between the client side and the server.

                 +------ s2 ------+              primary  (short, fast)
   h1 --+        |                |
        s1 ------+                +------ s4 -- h2 (UDP server)
   h3 --+        |                |
                 +-- s3 ==== s5 --+              backup   (longer, slower)

 h1, h3 : UDP clients       h2 : UDP server
 primary path  s1-s2-s4        (2 ms + 2 ms links)
 backup  path  s1-s3-s5-s4     (s3-s5 has 10 ms delay -> higher RTT after failover)

Usage:
   sudo python3 topology/topo.py --cli     # manual demo (controller must be running)
"""
import argparse

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo


class AdaptiveTopo(Topo):
    def build(self):
        sw = {n: self.addSwitch(n, protocols='OpenFlow13')
              for n in ('s1', 's2', 's3', 's4', 's5')}
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
        # access links
        self.addLink(h1, sw['s1'], bw=10, delay='1ms')
        self.addLink(h3, sw['s1'], bw=10, delay='1ms')
        self.addLink(h2, sw['s4'], bw=10, delay='1ms')
        # primary path
        self.addLink(sw['s1'], sw['s2'], bw=10, delay='2ms')
        self.addLink(sw['s2'], sw['s4'], bw=10, delay='2ms')
        # backup path
        self.addLink(sw['s1'], sw['s3'], bw=10, delay='2ms')
        self.addLink(sw['s3'], sw['s5'], bw=10, delay='10ms')
        self.addLink(sw['s5'], sw['s4'], bw=10, delay='2ms')


def build_net(ip='127.0.0.1', port=6653):
    net = Mininet(topo=AdaptiveTopo(), switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=False)
    net.addController('c0', controller=RemoteController, ip=ip, port=port)
    return net


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', action='store_true')
    ap.add_argument('--controller-ip', default='127.0.0.1')
    a = ap.parse_args()
    setLogLevel('info')
    net = build_net(a.controller_ip)
    net.start()
    if a.cli:
        CLI(net)
    net.stop()
