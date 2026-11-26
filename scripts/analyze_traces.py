#!/usr/bin/env python3
"""Multicast share and burstiness of Garnet message traces.

Reads results/<protocol>/<bench>/trace.csv.gz (written by the Garnet
trace_file hook), restricts it to the region of interest delimited by the
"# reset_stats" markers, and reports per run:

  * message / packet / flit counts, and the share that is multicast
    (a message with ndest > 1 becomes ndest unicast packets in Garnet)
  * fan-out distribution and the message types that multicast
  * burstiness of the injection process, for all traffic and for
    multicast only: CV of inter-arrival times, index of dispersion of
    counts IDC(T) over window sizes T, and a Hurst estimate from the
    aggregated-variance method

Outputs results/summary.csv (one row per run) and
results/<protocol>/<bench>/analysis.json.

usage: analyze_traces.py [--results DIR] [--only REGEX]
"""

import argparse
import gzip
import json
import os
import re
import sys

import numpy as np
import pandas as pd

WINDOWS = [10, 100, 1000, 10000]
LOCAL_SPAN = 50000  # cycles per window for phase-local statistics


CHUNK = 2_000_000  # trace lines per read; keeps memory bounded on big runs
COLS = ["enq_cyc", "start_cyc", "end_cyc", "src", "vnet", "flits", "type",
        "ndest", "pkts"]


def read_header(path):
    """Node map, vnet names, ROI markers and ROI limit from '#' lines."""
    nodes, vnets, markers, limit = {}, {}, [], None
    with gzip.open(path, "rt") as f:
        for line in f:
            if not line.startswith("#"):
                continue
            tok = line.split()
            if tok[1] == "node":
                kv = dict(t.split("=") for t in tok[3:])
                nodes[tok[2]] = (int(kv["base"]), int(kv["count"]))
            elif tok[1] == "vnet":
                vnets[int(tok[2])] = tok[3]
            elif tok[1] == "reset_stats":
                markers.append(int(tok[2]))
            elif tok[1] == "roi_limit":
                limit = int(tok[2])
    return nodes, vnets, markers, limit


def roi(markers, limit):
    """ROI = first to last stat-reset marker issued by the workload, cut at
    the --roi-max-cycles limit when the run was stopped early."""
    m = sorted(set(c for c in markers if c > 0))
    if not m:
        return None
    lo = m[0]
    hi = m[-1] if len(m) > 1 else None
    if limit is not None:
        hi = limit if hi is None else min(hi, limit)
    return None if hi is None else (lo, hi)


def load_eject(ej_path):
    """Dense array: packet id -> ejection cycle (-1 if never delivered)."""
    ej = np.full(1 << 20, -1, dtype=np.int64)
    for c in pd.read_csv(ej_path, comment="#", chunksize=CHUNK,
                         dtype={"pkt": np.int64, "cyc": np.int64}):
        top = int(c.pkt.max())
        if top >= len(ej):
            grown = np.full(max(top + 1, 2 * len(ej)), -1, dtype=np.int64)
            grown[:len(ej)] = ej
            ej = grown
        ej[c.pkt.values] = c.cyc.values
    return ej


