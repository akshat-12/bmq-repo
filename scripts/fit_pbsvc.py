#!/usr/bin/env python3
"""Fix A (guide 7.10): burst probability fitted at the NI's service
timescale. For each (src, vnet) NI queue of a run, replay the real
message arrivals (estimated NI arrival, guide 5.2) through BMQ's own
service times (bmq.service, with the high-load correction's first-router
hold) in a FIFO queue, take the mean wait, and pick the p_b at which BMQ's
NI-wait formula (bmq.head_wait, eqs. 5-10) gives the same wait. Uses only
arrival times and the model's service times, not Garnet's measured waits.
Never lowers p_b below the unit-timescale fit (fit_params.fit_pb).

Needs the run's bmq_fit.pkl (compare_models.py writes it). Writes
bmq_pbsvc.pkl next to the trace: {(src, vnet): p_b}.

usage: fit_pbsvc.py [--mesh N] [-j N] RUN_DIR...
"""

import argparse
import os
import pickle
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

import mesh_cfg                     # reads --mesh
import bmq
import fit_params as fp


def lindley(a, s):
    """Mean FIFO wait for sorted arrivals a and service times s:
    departure D_i = S_i + max_{k<=i}(a_k - S_{k-1}), S = cumsum(s)."""
    S = np.cumsum(s)
    D = S + np.maximum.accumulate(a - (S - s))
    return float(np.mean(D - s - a))


def queue_inputs(m):
    """Per queue: lam, mix {(K, L): prob}, buffer depth, first-router hold
    (as in bmq.Model.__init__)."""
    per_q, first, ncopy = defaultdict(Counter), defaultdict(float), Counter()
    for (s, ds, L, v), r in m.rates.items():
        per_q[(s, v)][(len(ds), L)] += r
        ncopy[(s, v)] += r * len(ds)
        for d in ds:
            c = bmq.path(s, d)[0]
            first[(s, v)] += r * (m._inj(s) + m.W[c] + m.Wvc.get((c, v), 0.0))
    out = {}
    for q, mix in per_q.items():
        lam = sum(mix.values())
        out[q] = (lam, {k: r / lam for k, r in mix.items()},
                  m.vbuf.get(q[1], 1), first[q] / ncopy[q] if ncopy[q] else 0.0)
    return out


def fit_run(run):
    trace = os.path.join(run, "trace.csv.gz")
    out_f = os.path.join(run, "bmq_pbsvc.pkl")
    if os.path.exists(out_f) and os.path.getmtime(out_f) > os.path.getmtime(trace):
        return run, "cached"
    with open(os.path.join(run, "bmq_fit.pkl"), "rb") as f:
        mesh, node, _ = pickle.load(f)
    _, layers, span = fp.roi(trace)
    bmq.set_mesh(fp.mesh(run), layers)
    bmq.VC_HOLD = True
    dly = fp.send_delays(trace, span)
    arr = defaultdict(list)
    for c in fp.chunks(trace, span):
        d = np.array([dly[g] for g in fp.groups(c)])
        ready = c.enq_cyc.values + np.minimum(d, (c.start_cyc - c.enq_cyc).values)
        for (s, v), idx in c.groupby(["src", "vnet"]).indices.items():
            arr[(int(s), int(v))].append(
                (ready[idx], c.ndest.values[idx].astype(np.int16),
                 c.flits.values[idx].astype(np.int16)))
    bmq.set_mesh(*mesh)
    m = bmq.Model(node)
    qin = queue_inputs(m)
    base = node.pb if isinstance(node.pb, dict) else {}
    new, svc_cache = {}, {}
    for q, parts in arr.items():
        if q not in qin:
            continue
        lam, mix, b, extra = qin[q]
        a = np.concatenate([p[0] for p in parts]).astype(np.float64)
        K = np.concatenate([p[1] for p in parts])
        L = np.concatenate([p[2] for p in parts])
        o = np.argsort(a, kind="stable")
        a, K, L = a[o], K[o], L[o]
        s = np.empty(len(a))
        for kl in set(zip(K.tolist(), L.tolist())):
            key = (kl, b, extra)
            if key not in svc_cache:
                svc_cache[key] = bmq.service(kl[0], kl[1], b, extra)
            s[(K == kl[0]) & (L == kl[1])] = svc_cache[key]
        target = lindley(a, s)
        lo = base.get(q, 0.0)
        if bmq.head_wait(lam, lo, mix, b, extra)[0] >= target:
            new[q] = lo
            continue
        hi = 0.99
        if bmq.head_wait(lam, hi, mix, b, extra)[0] < target:
            new[q] = hi
            continue
        for _ in range(50):
            mid = (lo + hi) / 2
            if bmq.head_wait(lam, mid, mix, b, extra)[0] < target:
                lo = mid
            else:
                hi = mid
        new[q] = lo
    with open(out_f, "wb") as f:
        pickle.dump(new, f)
    raised = sum(1 for q in new if new[q] > base.get(q, 0.0) + 1e-3)
    return run, f"{len(new)} queues, {raised} raised"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=int, default=4)
    ap.add_argument("-j", type=int, default=1)
    ap.add_argument("runs", nargs="+")
    a = ap.parse_args()
    with ProcessPoolExecutor(a.j) as ex:
        for run, msg in ex.map(fit_run, a.runs):
            print(os.path.relpath(run, mesh_cfg.ROOT), msg, flush=True)


if __name__ == "__main__":
    main()
