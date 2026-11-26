#!/usr/bin/env python3
"""Fit BMQ's traffic parameters to a gem5/Garnet trace, and validate.

A run directory has trace.csv.gz (one line per message, before
replication), eject_trace.csv.gz (delivery cycle per packet) and
config.ini. fit() returns the parameters at two levels (global, per node)
and Garnet's measured latencies (BMQ_COMPLETE_GUIDE.md section 5).

usage: fit_params.py RUN_DIR [RUN_DIR ...] [--csv FILE]
"""

import argparse
import gzip
import os
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

import bmq
from bmq import Model, Params

CHUNK = 500_000                    # trace lines per read (memory: 6x6/8x8 traces)


def mesh(run):
    """Mesh size from the run's config.ini: routers and their number."""
    with open(os.path.join(run, "config.ini")) as f:
        routers = sum(1 for line in f
                      if line.startswith("[system.ruby.network.routers")
                      and line.count(".") == 3)
    return round(routers ** 0.5)


def roi(trace):
    """Vnet names, controller layers {node-id base: count} and ROI (first
    to last stat-reset marker, cut at the ROI limit). Markers sit between
    data lines, so the whole file is read."""
    vnets, layers, marks, limit = {}, {}, [], None
    with gzip.open(trace, "rt") as f:
        for line in f:
            if line.startswith("#"):
                tok = line.split()
                if tok[1] == "vnet":
                    vnets[int(tok[2])] = tok[3]
                elif tok[1] == "node":
                    kv = dict(t.split("=") for t in tok[3:])
                    layers[int(kv["base"])] = int(kv["count"])
                elif tok[1] == "reset_stats":
                    marks.append(int(tok[2]))
                elif tok[1] == "roi_limit":
                    limit = int(tok[2])
    m = sorted(set(c for c in marks if c > 0))       # cycle 0 = startup
    hi = (m[-1] if len(m) > 1 else None) if m else None
    if m and limit is not None:
        hi = limit if hi is None else min(hi, limit)
    return vnets, layers, (m[0], hi) if m and hi is not None else None


def chunks(trace, span):
    """Messages inside the ROI, CHUNK rows at a time."""
    for c in pd.read_csv(trace, comment="#", chunksize=CHUNK,
                         dtype={"type": str, "dests": str, "pkts": str}):
        if span:
            c = c[(c.enq_cyc >= span[0]) & (c.enq_cyc < span[1])]
        if not c.empty:
            yield c


def bases(nodes):
    """bmq.base_of for an array of node ids."""
    b = np.array(sorted(bmq.LAYERS)) if bmq.LAYERS else None
    if b is None:
        return nodes // bmq.NROUTERS * bmq.NROUTERS
    return b[np.searchsorted(b, nodes, side="right") - 1]


def groups(c):
    """(vnet, type, sender kind: L1 or home node) per message."""
    kind = np.where(bases(c.src.values) == 0, "l1", "home")
    return list(zip(c.vnet.tolist(), c.type.tolist(), kind.tolist()))


def send_delays(trace, span):
    """Fixed controller delay per group: min(start - enq), or min + 1 if
    more common. Queueing only adds to it (guide 5.2)."""
    hist = defaultdict(Counter)
    for c in chunks(trace, span):
        for g, d in zip(groups(c), (c.start_cyc - c.enq_cyc).values):
            hist[g][int(d)] += 1
    return {g: min(h) + 1 if h[min(h) + 1] > h[min(h)] else min(h)
            for g, h in hist.items()}


def fit_pb(t):
    """Burst probability from sorted arrival times (Mandal et al.): mean
    wait W_v of a unit-service virtual queue (Lindley, closed form), then
    invert W_v = p_b / ((1 - p_b)(1 - lam))."""
    if len(t) < 2:
        return 0.0
    x = np.arange(len(t)) - (t - t[0])
    w = x - np.minimum.accumulate(x)
    wv = w.mean() * (1 - len(t) / max(1, t[-1] - t[0] + 1))
    return float(wv / (1 + wv))


def eject_times(path):
    """Delivery cycle per packet id (-1 if never delivered)."""
    ej = np.full(1 << 20, -1, dtype=np.int64)
    for c in pd.read_csv(path, comment="#", chunksize=CHUNK):
        if c.pkt.max() >= len(ej):
            ej = np.concatenate([ej, np.full(max(c.pkt.max() + 1, 2 * len(ej))
                                             - len(ej), -1, dtype=np.int64)])
        ej[c.pkt.values] = c.cyc.values
    return ej


def mix(counter):
    tot = sum(counter.values())
    return {k: n / tot for k, n in counter.items()} if tot else {}