class LatencyAcc:
    """Running sums for per-copy and completion latency (source NI enqueue
    to tail-flit ejection at the destination NI)."""

    def __init__(self, ej):
        self.ej = ej
        self.uni = [0.0, 0]
        self.copy = [0.0, 0]
        self.done = {}  # fan-out -> [sum, count]

    def lookup(self, pkt):
        ok = (pkt >= 0) & (pkt < len(self.ej))
        out = np.full(len(pkt), -1, dtype=np.int64)
        out[ok] = self.ej[pkt[ok]]
        return out

    def add(self, c):
        uc = c[c.ndest == 1]
        e = self.lookup(uc.pkts.astype(np.int64).values)
        lat = (e - uc.enq_cyc.values)[e >= 0]
        self.uni[0] += lat.sum()
        self.uni[1] += len(lat)

        mc = c[c.ndest > 1]
        if mc.empty:
            return
        cp = mc[["enq_cyc", "ndest"]].assign(
            pkt=mc.pkts.str.split(";")).explode("pkt")
        e = self.lookup(cp.pkt.astype(np.int64).values)
        cp = cp.assign(lat=e - cp.enq_cyc.values, ok=e >= 0)
        good = cp[cp.ok]
        self.copy[0] += good.lat.sum()
        self.copy[1] += len(good)
        per = cp.groupby(level=0).agg(lat=("lat", "max"), ok=("ok", "all"),
                                      k=("ndest", "first"))
        # completion only for multicasts with every copy delivered
        for k, g in per[per.ok].groupby("k").lat:
            acc = self.done.setdefault(int(k), [0.0, 0])
            acc[0] += g.sum()
            acc[1] += len(g)

    def result(self):
        mean = lambda a: float(a[0] / a[1]) if a[1] else None
        out = {"unicast_lat_mean": mean(self.uni)}
        if self.copy[1]:
            tot = [sum(v[0] for v in self.done.values()),
                   sum(v[1] for v in self.done.values())]
            out["mcast_copy_lat_mean"] = mean(self.copy)
            out["mcast_completion_lat_mean"] = mean(tot)
            out["mcast_completion_by_fanout"] = {
                k: mean(v) for k, v in sorted(self.done.items())}
        return out


def load_trace(path, span, lat):
    """ROI rows of the trace as compact numeric columns; packet ids are
    consumed chunk by chunk by the latency accumulator, not kept."""
    parts = []
    for c in pd.read_csv(path, comment="#", usecols=COLS, chunksize=CHUNK,
                         dtype={"type": "category", "pkts": str}):
        if span:
            c = c[(c.enq_cyc >= span[0]) & (c.enq_cyc < span[1])]
        if c.empty:
            continue
        if lat:
            lat.add(c)
        parts.append(pd.DataFrame({
            "enq_cyc": c.enq_cyc.values.astype(np.int64),
            "ser": (c.end_cyc - c.start_cyc).values.astype(np.int32),
            "src": c.src.values.astype(np.int16),
            "vnet": c.vnet.values.astype(np.int8),
            "flits": c.flits.values.astype(np.int16),
            "ndest": c.ndest.values.astype(np.int16),
            "type": c.type.astype(str).values,
        }))
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df["type"] = df.type.astype("category")
    return df


def node_type(nodes, nid):
    for name, (base, count) in nodes.items():
        if base <= nid < base + count:
            return name
    return "?"


