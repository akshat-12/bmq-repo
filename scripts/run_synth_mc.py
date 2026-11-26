#!/usr/bin/env python3
"""High-injection validation with multicast and bursty traffic: our
extension of Garnet_standalone's synthetic tester (--mc-prob: a packet is a
15-way multicast to every other directory; --burst-prob: GGeo bursts, i.e.
after one packet another in the same cycle with probability p_b). The
traffic is open-loop, and every model input is set on the command line, so
nothing is fitted: the model gets the same numbers gem5 gets.

On a larger mesh (--mesh N) a multicast goes to every other directory
(K = N*N - 1, or all 32 directories on 6x6 from a router without one), and
each configuration's rates are scaled so the model's busiest queue has
the same utilization as at the 4x4 rate.

usage: run_synth_mc.py [--mesh N] [-j N] [--dry]     run the sweep
       run_synth_mc.py [--mesh N] --compare [--csv FILE]   model vs Garnet
"""

import argparse
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from mesh_cfg import MESH, RESULTS, gem5_mesh_args, pow2_below, rate_scale, \
    use_mesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEM5 = os.path.join(ROOT, "gem5")
BIN = os.path.join(GEM5, "build", "Garnet_standalone", "gem5.opt")
CFG = os.path.join(GEM5, "configs", "example", "garnet_synth_traffic.py")
OUT = os.path.join(RESULTS, "garnet_synth_mc")
SIM_CYCLES = 200_000            # network cycles
TICKS_PER_CYCLE = 500           # 2 GHz; the tester's --sim-cycles is in ticks
MIX = {(1, 0): 1 / 3, (1, 1): 1 / 3, (5, 2): 1 / 3}   # --inj-vnet=-1

# (vnet, multicast fraction, burst probability, injection rates)
CONFIGS = (
    (0, 0.05, 0.0, (0.02, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5)),
    (0, 0.2, 0.0, (0.02, 0.05, 0.1, 0.15, 0.18, 0.2, 0.22, 0.25)),
    (0, 0.05, 0.5, (0.02, 0.05, 0.1, 0.15, 0.2, 0.23, 0.26, 0.3)),
    (-1, 0.05, 0.0, (0.02, 0.05, 0.1, 0.15, 0.18, 0.2, 0.22)),
)


def runs():
    """(vnet, alpha, pb, rate) per run; rates scaled on larger meshes."""
    out = []
    for v, a, pb, rates in CONFIGS:
        k = 1.0
        if MESH != 4:
            import bmq
            k = rate_scale(bmq, lambda r, m: params(v, a, pb, r, m))
        out += [(v, a, pb, float(f"{r * k:.3g}")) for r in rates]
    return out


def name(vnet, alpha, pb, rate):
    return (f"v{'mix' if vnet < 0 else vnet}-mc{alpha:g}-pb{pb:g}-i{rate:g}")


def cmd(vnet, alpha, pb, rate, outdir):
    return [BIN, f"--outdir={outdir}", "--redirect-stdout",
            "--redirect-stderr", CFG,
            "--network=garnet", *gem5_mesh_args(),
            "--synthetic=uniform_random", f"--injectionrate={rate}",
            f"--inj-vnet={vnet}", f"--mc-prob={alpha}", f"--burst-prob={pb}",
            "--sys-clock=2GHz",
            f"--sim-cycles={SIM_CYCLES * TICKS_PER_CYCLE}",
            "--garnet-trace=trace.csv.gz"]


def run(spec, dry):
    out = os.path.join(OUT, name(*spec))
    if os.path.exists(os.path.join(out, "eject_trace.csv.gz")):
        return f"skip {name(*spec)}"
    c = cmd(*spec, out)
    if dry:
        return " ".join(c)
    os.makedirs(out, exist_ok=True)
    r = subprocess.run(c, cwd=GEM5)
    return f"{'ok' if r.returncode == 0 else 'FAIL ' + str(r.returncode)} " \
           f"{name(*spec)}"


def params(vnet, alpha, pb, rate, mesh=MESH):
    """The model's inputs, straight from the gem5 options. A multicast goes
    to every other directory (K at least the number of them)."""
    import bmq
    n = mesh * mesh
    mix = {(1, 0): 1.0} if vnet == 0 else MIX
    return bmq.Params(lam=rate / (1 - pb), alpha=alpha, K=n - 1,
                      pattern="uniform", packets=mix, mc_packets=mix, pb=pb,
                      sources=tuple(range(n)), dst_base=n, mc_dst_base=n,
                      vbuf={0: 1, 1: 1, 2: 4})


def compare(csv):
    import pandas as pd
    import bmq
    import fit_params
    rows = []
    for spec in runs():
        d = os.path.join(OUT, name(*spec))
        if not os.path.exists(os.path.join(d, "eject_trace.csv.gz")):
            continue
        use_mesh(bmq)
        m = bmq.Model(params(*spec))
        pred = {} if m.saturated() else m.summary()
        meas = fit_params.fit(d)[2]
        row = dict(run=name(*spec), vnet=spec[0], alpha=spec[1], pb=spec[2],
                   rate=spec[3], max_ni_rho=max(m.ni_rho.values()),
                   max_link_rho=max(m.link_rho.values()))
        for k in ("uni", "copy", "compl", "ni_lastvc"):
            row[f"{k}_meas"], row[f"{k}_pred"] = meas.get(k), pred.get(k)
        rows.append(row)
        print(f"{row['run']:28s} ni {row['max_ni_rho']:.2f} link "
              f"{row['max_link_rho']:.2f}  compl {meas.get('compl', 0):8.1f} / "
              f"{pred.get('compl', float('inf')):7.1f}   uni "
              f"{meas.get('uni', 0):7.1f} / {pred.get('uni', float('inf')):6.1f}",
              flush=True)
    t = pd.DataFrame(rows)
    if csv:
        t.to_csv(csv, index=False, float_format="%.4g")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", type=int, default=4, help="N x N mesh")
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--csv", default=os.path.join(RESULTS,
                                                  "garnet_synth_mc.csv"))
    a = ap.parse_args()
    if a.compare:
        compare(a.csv)
        return
    with ThreadPoolExecutor(a.j) as ex:
        for msg in ex.map(lambda s: run(s, a.dry), runs()):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
