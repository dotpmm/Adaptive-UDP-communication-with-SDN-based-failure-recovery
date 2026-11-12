# Adaptive Communication Network with Failure Recovery
**SDN + UDP Socket Programming (Jackfruit Mini Project, UE25CS243A, Problem 30)**

A resilient UDP communication framework. An OpenFlow 1.3 (Ryu) controller monitors the
topology, **detects link failures within milliseconds, and reroutes application traffic over an
alternate path**, while the UDP application itself **detects the outage, probes the network and
resends lost data** (application-level recovery). Recovery time, packet loss and latency are
measured and compared against a static-routing baseline.

---
## 1. Objectives (mapped to the problem statement)

| Requirement | Where it is implemented |
|---|---|
| Implement UDP communication | `src/server.py`, `src/client.py`, `src/protocol.py` (raw `socket.SOCK_DGRAM`) |
| Create multiple network paths | `topology/topo.py` (primary s1-s2-s4, backup s1-s3-s5-s4) |
| Monitor topology and link status | `controller/adaptive_router.py` (LLDP link events, OFPPortStatus, port-stats polling) |
| Simulate link/path failures | `experiments/run_experiment.py --events ...` (`net.configLinkStatus`) |
| Detect failures dynamically | controller: PortStatus (fast) + LLDP timeout (fallback); client: ACK-silence detector |
| Reroute application traffic | controller recomputes shortest paths and re-installs flows for all hosts |
| Application-level recovery | client `NORMAL -> RECOVERING (probe) -> resend backlog` state machine |
| Measure recovery time, loss, latency | `analysis/analyze.py` |

---
## 2. Architecture

```
                         +---------------------------------------------+
                         |  Ryu controller (OpenFlow 1.3)              |
                         |  - topology graph (LLDP + PortStatus)       |
                         |  - ARP proxy / host table                   |
                         |  - BFS shortest-path flow computation       |
                         |  - port-stats monitor, event log (CSV)      |
                         +----------------------+----------------------+
                                                | OpenFlow
                              +----- s2 --------+--------+
        h1 (client 1) --+     |                          |
                        s1 ---+                          +--- s4 --- h2 (UDP server :5000)
        h3 (client 2) --+     |                          |
                              +----- s3 ==== s5 ---------+
                    primary: s1-s2-s4  (RTT ~12 ms)   backup: s1-s3-s5-s4 (RTT ~32 ms)
```

**Communication flow.** Client -> `DATA(seq)` -> server -> `ACK(seq)` -> client. Packets travel on
flows the controller installed (destination-IP match, output to the next hop of the shortest path).

**Failure flow (dynamic mode).**
1. `link s2-s4 down` -> OVS emits `OFPPortStatus(LINK_DOWN)`.
2. Controller removes the link from its graph (`link_down_detected`).
3. Controller recomputes BFS paths and overwrites the flows on all switches (`routes_updated`);
   h1->h2 now uses `s1>s3>s5>s4`.
4. Client: ACKs stopped -> after `outage_timeout` (0.4 s) it enters RECOVERING, buffers new messages
   and sends `PROBE` every 0.1 s. First `PROBE_ACK` -> resend all unACKed + buffered messages in order.

**Baseline (static mode).** Same initial shortest paths, but the controller *ignores* link-down
events, so traffic black-holes until the link comes back. 4 configurations are compared:

| Config | Controller | Client |
|---|---|---|
| `static-noapp`  | static | plain UDP (baseline) |
| `static-app`    | static | + app recovery |
| `dynamic-noapp` | **SDN reroute** | plain UDP |
| `dynamic-app`   | **SDN reroute** | **+ app recovery (full system)** |

### Socket <-> SDN integration
* The application's behaviour is *driven by* the network state (outage detection / probing / resend), and
  the controller's rules are *driven by* topology events caused by the experiment.
* The controller proxies ARP and learns host locations from the application hosts' own traffic,
  and its flows carry the UDP traffic end to end.
* Per-port counters polled by the controller (`port_stats.csv`) show the traffic shifting from the
  primary to the backup path.

### Application protocol (`src/protocol.py`)
16-byte header `!BBHId` = `type, client_id, attempt, seq, send_ts`, plus padding.
`DATA`->`ACK` (echoes `send_ts` so RTT is per attempt), `PROBE`->`PROBE_ACK` (liveness check).
Server: receiver thread + worker pool, duplicate detection per `(client_id, seq)`.

