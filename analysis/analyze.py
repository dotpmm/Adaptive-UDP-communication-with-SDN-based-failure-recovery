#!/usr/bin/env python3
"""Aggregate all runs under results/<scenario>/<config>/run<i>/ into tables + plots.

Metrics per run
  detect_ms        link failure injected -> controller 'link_down_detected'
  reroute_ms       link failure injected -> new flows pushed ('routes_updated')
  react_ms         switch reports the port down -> controller has pushed the new flows
  outage_ms        service interruption seen by the application: first message that did not
                   get through after the failure -> first later message delivered on its first
                   attempt (client 1); 0 = no message affected
  max_gap_ms       largest gap between server arrivals (also shows VM jitter / noise floor)
  loss_pct         application-level loss (never ACKed, all clients)
  first_try_loss   % of messages whose first transmission was not ACKed
  retransmissions  extra DATA datagrams sent
  rtt_pre_ms       mean RTT before the failure (primary path)
  rtt_backup_ms    mean RTT after recovery while running on the backup path
  e2e_p95/max_ms   generation->ACK latency (includes waiting for recovery)
"""
import argparse
import glob
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt   # noqa: E402
import numpy as np                # noqa: E402
import pandas as pd               # noqa: E402

CONFIGS = ['static-noapp', 'static-app', 'dynamic-noapp', 'dynamic-app']
LABELS = {'static-noapp': 'Static routing\n+ plain UDP',
          'static-app': 'Static routing\n+ app recovery',
          'dynamic-noapp': 'SDN reroute\n+ plain UDP',
          'dynamic-app': 'SDN reroute\n+ app recovery'}


def analyze_run(rundir):
    meta = json.load(open(os.path.join(rundir, 'meta.json')))
    ev = pd.read_csv(os.path.join(rundir, 'controller_events.csv'))
    srv = pd.read_csv(os.path.join(rundir, 'server_log.csv'))
    cl = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(rundir, 'client*.csv')))
                    if 'summary' not in f])
    thr = max(0.2, 5.0 / meta['rate'])
    downs = [e for e in meta['events'] if e['action'] == 'down']
    fail = downs[0]['ts'] if downs else None
    r = dict(fail=fail)

    # controller side
    r['detect_ms'] = r['reroute_ms'] = r['react_ms'] = np.nan
    if fail:
        d = ev[(ev.event == 'link_down_detected') & (ev.ts >= fail - 0.05)].ts
        u = ev[(ev.event == 'routes_updated') & ev.detail.str.contains('reason=link_down') &
               (ev.ts >= fail - 0.05)].ts
        pd_ = ev[(ev.event == 'port_status') & ev.detail.str.contains('down=True') &
                 (ev.ts >= fail - 0.05)].ts
        if len(d): r['detect_ms'] = (d.min() - fail) * 1000
        if len(u) and len(pd_): r['react_ms'] = (u.min() - pd_.min()) * 1000
        if len(u): r['reroute_ms'] = (u.min() - fail) * 1000

    # data-plane outage as seen by the application (client 1).
    # outage_ms = failure injection -> first message sent after the failure that got
    # through on its FIRST attempt (messages sent while the path was dead need retries
    # or are lost). Robust against random VM jitter elsewhere in the run.
    c1 = pd.read_csv(os.path.join(rundir, 'client1.csv'))
    s = np.sort(srv[srv.client_id == 1].recv_ts.to_numpy())
    gaps = np.diff(s)
    r['max_gap_ms'] = float(gaps.max() * 1000) if len(gaps) else np.nan   # includes VM jitter
    r['outage_ms'] = np.nan
    if fail:
        post = c1[c1.first_tx.notna() & (c1.first_tx > fail)].sort_values('first_tx')
        bad = post[(post.attempts > 1) | (post.status == 'lost')]
        if len(bad) == 0:
            r['outage_ms'] = 0.0          # no message was affected
        else:
            start = bad.first_tx.iloc[0]   # first message that did not get through
            good = post[(post.first_tx > start) & (post.attempts == 1) & post.rtt_ms.notna()]
            if len(good):
                r['outage_ms'] = (good.first_tx.iloc[0] - start) * 1000

    # loss / retransmissions (all clients)
    tot = len(cl)
    lost = int((cl.status == 'lost').sum())
    r['loss_pct'] = 100.0 * lost / tot
    r['first_try_loss_pct'] = 100.0 * (((cl.attempts > 1) | (cl.status == 'lost')).sum()) / tot
    r['retransmissions'] = int((cl.attempts - 1).clip(lower=0).sum())

    # latency
    ok = cl[cl.rtt_ms.notna()]
    r['rtt_mean_ms'] = ok.rtt_ms.mean()
    r['e2e_p95_ms'] = ok.e2e_ms.quantile(0.95)
    r['e2e_max_ms'] = ok.e2e_ms.max()
    r['rtt_pre_ms'] = r['rtt_backup_ms'] = np.nan
    if fail:
        pre = ok[ok.first_tx < fail - 0.3]
        r['rtt_pre_ms'] = pre.rtt_ms.mean()
        ups = [e['ts'] for e in meta['events'] if e['action'] == 'up' and e['ts'] > fail]
        end = ups[0] if ups else meta['t_start'] + meta['duration']
        rec = r['outage_ms'] / 1000 if np.isfinite(r['outage_ms']) else 0
        bk = ok[(ok.first_tx > fail + rec + 1.0) & (ok.first_tx < end - 0.5)]
        r['rtt_backup_ms'] = bk.rtt_ms.mean()
    return r