def counts_per_window(t, T, t0, t1):
    nb = max(1, int((t1 - t0) // T))
    c, _ = np.histogram(t, bins=nb, range=(t0, t0 + nb * T))
    return c


def burstiness(t, t0, t1):
    """Burstiness statistics of a point process given event times t."""
    t = np.sort(np.asarray(t, dtype=np.int64))
    out = {"events": int(len(t))}
    if len(t) < 3:
        return out
    dt = np.diff(t)
    mean = dt.mean()
    out["rate_per_kcyc"] = 1000.0 * len(t) / max(1, t1 - t0)
    out["iat_mean"] = float(mean)
    # CV = 1 for Poisson, > 1 bursty
    out["iat_cv"] = float(dt.std() / mean) if mean > 0 else None
    # share of events arriving in the same cycle as the previous one
    out["iat_zero_frac"] = float((dt == 0).mean())
    # IDC(T) = Var[N_T] / E[N_T]; 1 for Poisson, grows with T if bursty
    idc = {}
    for T in WINDOWS:
        c = counts_per_window(t, T, t0, t1)
        if len(c) >= 20 and c.mean() > 0:
            idc[str(T)] = float(c.var() / c.mean())
    out["idc"] = idc
    # Hurst via aggregated variance: Var(X^(m)) ~ m^(2H-2)
    base = counts_per_window(t, 10, t0, t1).astype(float)
    ms, vs = [], []
    m = 1
    while len(base) // m >= 32:
        n = len(base) // m
        agg = base[: n * m].reshape(n, m).mean(axis=1)
        if agg.var() > 0:
            ms.append(m)
            vs.append(agg.var())
        m *= 2
    if len(ms) >= 4:
        slope = np.polyfit(np.log(ms), np.log(vs), 1)[0]
        out["hurst"] = float(1 + slope / 2)
    out["local"] = local_burstiness(t, t0, t1)
    return out


def local_burstiness(t, t0, t1, span=LOCAL_SPAN, min_events=200):
    """Burstiness inside fixed windows, so program phases (e.g. FFT's
    transpose vs compute) do not inflate the statistics. Medians over the
    windows with enough events; this is what a stationary model sees."""
    cvs, idc100, rates = [], [], []
    for w0 in range(t0, t1 - span + 1, span):
        tw = t[(t >= w0) & (t < w0 + span)]
        if len(tw) < min_events:
            continue
        dt = np.diff(tw)
        if dt.mean() > 0:
            cvs.append(dt.std() / dt.mean())
        c = counts_per_window(tw, 100, w0, w0 + span)
        if c.mean() > 0:
            idc100.append(c.var() / c.mean())
        rates.append(1000.0 * len(tw) / span)
    if not cvs:
        return {"windows": 0}
    return {
        "windows": len(cvs),
        "active_frac": len(cvs) / max(1, (t1 - t0) // span),
        "iat_cv_median": float(np.median(cvs)),
        "idc_100_median": float(np.median(idc100)),
        "rate_per_kcyc_median": float(np.median(rates)),
    }


def analyze(path):
    nodes, vnets, markers, limit = read_header(path)
    span = roi(markers, limit)
    ej_path = os.path.join(os.path.dirname(path),
                           "eject_" + os.path.basename(path))
    lat = LatencyAcc(load_eject(ej_path)) if os.path.exists(ej_path) else None
    df = load_trace(path, span, lat)
    if df.empty:
        return None
    t0 = span[0] if span else int(df.enq_cyc.min())
    t1 = span[1] if span else int(df.enq_cyc.max()) + 1

    mc = df.ndest > 1
    res = {
        "roi": span,
        "roi_cycles": int(t1 - t0),
        "messages": int(len(df)),
        "mcast_messages": int(mc.sum()),
        "packets": int(df.ndest.sum()),
        "mcast_packets": int(df.ndest[mc].sum()),
        "flits": int((df.ndest * df.flits).sum()),
        "mcast_flits": int((df.ndest * df.flits)[mc].sum()),
    }
    res["mcast_msg_frac"] = res["mcast_messages"] / res["messages"]
    res["mcast_pkt_frac"] = res["mcast_packets"] / res["packets"]
    res["mcast_flit_frac"] = res["mcast_flits"] / res["flits"]

    fan = df.ndest[mc].value_counts().sort_index()
    res["fanout_hist"] = {int(k): int(v) for k, v in fan.items()}
    res["fanout_mean"] = float(df.ndest[mc].mean()) if mc.any() else 0.0

    by_type = df.groupby("type", observed=True).agg(
        msgs=("ndest", "size"),
        mcast=("ndest", lambda s: int((s > 1).sum())),
        mean_ndest=("ndest", "mean"))
    res["by_type"] = {k: {kk: (float(vv) if kk == "mean_ndest" else int(vv))
                          for kk, vv in r.items()}
                      for k, r in by_type.iterrows()}
    res["by_vnet"] = {
        f"{v}:{vnets.get(v, '?')}": {
            "msgs": int((df.vnet == v).sum()),
            "mcast": int(((df.vnet == v) & mc).sum())}
        for v in sorted(df.vnet.unique())}
    # cores generating traffic in the ROI; catches serial-only windows
    base, count = nodes.get("L1Cache", (0, 0))
    per_l1 = df.src[(df.src >= base) & (df.src < base + count)].value_counts()
    res["active_l1"] = int((per_l1 > 0.01 * len(df) / max(1, count)).sum())
    res["mcast_src_types"] = {
        k: int(v) for k, v in df.src[mc].map(
            lambda n: node_type(nodes, n)).value_counts().items()}

    res["burst_all"] = burstiness(df.enq_cyc.values, t0, t1)
    res["burst_mcast"] = burstiness(df.enq_cyc[mc].values, t0, t1)
    # multicast arrivals per source NI (what an NI-level model sees)
    per_src = [burstiness(g.enq_cyc.values, t0, t1)
               for _, g in df[mc].groupby("src") if len(g) > 50]
    if per_src:
        res["burst_mcast_per_src_cv_median"] = float(np.median(
            [b["iat_cv"] for b in per_src if b.get("iat_cv") is not None]))
    # NI queueing: cycles a multicast spends acquiring VCs for all copies
    res["mcast_ni_serialization_mean"] = float(
        df.ser[mc].mean()) if mc.any() else 0.0

    if lat:
        res.update(lat.result())
    return res


def source(bench):
    """micro-* are our synthetic kernels; s3/s4 are SPLASH-3/4, p- PARSEC."""
    if bench.startswith("micro-"):
        return "custom-micro"
    if bench.startswith(("sweep-", "nsweep-")):
        return "custom-sweep"
    if bench.startswith("p-"):
        return "standard-PARSEC-3.0"
    return "standard-SPLASH-" + bench[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "results"))
    ap.add_argument("--only", default=".*")
    a = ap.parse_args()

    rows = []
    for proto in sorted(os.listdir(a.results)):
        pdir = os.path.join(a.results, proto)
        if not os.path.isdir(pdir):
            continue
        for bench in sorted(os.listdir(pdir)):
            tr = os.path.join(pdir, bench, "trace.csv.gz")
            if not re.search(a.only, bench) or not os.path.exists(tr):
                continue
            try:
                r = analyze(tr)
            except Exception as e:  # truncated trace from a failed run
                print(f"skip {proto}/{bench}: {e}", file=sys.stderr)
                continue
            if r is None:
                continue
            with open(os.path.join(pdir, bench, "analysis.json"), "w") as f:
                json.dump(r, f, indent=1)
            ba, bm = r["burst_all"], r["burst_mcast"]
            rows.append({
                "protocol": proto, "bench": bench,
                "source": source(bench),
                "roi_kcycles": r["roi_cycles"] / 1e3,
                "active_l1": r["active_l1"],
                "messages": r["messages"],
                "mcast_msg_%": 100 * r["mcast_msg_frac"],
                "mcast_pkt_%": 100 * r["mcast_pkt_frac"],
                "mcast_flit_%": 100 * r["mcast_flit_frac"],
                "fanout_mean": r["fanout_mean"],
                "all_iat_cv": ba.get("iat_cv"),
                "mcast_iat_cv": bm.get("iat_cv"),
                "mcast_idc_100": bm.get("idc", {}).get("100"),
                "mcast_idc_1000": bm.get("idc", {}).get("1000"),
                "mcast_hurst": bm.get("hurst"),
                "mcast_local_iat_cv": bm.get("local", {}).get("iat_cv_median"),
                "mcast_local_idc_100": bm.get("local", {}).get(
                    "idc_100_median"),
                "all_local_iat_cv": ba.get("local", {}).get("iat_cv_median"),
                "mcast_per_kcyc": bm.get("rate_per_kcyc"),
                "unicast_lat": r.get("unicast_lat_mean"),
                "mcast_copy_lat": r.get("mcast_copy_lat_mean"),
                "mcast_completion_lat": r.get("mcast_completion_lat_mean"),
            })
            print(f"{proto:15s} {bench:22s} mcast msgs "
                  f"{100 * r['mcast_msg_frac']:5.1f}%  pkts "
                  f"{100 * r['mcast_pkt_frac']:5.1f}%", flush=True)
    if rows:
        # merge into the existing summary so --only reruns update in place
        out = os.path.join(a.results, "summary.csv")
        new = pd.DataFrame(rows)
        if os.path.exists(out):
            old = pd.read_csv(out)
            keep = ~old.set_index(["protocol", "bench"]).index.isin(
                new.set_index(["protocol", "bench"]).index)
            new = pd.concat([old[keep], new], ignore_index=True)
        new = new.sort_values(["protocol", "bench"])
        new.to_csv(out, index=False, float_format="%.4g")
        # standard suites and our microbenchmarks reported separately
        std = new.source.str.startswith("standard")
        new[std].to_csv(os.path.join(a.results, "summary_standard.csv"),
                        index=False, float_format="%.4g")
        new[~std].to_csv(os.path.join(a.results, "summary_custom.csv"),
                         index=False, float_format="%.4g")


if __name__ == "__main__":
    main()