---
## 3. Setup (Ubuntu 20.04/22.04 VM, per the Mininet manual)
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install mininet openvswitch-switch python3-pip -y
sudo pip3 install ryu "eventlet==0.30.2" pandas matplotlib numpy
sudo mn --test pingall          # sanity check: 0% dropped
sudo mn -c                      # cleanup helper
```
If `ryu-manager` is not found under sudo, always run scripts with `sudo env "PATH=$PATH" ...`.
If Ryu fails to import on newer Python/setuptools: `sudo pip3 install "setuptools<68"`.

## 4. Running

### A. Quick check of the socket layer (no Mininet)
```bash
python3 tests/test_local_recovery.py        # expects PASS
```

### B. Manual demo
```bash
# terminal 1 - controller
ROUTING_MODE=dynamic RESULTS_DIR=results/demo ryu-manager --observe-links controller/adaptive_router.py
# terminal 2 - network
sudo python3 topology/topo.py --cli
mininet> pingall
mininet> h2 python3 src/server.py --log /tmp/s.csv &
mininet> h1 python3 src/client.py --server 10.0.0.2 --duration 40 --log /tmp/c.csv &
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1        # flows -> port towards s2
mininet> link s2 s4 down                                  # watch controller log: detection + reroute
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1        # flows now -> port towards s3
mininet> link s2 s4 up
```
(For `static` mode start the controller with `ROUTING_MODE=static` to show the black-hole.)

### C. One automated experiment
```bash
sudo env "PATH=$PATH" python3 experiments/run_experiment.py --mode dynamic --recovery on \
     --duration 30 --events "8:down:s2-s4,20:up:s2-s4" --out results/single/dynamic-app/run1
