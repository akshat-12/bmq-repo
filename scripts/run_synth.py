#!/usr/bin/env python3
"""High-link-load validation of the network part (UNIFIED_MODEL.md 5.5):
Garnet_standalone synthetic traffic on the same 4x4 Mesh_XY, swept in
injection rate up to saturation. Unicast only (the synthetic tester cannot
multicast), so the NI part is a plain bursty queue and the runs isolate
the link model. Traces go to results/garnet_synth/<run>/ in the same format
as the coherence runs, so `bmq_model.py --protocol garnet_synth` reads them.

On a larger mesh (--mesh N) the rates are scaled so the model's busiest
queue has the same utilization as at the 4x4 rate, results go to
results/mesh<N>/garnet_synth/, and --compare checks them against the model
(from the gem5 options, as run_synth_mc.py). Transpose needs one directory
per router, so a 6x6 mesh (32 directories) runs uniform random only.

usage: run_synth.py [--mesh N] [-j N] [--dry]
       run_synth.py --mesh N --compare [--csv FILE]
"""

import argparse
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from mesh_cfg import MESH, NCPU, NHOME, RESULTS, gem5_mesh_args, rate_scale, \
    use_mesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEM5 = os.path.join(ROOT, "gem5")
BIN = os.path.join(GEM5, "build", "Garnet_standalone", "gem5.opt")
CFG = os.path.join(GEM5, "configs", "example", "garnet_synth_traffic.py")
OUT = os.path.join(RESULTS, "garnet_synth")
SIM_CYCLES = 200_000            # network cycles
TICKS_PER_CYCLE = 500           # 2 GHz; the tester's --sim-cycles is in ticks

# (pattern, vnet): vnet 0 is 1-flit packets, -1 mixes 1-flit (vnets 0, 1)
# and 5-flit (vnet 2). Rates are packets/cycle/node, up to past saturation
RUNS = []
for pattern in ("uniform_random", "transpose"):
    for vnet, rates in ((0, (0.05, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5)),
                        (-1, (0.02, 0.05, 0.08, 0.11, 0.14, 0.17, 0.2))):
        for r in rates:
            RUNS.append((pattern, vnet, r))
CALIB = ("uniform_random", -1, 0.005)   # lightest load (formerly T0 calibration)
MIX = {(1, 0): 1 / 3, (1, 1): 1 / 3, (5, 2): 1 / 3}


def params(pattern, vnet, rate, mesh=MESH):
    """The model's inputs, straight from the gem5 options."""
    import bmq
    n = mesh * mesh
    return bmq.Params(lam=rate, pattern=pattern.replace("_random", ""),
                      packets={(1, 0): 1.0} if vnet == 0 else MIX,
                      sources=tuple(range(n)), dst_base=n,
                      vbuf={0: 1, 1: 1, 2: 4})


def runs():
    """CALIB + RUNS, rates scaled on larger meshes."""
    if MESH == 4:
        return [CALIB] + RUNS
    import bmq
    out = []
    for pattern in ("uniform_random", "transpose"):
        if pattern == "transpose" and NHOME != NCPU:
            continue
        for vnet in (0, -1):
            k = rate_scale(bmq, lambda r, m: params(pattern, vnet, r, m))
            spec = [s for s in [CALIB] + RUNS if s[:2] == (pattern, vnet)]
            out += [(pattern, vnet, float(f"{r * k:.3g}")) for _, _, r in spec]
    return out


def name(pattern, vnet, rate):
    return f"{pattern}-v{'mix' if vnet < 0 else vnet}-i{rate:g}"


def cmd(pattern, vnet, rate, outdir):
    return [BIN, f"--outdir={outdir}", "--redirect-stdout",
            "--redirect-stderr", CFG,
            "--network=garnet", *gem5_mesh_args(),
            f"--synthetic={pattern}", f"--injectionrate={rate}",
            f"--inj-vnet={vnet}",
            # tester on the network clock, so rates are per network cycle
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


def compare(csv):
    """Model (from the gem5 options) vs Garnet's unicast latency."""
    import pandas as pd
    import bmq
    import fit_params
    rows = []
    for spec in runs():
        d = os.path.join(OUT, name(*spec))
        if not os.path.exists(os.path.join(d, "eject_trace.csv.gz")):
            continue
        meas = fit_params.fit(d)[2]
        use_mesh(bmq)
        m = bmq.Model(params(*spec))
        pred = float("inf") if m.saturated() else m.summary()["uni"]
        rows.append(dict(run=name(*spec), pattern=spec[0], vnet=spec[1],
                         rate=spec[2], max_ni_rho=max(m.ni_rho.values()),
                         max_link_rho=max(m.link_rho.values()),
                         uni_meas=meas.get("uni"), uni_pred=pred))
        print(f"{name(*spec):32s} link {rows[-1]['max_link_rho']:.2f}  uni "
              f"{meas.get('uni', 0):8.1f} / {pred:6.1f}", flush=True)
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
    ap.add_argument("--csv", default=os.path.join(RESULTS, "garnet_synth.csv"))
    a = ap.parse_args()
    if a.compare:
        compare(a.csv)
        return
    with ThreadPoolExecutor(a.j) as ex:
        for msg in ex.map(lambda s: run(s, a.dry), runs()):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
