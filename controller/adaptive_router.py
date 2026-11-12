"""
Adaptive router - Ryu / OpenFlow 1.3 controller for the
"Adaptive Communication Network with Failure Recovery" project.

Run:   ROUTING_MODE=dynamic RESULTS_DIR=results/demo \
       ryu-manager --observe-links controller/adaptive_router.py

ROUTING_MODE
  dynamic (default): topology/link-state aware. On any link failure/recovery the
                     controller updates its graph and re-computes + re-installs
                     shortest-path flows for every known host.
  static           : BASELINE. Same initial shortest paths, but link-down events
                     are ignored (no rerouting) - traffic black-holes until the
                     link physically returns.

What it does
  * Topology discovery  : ryu.topology (LLDP) -> EventLinkAdd / EventLinkDelete
  * Fast failure detect : OFPPortStatus (LINK_DOWN) straight from the switches,
                          LLDP timeout is the fallback
  * ARP proxy           : controller answers ARP from its host table and floods
                          unknown requests ONLY on host-facing ports (the
                          topology has loops, so normal flooding would storm)
  * Routing             : destination-based IPv4 flows computed with BFS
                          (hop count) on the live link graph
  * Monitoring          : per-port counters polled every 1 s -> port_stats.csv
  * Event log           : timestamped events -> controller_events.csv
  * Dashboard hooks     : CONTROL_FILE (json, polled 4x/s) switches the mode at runtime
                          and delivers application events; STATE_FILE (json) exports
                          links / paths / port counters for the live dashboard.
                          An application "outage_detected" event triggers a port audit
                          (OFPMP_PORT_DESC) that reconciles any missed link-down.
"""
import csv
import json
import os
import time
from collections import deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import arp, ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_switch

ETH_IPV4 = ether_types.ETH_TYPE_IP
ROUTE_PRIO = 10
POLL_INTERVAL = 1.0