```
### D. Full matrix + analysis (~25 min for REPS=3)
```bash
sudo env "PATH=$PATH" REPS=3 ./experiments/run_all.sh
python3 analysis/analyze.py            # -> results/summary_*.csv, comparison.png, timeline_*.png
```

### E. Live dashboard
One command starts Ryu + the Mininet topology + the UDP server/clients + a web UI. No extra pip packages.
```bash
sudo env "PATH=$PATH" python3 dashboard/app.py          # open http://localhost:8080  (add --mode static for the baseline)
python3 dashboard/app.py --sim                          # UI preview WITHOUT Mininet/Ryu (software-emulated network)
python3 tests/test_dashboard_sim.py                     # automated check of the dashboard backend (sim mode)
```
| UI element | What it shows / does |
|---|---|
| Topology | live link state (green up, red down, amber = *down but controller unaware*), animated active path, per-link kbit/s; **click a link to break / restore it** |
| KPI cards | active path, RTT, loss %, delivered, retransmissions, app state (NORMAL/RECOVERING), last detect / reroute / recovery time |
| Charts | RTT per message, delivered msgs/s, link utilisation (traffic shifting to the backup path), un-ACKed + lost messages; red bands = link down |
| Control panel | break/restore each of the 5 inter-switch links, restore all, **scenarios** (single failure, double failure, flapping), **controller mode** (SDN dynamic <-> static baseline, switched at runtime), **app recovery** on/off, rate, 1/2 clients |
| Incidents table | every failure measured live: detect, reroute and recovery time, path before => after |
| Event log | controller events (port_status, link_down_detected, routes_updated, ...), application events (outage_detected, recovery_complete), user actions |
| OpenFlow table | `ovs-ofctl dump-flows` per switch, refreshed live (flows change after a failure) |
| Benchmark | one click runs the single-failure scenario for all 4 configurations (static/dynamic x plain/app recovery) and fills a comparison table |
| Evaluation | bar charts of the saved experiment matrix (`results/summary_by_config.csv`) |

Suggested 5-minute demo: (1) show traffic on `s1>s2>s4`, flow table of s1; (2) **Break s2-s4** -> controller detect ~tens of ms, path flips to `s1>s3>s5>s4`, RTT steps up, flows change;
(3) restore; (4) switch to **Static baseline** + app recovery OFF, break again -> black hole, loss climbs, amber link (controller unaware);
(5) **Double failure** scenario -> no path, app enters RECOVERING and resends after restoration; (6) run the **benchmark** and show the table + saved-results charts.

How it plugs into the SDN control plane: the dashboard writes `control.json` (mode switch, application events) and reads `state.json` (links, paths, port counters)
exported by `controller/adaptive_router.py`. An application-level `outage_detected` event makes the controller audit all switch port tables (OFPMP_PORT_DESC)
and reconcile any missed link-down - application events therefore influence SDN behaviour (Socket-SDN integration rubric item).
`--sim` replaces Mininet/Ryu with a software model (same event names), so numbers from `--sim` must **not** be reported as results.

---
## 5. Test cases

| ID | Scenario | Schedule | Expected behaviour |
|---|---|---|---|
| T1 | Connectivity | none | pingall 0% loss; flows follow s1>s2>s4; RTT ~12 ms |
| T2 | Single link failure | s2-s4 down @8 s, up @20 s | dynamic: detect < ~50 ms, reroute via s3-s5, RTT ~32 ms; static: outage until 20 s |
| T3 | Challenge: double failure | s2-s4 down @8, s3-s5 down @14, up @20 / @26 | no path 14-20 s (drop rules, `UNREACHABLE` in log); app recovery resends after restoration |
| T4 | Challenge: link flapping | s2-s4 down/up/down/up | controller re-converges every time, no loops or flow storms |
| T5 | Multi-client | 2 clients (h1, h3) | both flows rerouted; per-client sequence spaces independent |
| T6 | App recovery only | `dynamic-noapp` vs `dynamic-app` | removes the residual loss during the detection window |
| T7 | Control run | no failure | 0% loss, shows measurement overhead is negligible |
| T8 | Socket layer | `tests/test_local_recovery.py` | ON: 0 lost, OFF: all blackout packets lost |
| T9 | Dashboard backend | `tests/test_dashboard_sim.py` | reroute, double fault (no path), static black-hole all verified through the HTTP API |

## 6. Metrics
`recovery_ms` (failure injection -> first datagram reaching the server afterwards), `detect_ms`,
`reroute_ms` (controller log), `max_outage_ms`, `loss_pct` (never ACKed), `first_try_loss_pct`,
`retransmissions`, `rtt_pre_ms` vs `rtt_backup_ms`, `e2e_p95_ms` / `e2e_max_ms` (generation -> ACK).
Sender and receiver run on the same VM clock, so one-way timestamps are comparable.

## 7. Results
Run section 4D and paste your numbers/plots here (`results/summary_by_config.csv`, `comparison.png`,
`timeline_*.png`). *Expected shape:* static baseline recovery ~ outage window (12 s);
dynamic reroute recovery well under 1 s; app recovery cuts residual loss to ~0 at the price of
higher worst-case latency for buffered messages; latency increases after failover (longer path).

## 8. Repository layout
```
src/          protocol.py server.py client.py          UDP application
controller/   adaptive_router.py                       Ryu app (dynamic + static baseline)
topology/     topo.py                                  Mininet topology (+ --cli)
experiments/  run_experiment.py run_all.sh             automation
analysis/     analyze.py                               tables + plots
tests/        test_local_recovery.py test_dashboard_sim.py
results/      (generated)
```

## 9. Design decisions and limitations
* **Why proxy ARP?** The topology has loops; flooding ARP through the fabric would create a broadcast storm.
  The controller floods only on host-facing ports.
* **Why destination-based proactive flows?** Re-routing after a failure is a single overwrite of
  one flow per (switch, host); no per-flow state, no packet-in storm.
* **Detection:** PortStatus gives millisecond detection for local link-down; LLDP timeout (~seconds)
  is the fallback for silent failures. Link-up detection relies on LLDP rediscovery (~1-2 s).
* **Probe-based recovery** avoids a retransmission storm during long outages.
* Hop-count routing is used (not delay-aware); an extension is weighting by measured latency.
* UDP gives no delivery guarantee; reliability here is implemented at the application layer on purpose.
* Authors' note: the socket layer was verified locally (`tests/test_local_recovery.py`) and the analysis
  pipeline on synthetic data; run sections 4C/4D on your Mininet VM to produce the real results.

## 10. Guideline / rubric checklist
| Rubric item | Evidence in this repo |
|---|---|
| Problem understanding & architecture | sections 1-2, topology diagram, dashboard topology view |
| TCP/UDP socket implementation | `src/` raw `SOCK_DGRAM`, threads, ACK/probe protocol, duplicate detection, retries |
| Mininet + SDN implementation | `topology/topo.py`, `controller/adaptive_router.py` (LLDP, PortStatus, BFS flows, ARP proxy) |
| Socket-SDN integration | UDP traffic carried by controller-installed flows; app `outage_detected` -> controller port audit; live path/flow view in the dashboard |
| Initial testing & demo | T1, `pingall`, dashboard baseline view |
| Complete functionality | 2 clients + server, recovery state machine, runtime mode switch |
| Dynamic SDN functionality | failure detection, rerouting, link-up re-convergence, monitoring (port stats) |
| Challenging scenario / robustness | T3 double failure, T4 flapping (dashboard scenario buttons) |
| Performance evaluation & comparison | `experiments/`, `analysis/`, dashboard benchmark; static baseline vs SDN, with/without app recovery, 3 scenarios x 3 runs |
| Documentation, demo | this README, `dashboard/`, section 9 (design decisions) |
