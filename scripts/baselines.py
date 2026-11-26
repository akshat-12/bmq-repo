#!/usr/bin/env python3
"""The two published baselines, on BMQ's inputs: the same traffic
parameters (bmq.Params, expanded by bmq.expand), the same mesh and XY
paths (bmq.path, any mesh size) and the same derived zero-load timing
(a lone packet's tail leaves the NI, then bmq.head_latency). Only the
queueing model differs.

  mandal   Mandal et al. [M20, M21], applied to multicast: every copy is
           its own packet in one GGeo stream per (source, vnet) NI queue,
           p_b fitted to the copies' arrivals (Params.pb_pkt), GGeo/G/1
           wait, the same for every copy (no copy order); network: GGeo/G/1
           decomposition with departure SCV [M21 eq. 18] and split/merge
           [M21 eq. 19], one link delivering at most one flit per cycle
           (SCV capped at 1 - r, as in bmq_model.py)
  moadeli  Moadeli & Vanderbauwhede, IPDPS'09: every channel (injection,
           links, ejection) an M/G/1 queue with service time computed
           backward from the ejection channel; copies are independent
           Poisson streams; completion = E[max] of exponential path waits
           + zero-load time of the farthest copy (moadeli_baseline.py)

The older 4x4-only versions (bmq_model.py, moadeli_baseline.py, fitted T0)
are kept for reference.
"""

from collections import Counter, defaultdict

import bmq
import moadeli_baseline as mb


def t0(h, L, b=1):
    """Zero-load latency shared by all models: lone tail + head latency."""
    return bmq.ni_schedule(1, L, b)[1][0] + bmq.head_latency(h)


def _vbuf(p, rates):
    if p.vbuf:
        return dict(p.vbuf)
    lmin = min(L for (_, _, L, _) in rates)
    out = {}
    for (_, _, L, v) in rates:
        out[v] = max(out.get(v, 1), 4 if L > lmin else 1)
    return out


def _summary(rates, copy_lat, compl_lat):
    """Rate-weighted unicast, per-copy and completion latency."""
    acc = defaultdict(float)
    for key, n in rates.items():
        s, ds, L, v = key
        lat = copy_lat(key)
        kind = "mc" if len(ds) > 1 else "uni"
        acc[kind + "_n"] += n
        acc[kind + "_copy"] += n * sum(lat) / len(lat)
        acc[kind + "_compl"] += n * compl_lat(key, lat)
        acc[kind + "_k"] += n * len(ds)
        acc[kind + "_copysum"] += n * sum(lat)
    out = {}
    if acc["uni_n"]:
        out["uni"] = acc["uni_copy"] / acc["uni_n"]
    if acc["mc_n"]:
        out["copy"] = acc["mc_copysum"] / acc["mc_k"]
        out["compl"] = acc["mc_compl"] / acc["mc_n"]
    return out


# ------------------------------------------------------------------ Mandal

def ggeo_wait(lam, T, cs2, ca2):
    """GGeo/G/1 mean wait [K94; M21 eqs. 4-5]."""
    rho = lam * T
    if rho >= 1:
        return float("inf")
    return max(0.0, T / 2 * ((ca2 - 1) + rho * (ca2 + cs2) / (1 - rho)))


def departure_scv(rho, cs2, ca2):
    """[M21 eq. 18]."""
    return rho ** 2 * (cs2 + 1) + (1 - rho) * ca2 + rho * (1 - 2 * rho)


def mandal(p, iters=30):
    """Returns ({uni, copy, compl}, saturated)."""
    rates = bmq.expand(p)
    vbuf = _vbuf(p, rates)
    pkts = defaultdict(Counter)                   # (s, v) -> {L: packets/cycle}
    flows = Counter()
    for (s, ds, L, v), r in rates.items():
        pkts[(s, v)][L] += r * len(ds)
        for d in ds:
            flows[(s, d, L)] += r
    pbp = p.pb_pkt if p.pb_pkt is not None else p.pb
    ni_w, out_rate, cd2 = {}, defaultdict(float), defaultdict(float)
    for q, m in pkts.items():
        lam = sum(m.values())
        T = sum(L * r for L, r in m.items()) / lam
        cs2 = sum(L * L * r for L, r in m.items()) / lam / T ** 2 - 1
        pb = min(pbp.get(q, 0.0) if isinstance(pbp, dict) else pbp, 0.99)
        ca2 = 2 / (1 - pb) - lam - 1                                  # [M20]
        if lam * T >= 1:
            return {}, True
        ni_w[q] = ggeo_wait(lam, T, cs2, ca2)
        out_rate[("ni", q[0])] += lam
        cd2[("ni", q[0])] += lam * departure_scv(lam * T, cs2, ca2)
    cd2 = {u: x / out_rate[u] for u, x in cd2.items()}
    # network channels
    lam, fl, fl2 = defaultdict(float), defaultdict(float), defaultdict(float)
    into = defaultdict(lambda: defaultdict(float))
    for (s, d, L), r in flows.items():
        up = ("ni", s)
        for ch in bmq.path(s, d):
            lam[ch] += r
            fl[ch] += r * L
            fl2[ch] += r * L * L
            into[ch][up] += r
            up = ch
    T = {c: fl[c] / lam[c] for c in lam}
    cs2 = {c: fl2[c] / lam[c] / T[c] ** 2 - 1 for c in lam}
    if any(lam[c] * T[c] >= 1 for c in lam):
        return {}, True
    out_rate.update(lam)
    W = {}
    for _ in range(iters):
        for c in lam:
            num = 0.0
            for u, r in into[c].items():
                pr = min(1.0, r / out_rate[u])
                num += r * min(pr * cd2.get(u, 1.0) + 1 - pr, 1 - r)  # [M21 19]
            ca2 = num / lam[c]
            W[c] = ggeo_wait(lam[c], T[c], cs2[c], ca2)
            cd2[c] = departure_scv(lam[c] * T[c], cs2[c], ca2)

    def copy_lat(key):
        s, ds, L, v = key
        return [ni_w[(s, v)] + sum(W[c] for c in bmq.path(s, d))
                + t0(bmq.hops(s, d), L, vbuf.get(v, 1)) for d in ds]

    return _summary(rates, copy_lat, lambda key, lat: max(lat)), False


# ----------------------------------------------------------------- Moadeli

def moadeli(p):
    """Returns ({uni, copy, compl}, saturated)."""
    rates = bmq.expand(p)
    vbuf = _vbuf(p, rates)
    u, m = Counter(), Counter()                    # Moadeli has no vnets
    for (s, ds, L, v), r in rates.items():
        if len(ds) == 1:
            u[(s, ds[0], L)] += r
        else:
            m[(s, ds, L)] += r
    # channels: NI injection link, then BMQ's XY path (any mesh size);
    # "+1 per hop" of eq. 6 is a router plus a link here
    mb.path = lambda s, d: [("inj", s)] + bmq.path(s, d)
    mb.HOP = bmq.T_ROUTER + bmq.T_LINK
    model = mb.Moadeli(u, m, 1.0)
    if not model.solve():
        return {}, True

    def copy_lat(key):
        s, ds, L, v = key
        return [model.wait(s, d) + t0(bmq.hops(s, d), L, vbuf.get(v, 1))
                for d in ds]

    def compl_lat(key, lat):
        s, ds, L, v = key
        if len(ds) == 1:
            return lat[0]
        return (mb.e_max_exp([model.wait(s, d) for d in ds])       # eqs. 8-14
                + t0(max(bmq.hops(s, d) for d in ds), L, vbuf.get(v, 1)))

    return _summary(rates, copy_lat, compl_lat), False