class AdaptiveRouter(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = os.environ.get('ROUTING_MODE', 'dynamic').lower()
        outdir = os.environ.get('RESULTS_DIR', '.')
        os.makedirs(outdir, exist_ok=True)
        self._evf = open(os.path.join(outdir, 'controller_events.csv'), 'w', newline='')
        self._evw = csv.writer(self._evf)
        self._evw.writerow(['ts', 'event', 'detail'])
        self._evf.flush()
        self._psf = open(os.path.join(outdir, 'port_stats.csv'), 'w', newline='')
        self._psw = csv.writer(self._psf)
        self._psw.writerow(['ts', 'dpid', 'port', 'rx_pkts', 'tx_pkts',
                            'rx_bytes', 'tx_bytes'])

        self.control_file = os.environ.get('CONTROL_FILE', os.path.join(outdir, 'control.json'))
        self.state_file = os.environ.get('STATE_FILE', os.path.join(outdir, 'state.json'))
        self._app_seq = 0
        self.port_counters = {}  # 'dpid:port' -> [rx_bytes, tx_bytes, rx_pkts, tx_pkts]
        self.ports_ts = 0.0
        self.dps = {}            # dpid -> datapath
        self.links = {}          # (src_dpid, dst_dpid) -> (src_port, dst_port)
        self.isl_ports = set()   # (dpid, port) that ever belonged to an inter-switch link
        self.hosts = {}          # ip -> {mac, dpid, port}
        hub.spawn(self._monitor)
        hub.spawn(self._control_loop)
        self._log('controller_start', 'mode=%s' % self.mode)

    # ------------------------------------------------------------------ utils
    def _log(self, event_name, detail=''):
        self._evw.writerow(['%.6f' % time.time(), event_name, detail])
        self._evf.flush()
        self.logger.info('%s %s', event_name, detail)

    def _add_flow(self, dp, prio, match, actions):
        p, o = dp.ofproto_parser, dp.ofproto
        inst = [p.OFPInstructionActions(o.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=prio, match=match,
                                 instructions=inst))   # no instructions == drop

    def _out(self, dp, port, data):
        if dp is None:
            return
        p, o = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=o.OFP_NO_BUFFER,
                                   in_port=o.OFPP_CONTROLLER,
                                   actions=[p.OFPActionOutput(port)], data=data))

    # ------------------------------------------------------- switch handshake
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        p, o = dp.ofproto_parser, dp.ofproto
        self._add_flow(dp, 0, p.OFPMatch(),
                       [p.OFPActionOutput(o.OFPP_CONTROLLER, o.OFPCML_NO_BUFFER)])

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.dps[dp.id] = dp
            self._log('switch_up', 's%d' % dp.id)
            self.rebuild_routes('switch_up')
        elif dp.id in self.dps:
            del self.dps[dp.id]
            self._log('switch_down', 's%d' % dp.id)

    # ------------------------------------------------ topology / link tracking
    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        l = ev.link
        key = (l.src.dpid, l.dst.dpid)
        self.links[key] = (l.src.port_no, l.dst.port_no)
        self.isl_ports.add((l.src.dpid, l.src.port_no))
        self.isl_ports.add((l.dst.dpid, l.dst.port_no))
        self._log('link_up', 's%d:%d -> s%d:%d' % (l.src.dpid, l.src.port_no,
                                                    l.dst.dpid, l.dst.port_no))
        if self.mode == 'dynamic':
            self.rebuild_routes('link_up')

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        l = ev.link
        self._handle_link_down(l.src.dpid, l.src.port_no, 'lldp_event')

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp, o = msg.datapath, msg.datapath.ofproto
        down = bool(msg.desc.state & o.OFPPS_LINK_DOWN) or \
            bool(msg.desc.config & o.OFPPC_PORT_DOWN) or msg.reason == o.OFPPR_DELETE
        self._log('port_status', 's%d port=%d down=%s' % (dp.id, msg.desc.port_no, down))
        if down:
            self._handle_link_down(dp.id, msg.desc.port_no, 'port_status')

    def _handle_link_down(self, dpid, port, source):
        if self.mode == 'static':
            self._log('link_down_ignored_static', 's%d:%d via %s' % (dpid, port, source))
            return
        gone = [k for k, (sp, dpp) in self.links.items()
                if (k[0] == dpid and sp == port) or (k[1] == dpid and dpp == port)]
        if not gone:
            return          # already handled via the other detection path
        for k in gone:
            del self.links[k]
        self._log('link_down_detected', 's%d:%d via %s' % (dpid, port, source))
        self.rebuild_routes('link_down')

    # --------------------------------------------------------------- routing
    def _routes_to(self, dst):
        """BFS from dst over reversed links. Returns {dpid: (out_port, next_dpid)}."""
        dist, q = {dst: 0}, deque([dst])
        while q:
            u = q.popleft()
            for v in sorted(a for (a, b) in self.links if b == u):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    q.append(v)
        nxt = {}
        for u in dist:
            if u == dst:
                continue
            cands = sorted(b for (a, b) in self.links
                           if a == u and dist.get(b, 1 << 30) == dist[u] - 1)
            nxt[u] = (self.links[(u, cands[0])][0], cands[0])
        return nxt

    def _path(self, src, dst, nxt):
        path, u = [src], src
        while u != dst:
            if u not in nxt:
                return None
            u = nxt[u][1]
            path.append(u)
        return path

    def rebuild_routes(self, reason):
        t0 = time.time()
        n, paths = 0, []
        routes = {}
        for ip, h in self.hosts.items():
            nxt = routes[ip] = self._routes_to(h['dpid'])
            for dpid, dp in self.dps.items():
                p = dp.ofproto_parser
                match = p.OFPMatch(eth_type=ETH_IPV4, ipv4_dst=ip)
                out = h['port'] if dpid == h['dpid'] else (nxt[dpid][0] if dpid in nxt else None)
                acts = [p.OFPActionOutput(out)] if out is not None else []
                self._add_flow(dp, ROUTE_PRIO, match, acts)   # ADD overwrites same match
                n += 1
        for sip, s in self.hosts.items():
            for dip, d in self.hosts.items():
                if sip < dip:
                    pa = self._path(s['dpid'], d['dpid'], routes[dip])
                    paths.append('%s->%s:%s' % (sip, dip, '>'.join('s%d' % x for x in pa)
                                                if pa else 'UNREACHABLE'))
        self._log('routes_updated', 'reason=%s flows=%d took_ms=%.2f paths=[%s]' % (
            reason, n, (time.time() - t0) * 1000, ' '.join(paths)))
        self._export_state()

    # ------------------------------------------------------- ARP proxy / hosts
    def _learn(self, ip, mac, dpid, port):
        if not self.links or (dpid, port) in self.isl_ports:
            return
        h = self.hosts.get(ip)
        if h and h['mac'] == mac and h['dpid'] == dpid and h['port'] == port:
            return
        self.hosts[ip] = {'mac': mac, 'dpid': dpid, 'port': port}
        self._log('host_learned', '%s %s at s%d:%d' % (ip, mac, dpid, port))
        self.rebuild_routes('host_learned')

    def _flood_edges(self, src_dpid, in_port, data):
        for dpid, dp in self.dps.items():
            sw = get_switch(self, dpid)
            if not sw:
                continue
            for pt in sw[0].ports:
                if pt.port_no >= 0xffffff00 or (dpid, pt.port_no) in self.isl_ports:
                    continue
                if dpid == src_dpid and pt.port_no == in_port:
                    continue
                self._out(dp, pt.port_no, data)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp, in_port = msg.datapath, msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP,
                                            ether_types.ETH_TYPE_IPV6):
            return
        a = pkt.get_protocol(arp.arp)
        if a is None:
            return      # IPv4 is forwarded by proactive flows; nothing to do
        if not self.links or (dp.id, in_port) in self.isl_ports:
            return
        self._learn(a.src_ip, a.src_mac, dp.id, in_port)
        if a.opcode == arp.ARP_REQUEST:
            tgt = self.hosts.get(a.dst_ip)
            if tgt:
                e = ethernet.ethernet(dst=a.src_mac, src=tgt['mac'],
                                      ethertype=ether_types.ETH_TYPE_ARP)
                r = arp.arp(opcode=arp.ARP_REPLY, src_mac=tgt['mac'], src_ip=a.dst_ip,
                            dst_mac=a.src_mac, dst_ip=a.src_ip)
                out = packet.Packet()
                out.add_protocol(e)
                out.add_protocol(r)
                out.serialize()
                self._out(dp, in_port, out.data)
            else:
                self._flood_edges(dp.id, in_port, msg.data)
        elif a.opcode == arp.ARP_REPLY:
            dst = self.hosts.get(a.dst_ip)
            if dst:
                self._out(self.dps.get(dst['dpid']), dst['port'], msg.data)

    # ------------------------------------------------------------- monitoring
    def _monitor(self):
        while True:
            for dp in list(self.dps.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                    dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        ts = '%.6f' % time.time()
        self.ports_ts = time.time()
        for st in ev.msg.body:
            if st.port_no < 0xffffff00:
                self.port_counters['%d:%d' % (ev.msg.datapath.id, st.port_no)] = [
                    st.rx_bytes, st.tx_bytes, st.rx_packets, st.tx_packets]
                self._psw.writerow([ts, ev.msg.datapath.id, st.port_no, st.rx_packets,
                                    st.tx_packets, st.rx_bytes, st.tx_bytes])
        self._psf.flush()

    # ------------------------------------------- dashboard hooks (control/state)
    def _control_loop(self):
        while True:
            hub.sleep(0.25)
            try:
                with open(self.control_file) as f:
                    c = json.load(f)
            except (OSError, ValueError):
                c = {}
            m = str(c.get('mode', self.mode)).lower()
            if m in ('dynamic', 'static') and m != self.mode:
                self.mode = m
                self._log('mode_change', 'mode=%s' % m)
                if m == 'dynamic':
                    self._resync_ports('mode_change')   # catch failures missed while static
                    self.rebuild_routes('mode_change')
            seq = int(c.get('app_seq', 0))
            if seq > self._app_seq:     # event raised by the UDP application
                self._app_seq = seq
                self._log('app_event', str(c.get('app_event', '')))
                if self.mode == 'dynamic':
                    self._resync_ports('app_event')
            self._export_state()

    def _resync_ports(self, why):
        """Ask every switch for its port table; reply handler reconciles the link graph."""
        for dp in list(self.dps.values()):
            dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_reply(self, ev):
        dp, o = ev.msg.datapath, ev.msg.datapath.ofproto
        for p in ev.msg.body:
            down = bool(p.state & o.OFPPS_LINK_DOWN) or bool(p.config & o.OFPPC_PORT_DOWN)
            if down and (dp.id, p.port_no) in self.isl_ports:
                self._handle_link_down(dp.id, p.port_no, 'port_audit')

    def _export_state(self):
        try:
            paths = {}
            for dip, d in self.hosts.items():
                nxt = self._routes_to(d['dpid'])
                for sip, s in self.hosts.items():
                    if sip != dip:
                        paths['%s->%s' % (sip, dip)] = self._path(s['dpid'], d['dpid'], nxt)
            st = {'ts': time.time(), 'mode': self.mode, 'switches': sorted(self.dps),
                  'links': [[a, sp, b, dpt] for (a, b), (sp, dpt) in sorted(self.links.items())],
                  'hosts': {ip: [h['dpid'], h['port']] for ip, h in self.hosts.items()},
                  'paths': paths, 'ports': self.port_counters, 'ports_ts': self.ports_ts}
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(st, f)
            os.replace(tmp, self.state_file)
        except Exception as e:      # never let the dashboard export break routing
            self.logger.warning('state export failed: %s', e)
