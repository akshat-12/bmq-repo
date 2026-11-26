#!/usr/bin/env python3
"""Baseline analytical model: Moadeli & Vanderbauwhede, "A Performance Model
of Multicast Communication in Wormhole-Routed Networks on-Chip", IPDPS 2009,
applied to our gem5/Garnet traces.

Model as in the paper:
  * every channel is an M/G/1 queue (Poisson arrivals at each channel):
        W = lambda*x^2 / (2(1 - lambda*x)) * (1 + sigma^2/x^2),
        rho = lambda*x, sigma = x - msg                          (eqs. 3-5)
    (fixed 2026-11-06: earlier code used lambda*rho = lambda^2*x in place
    of lambda*x^2, under-stating every wait by a factor x/lambda)
  * service time computed backward from the ejection channel (x = msg):
        x_i = sum_j ((1 - lambda_ij/lambda_j) W_j + x_j + hop) P_ij  (eq. 6)
  * unicast latency = sum of header waits on the path + msg + hops (eq. 7)
  * multicast: each stream's total wait ~ exponential with rate
    mu = 1/sum(w); multicast wait = E[max] over the independent streams;
    latency = E[max] + msg + max hops                        (eqs. 8-14)
  * multicast streams are independent and unsynchronized; message
    generation is Poisson

Adaptations to our system (documented, not in the paper):
  * topology: 4x4 mesh with XY routing instead of Quarc; channels are the
    NI injection link, inter-router links, and the NI ejection link
  * traffic: the measured traffic matrix of each trace's ROI (per source,
    destination and packet length) instead of uniform random destinations
  * a Garnet multicast is replicated at the source NI into K copies; each
    copy is treated as one of Moadeli's independent streams. This is the
    paper's key assumption and what our batch model replaces
  * two packet lengths (1 and 5 flits) instead of one; per-channel mean
    length is used for msg in eqs. 5-6
  * Garnet pipeline: "msg + hops" is replaced by the zero-load latency
    measured from the traces, T0 = 5 + 2H + 1.25(L-1), and the "+1" per hop
    in eq. 6 by HOP = 2 cycles, so both models share the same constants

Outputs, per run: predicted vs measured mean unicast latency, multicast
per-copy latency and multicast completion latency (results/summary.csv).
Writes results/moadeli_baseline.csv.

usage: moadeli_baseline.py [--results DIR] [--only REGEX]
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_traces import CHUNK, read_header, roi  # noqa: E402

ROWS = COLS = 4
NROUTERS = ROWS * COLS
HOP = 2  # cycles per hop in Garnet: router + link


def t0(hops, flits):
    """Garnet zero-load latency, enqueue to tail ejection (measured)."""
    return 5 + 2 * hops + 1.25 * (flits - 1)


def router(node):
    # Mesh_XY attaches controller i to router i mod 16
    return node % NROUTERS


def hops(src, dst):
    a, b = router(src), router(dst)
    return abs(a % COLS - b % COLS) + abs(a // COLS - b // COLS)


def path(src, dst):
    """Channels used by a packet: injection, XY mesh links, ejection."""
    ch = [("inj", src)]
    r, d = router(src), router(dst)
    while r % COLS != d % COLS:
        n = r + (1 if d % COLS > r % COLS else -1)
        ch.append(("link", r, n))
        r = n
    while r // COLS != d // COLS:
        n = r + (COLS if d // COLS > r // COLS else -COLS)
        ch.append(("link", r, n))
        r = n
    ch.append(("ej", dst))
    return ch


def load_traffic(trace):
    """Packet counts in the ROI: unicast (src, dst, flits) and multicast
    groups (src, dests, flits)."""
    _, _, markers, limit = read_header(trace)
    span = roi(markers, limit)
    uni, mcast = Counter(), Counter()
    t_lo, t_hi = None, None
    for c in pd.read_csv(trace, comment="#", chunksize=CHUNK,
                         usecols=["enq_cyc", "src", "flits", "ndest", "dests"],
                         dtype={"dests": str}):
        if span:
            c = c[(c.enq_cyc >= span[0]) & (c.enq_cyc < span[1])]
        if c.empty:
            continue
        lo, hi = int(c.enq_cyc.min()), int(c.enq_cyc.max())
        t_lo = lo if t_lo is None else min(t_lo, lo)
        t_hi = hi if t_hi is None else max(t_hi, hi)
        u = c[c.ndest == 1]
        uni.update(zip(u.src, u.dests.astype(int), u.flits))
        m = c[c.ndest > 1]
        mcast.update(zip(m.src, m.dests, m.flits))
    cycles = (span[1] - span[0]) if span else (t_hi - t_lo + 1)
    mcast = Counter({(s, tuple(int(x) for x in d.split(";")), f): n
                     for (s, d, f), n in mcast.items()})
    return uni, mcast, cycles


class Moadeli:
    """Channel-level M/G/1 network model of eqs. 3-6."""

    def __init__(self, uni, mcast, cycles):
        # every packet (each multicast copy counted on its own, Poisson)
        flows = Counter()
        for (s, d, f), n in uni.items():
            flows[(s, d, f)] += n
        for (s, ds, f), n in mcast.items():
            for d in ds:
                flows[(s, d, f)] += n
        self.lam = defaultdict(float)     # packets/cycle on channel
        self.flit = defaultdict(float)    # flits/cycle on channel
        self.nxt = defaultdict(lambda: defaultdict(float))  # i -> j rate
        for (s, d, f), n in flows.items():
            r = n / cycles
            p = path(s, d)
            for k, ch in enumerate(p):
                self.lam[ch] += r
                self.flit[ch] += r * f
                if k + 1 < len(p):
                    self.nxt[ch][p[k + 1]] += r
        self.msg = {c: self.flit[c] / self.lam[c] for c in self.lam}
        self.W = {c: 0.0 for c in self.lam}
        self.x = dict(self.msg)

    def solve(self, iters=200, tol=1e-9):
        for _ in range(iters):
            x = {}
            for c, lam in self.lam.items():
                if c[0] == "ej":
                    x[c] = self.msg[c]
                    continue
                tot = 0.0
                for j, lij in self.nxt[c].items():
                    p = lij / lam
                    tot += ((1 - lij / self.lam[j]) * self.W[j]
                            + self.x[j] + HOP) * p
                x[c] = tot
            W = {}
            for c, lam in self.lam.items():
                xc = x[c]
                rho = lam * xc
                if rho >= 1:
                    return False  # saturated
                sigma = xc - self.msg[c]
                # M/G/1 (Pollaczek-Khinchine): lam x^2 (1 + sigma^2/x^2) / 2(1-rho)
                W[c] = rho * xc / (2 * (1 - rho)) * (1 + sigma ** 2 / xc ** 2)
            diff = max(abs(W[c] - self.W[c]) for c in W)
            self.W, self.x = W, x
            if diff < tol:
                break
        return True

    def wait(self, src, dst):
        return sum(self.W[c] for c in path(src, dst))

    def utilization(self):
        return max(self.flit.values())


def e_max_exp(means):
    """E[max] of independent exponentials with the given means (eqs. 9-13,
    in closed form): integral of 1 - prod(1 - exp(-t/m)) dt."""
    m = np.array([v for v in means if v > 0])
    if len(m) == 0:
        return 0.0
    if len(m) == 1:
        return float(m[0])
    t = np.linspace(0, 40 * m.max(), 4001)
    cdf = np.prod(1 - np.exp(-t[:, None] / m[None, :]), axis=1)
    return float(np.trapz(1 - cdf, t))


def predict(model, uni, mcast):
    out = {}
    n = sum(uni.values())
    out["unicast_lat"] = sum(
        k * (model.wait(s, d) + t0(hops(s, d), f))
        for (s, d, f), k in uni.items()) / n if n else None
    nc = sum(k * len(ds) for (s, ds, f), k in mcast.items())
    nm = sum(mcast.values())
    if nm:
        out["mcast_copy_lat"] = sum(
            k * sum(model.wait(s, d) + t0(hops(s, d), f) for d in ds)
            for (s, ds, f), k in mcast.items()) / nc
        out["mcast_completion_lat"] = sum(
            k * (e_max_exp([model.wait(s, d) for d in ds])
                 + t0(max(hops(s, d) for d in ds), f))
            for (s, ds, f), k in mcast.items()) / nm
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "results"))
    ap.add_argument("--only", default=".*")
    a = ap.parse_args()

    meas = pd.read_csv(os.path.join(a.results, "summary.csv"))
    rows = []
    for _, m in meas.iterrows():
        if not re.search(a.only, m.bench):
            continue
        trace = os.path.join(a.results, m.protocol, m.bench, "trace.csv.gz")
        uni, mcast, cycles = load_traffic(trace)
        model = Moadeli(uni, mcast, cycles)
        ok = model.solve()
        pred = predict(model, uni, mcast) if ok else {}
        row = {"protocol": m.protocol, "bench": m.bench, "source": m.source,
               "max_link_util": model.utilization(), "saturated": not ok}
        for k in ("unicast_lat", "mcast_copy_lat", "mcast_completion_lat"):
            row[f"{k}_meas"] = m[k]
            row[f"{k}_model"] = pred.get(k)
            if pred.get(k) is not None and pd.notna(m[k]):
                row[f"{k}_err_%"] = 100 * (pred[k] - m[k]) / m[k]
        rows.append(row)
        print(f"{m.protocol:15s} {m.bench:20s} "
              f"uni {row['unicast_lat_meas']:5.1f}/{row.get('unicast_lat_model') or 0:5.1f}  "
              f"copy {row['mcast_copy_lat_meas']:5.1f}/{row.get('mcast_copy_lat_model') or 0:5.1f}  "
              f"compl {row['mcast_completion_lat_meas']:5.1f}/{row.get('mcast_completion_lat_model') or 0:5.1f}"
              "  (measured/model)", flush=True)
    pd.DataFrame(rows).to_csv(os.path.join(a.results, "moadeli_baseline.csv"),
                              index=False, float_format="%.4g")


if __name__ == "__main__":
    main()