def collect(results):
    rows = []
    for meta in glob.glob(os.path.join(results, '*', '*', 'run*', 'meta.json')):
        rundir = os.path.dirname(meta)
        sc, cfg, run = rundir.split(os.sep)[-3:]
        try:
            row = analyze_run(rundir)
        except Exception as e:     # keep going if one run is broken
            print('skip %s: %s' % (rundir, e))
            continue
        row.update(scenario=sc, config=cfg, run=run, rundir=rundir)
        rows.append(row)
    return pd.DataFrame(rows)


def bar(ax, df, col, scenarios, title, ylabel, log=False):
    w = 0.2
    vmax = 1.0
    for i, cfg in enumerate(CONFIGS):
        vals, errs = [], []
        for sc in scenarios:
            x = df[(df.scenario == sc) & (df.config == cfg)][col]
            vals.append(x.mean() if len(x) else 0)
            errs.append(x.std() if len(x) > 1 else 0)
        vals, errs = np.nan_to_num(vals), np.nan_to_num(errs)
        vmax = max(vmax, float(np.max(vals + errs)))
        pos = np.arange(len(scenarios)) + (i - 1.5) * w
        ax.bar(pos, vals, w, yerr=errs, capsize=2, label=LABELS[cfg].replace('\n', ' '))
        for p, v in zip(pos, vals):          # print the value on every bar (also 0)
            txt = '%.0f' % v if v >= 10 else '%.1f' % v
            ax.annotate(txt, (p, v), ha='center', va='bottom', fontsize=6.5, rotation=90,
                        xytext=(0, 2), textcoords='offset points')
    if log:
        ax.set_yscale('symlog', linthresh=10)
        ax.set_ylim(0, vmax * 6)      # head-room for the value labels
        ylabel += ' (log scale)'
    else:
        ax.margins(y=0.18)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(scenarios)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis='y', alpha=.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results', default='results')
    a = ap.parse_args()
    df = collect(a.results)
    if df.empty:
        print('no runs found under', a.results)
        return
    df.drop(columns=['rundir']).to_csv(os.path.join(a.results, 'summary_runs.csv'), index=False)
    num = ['detect_ms', 'reroute_ms', 'react_ms', 'outage_ms', 'max_gap_ms', 'loss_pct',
           'first_try_loss_pct', 'retransmissions', 'rtt_pre_ms', 'rtt_backup_ms',
           'e2e_p95_ms', 'e2e_max_ms']
    agg = df.groupby(['scenario', 'config'])[num].agg(['mean', 'std']).round(2)
    agg.to_csv(os.path.join(a.results, 'summary_by_config.csv'))
    print(agg.to_string())

    scen = [s for s in ['single', 'double', 'flap'] if s in set(df.scenario)]
    if scen:
        fig, ax = plt.subplots(1, 3, figsize=(18, 5.2))
        bar(ax[0], df, 'outage_ms', scen, 'Service outage after first failure', 'ms', log=True)
        bar(ax[1], df, 'loss_pct', scen, 'Application-level packet loss', '% of messages')
        bar(ax[2], df, 'e2e_p95_ms', scen, 'p95 message latency (gen -> ACK)', 'ms', log=True)
        ax[1].legend(fontsize=7, loc='upper right')
        plt.tight_layout()
        plt.savefig(os.path.join(a.results, 'comparison.png'), dpi=140)
        plt.close()

    # RTT timeline of run1 for each config in each scenario
    for sc in scen:
        fig, axes = plt.subplots(len(CONFIGS), 1, figsize=(10, 9), sharex=True)
        for ax, cfg in zip(axes, CONFIGS):
            sub = df[(df.scenario == sc) & (df.config == cfg)]
            if sub.empty:
                continue
            rd = sub.iloc[0].rundir
            meta = json.load(open(os.path.join(rd, 'meta.json')))
            c = pd.read_csv(os.path.join(rd, 'client1.csv'))
            c = c[c.rtt_ms.notna() & c.first_tx.notna()]
            ax.plot(c.first_tx - meta['t_start'], c.rtt_ms, '.', ms=3)
            for e in meta['events']:
                ax.axvline(e['ts'] - meta['t_start'], color='r' if e['action'] == 'down'
                           else 'g', ls='--')
            ax.set_ylabel('RTT ms')
            ax.set_title(LABELS[cfg].replace('\n', ' '), fontsize=9)
        axes[-1].set_xlabel('time (s)   red = link down, green = link up')
        plt.tight_layout()
        plt.savefig(os.path.join(a.results, 'timeline_%s.png' % sc), dpi=140)
        plt.close()
    print('wrote summary_runs.csv, summary_by_config.csv, comparison.png, timeline_*.png')


if __name__ == '__main__':
    main()