def fit(run):
    """Returns (global Params, per-node Params, measured latencies)."""
    trace = os.path.join(run, "trace.csv.gz")
    vnets, layers, span = roi(trace)
    bmq.set_mesh(mesh(run), layers)      # stays set for the Model built next
    dly = send_delays(trace, span)
    ej = eject_times(os.path.join(run, "eject_trace.csv.gz"))
    n_src, n_mc, fan = Counter(), Counter(), Counter()
    fan_src = defaultdict(Counter)
    uni_pk, mc_pk = defaultdict(Counter), defaultdict(Counter)
    uni_base, mc_base = Counter(), Counter()
    times = defaultdict(list)
    ptimes = defaultdict(list)            # per copy, for the Mandal baseline
    acc = defaultdict(lambda: [0.0, 0])               # measured: [sum, n]
    lo = hi = None
    for c in chunks(trace, span):
        lo = min(lo or c.enq_cyc.min(), c.enq_cyc.min())
        hi = max(hi or 0, c.enq_cyc.max())
        # NI arrival = creation + fixed delay
        d = np.array([dly[g] for g in groups(c)])
        c = c.assign(ready=c.enq_cyc.values
                     + np.minimum(d, (c.start_cyc - c.enq_cyc).values))
        for q, g in c.groupby(["src", "vnet"]):
            times[q].append(g.ready.values)
            ptimes[q].append((g.ready.values, g.ndest.values.astype(np.int16)))
        # counts for the parameters
        mc = c.ndest > 1
        n_src.update(c.src.tolist())
        n_mc.update(c.src[mc].tolist())
        fan.update(c.ndest[mc].tolist())
        for (s, k), n in c[mc].groupby(["src", "ndest"]).size().items():
            fan_src[int(s)][int(k)] += int(n)
        for sel, pk in ((~mc, uni_pk), (mc, mc_pk)):
            for (s, L, v), n in c[sel].groupby(["src", "flits", "vnet"]).size().items():
                pk[int(s)][(int(L), int(v))] += int(n)
        base = pd.Series(bases(c.dests.str.split(";").str[0].astype(int).values),
                         index=c.index)
        uni_base.update(base[~mc].tolist())
        mc_base.update(base[mc].tolist())
        # Garnet's latencies, per copy then per message
        rows = c.assign(pkt=c.pkts.str.split(";")).explode("pkt")
        e = ej[rows.pkt.astype(np.int64).values]
        rows = rows.assign(e=e, ok=e >= 0)
        per = rows.groupby(level=0).agg(e=("e", "max"), ok=("ok", "all"),
                                        ready=("ready", "first"),
                                        ndest=("ndest", "first"),
                                        end=("end_cyc", "first"))
        per = per[per.ok]
        u, m = per[per.ndest == 1], per[per.ndest > 1]
        good = rows[rows.ok & (rows.ndest > 1)]
        for k, vals in (("uni", u.e - u.ready), ("compl", m.e - m.ready),
                        ("ni_lastvc", m.end - m.ready),
                        ("copy", good.e - good.ready)):
            acc[k][0] += float(vals.sum())
            acc[k][1] += len(vals)
    # the parameters
    cycles = (span[1] - span[0]) if span else (hi - lo + 1)
    sources = tuple(sorted(int(s) for s in n_src))
    tot = sum(n_src.values())
    pb = {(int(s), int(v)): fit_pb(np.sort(np.concatenate(t)))
          for (s, v), t in times.items()}
    q_n = {(int(s), int(v)): sum(map(len, t)) for (s, v), t in times.items()}
    all_uni = sum((uni_pk[s] for s in sources), Counter())
    all_mc = sum((mc_pk[s] for s in sources), Counter())
    common = dict(
        K=mix(fan) or 15, pattern="uniform", sources=sources,
        dst_base=mix(uni_base) or 0,
        mc_dst_base=max(mc_base, key=mc_base.get) if mc_base else None,
        vbuf={v: 4 if name == "response" else 1 for v, name in vnets.items()},
        packets=mix(all_uni),
        mc_packets=mix(all_mc) or None)
    glob = Params(lam=tot / cycles / len(sources),
                  alpha=sum(n_mc.values()) / tot,
                  pb=sum(q_n[q] * pb[q] for q in pb) / tot, **common)
    nodes = {s: {"lam": n_src[s] / cycles, "alpha": n_mc[s] / n_src[s],
                 "K": mix(fan_src[s]) or 15,
                 "packets": mix(uni_pk[s]) or mix(all_uni),
                 "mc_packets": mix(mc_pk[s]) or mix(all_mc) or None}
             for s in sources}
    node = Params(lam=glob.lam, alpha=glob.alpha, pb=pb, nodes=nodes, **common)
    node.pb_pkt = {}
    for (s, v), t in ptimes.items():     # expand copies one queue at a time
        node.pb_pkt[(int(s), int(v))] = fit_pb(np.sort(np.repeat(
            np.concatenate([r for r, _ in t]), np.concatenate([k for _, k in t]))))
    return glob, node, {k: a / n for k, (a, n) in acc.items() if n}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+", help="results/<protocol>/<bench> dirs")
    ap.add_argument("--csv", help="write the comparison to this file")
    a = ap.parse_args()
    keys = ("uni", "copy", "compl", "ni_lastvc")
    print(f"{'run':40s} {'params':7s} " +
          " ".join(f"{k + ' meas/pred':>21s}" for k in keys), flush=True)
    rows = []
    for run in a.runs:
        glob, node, meas = fit(run)
        for level, p in (("global", glob), ("node", node)):
            m = Model(p)
            pred = {} if m.saturated() else m.summary()
            row = {"run": run, "params": level, "saturated": m.saturated(),
                   "max_link_rho": max(m.link_rho.values())}
            cells = []
            for k in keys:
                row[f"{k}_meas"], row[f"{k}_pred"] = meas.get(k), pred.get(k)
                cells.append(f"{meas[k]:7.1f} /{pred[k]:7.1f}"
                             f"{100 * (pred[k] - meas[k]) / meas[k]:+6.0f}%"
                             if k in meas and k in pred else
                             ("saturated" if m.saturated() else "-"))
            rows.append(row)
            print(f"{run[-40:]:40s} {level:7s} " +
                  " ".join(f"{x:>21s}" for x in cells), flush=True)
        if a.csv:
            pd.DataFrame(rows).to_csv(a.csv, index=False, float_format="%.4g")


if __name__ == "__main__":
    main()
