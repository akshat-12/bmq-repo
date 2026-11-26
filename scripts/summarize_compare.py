#!/usr/bin/env python3
"""Error table from compare_models.py: per mesh, workload kind and model,
mean |error| of multicast completion (unicast latency for the unicast
synthetic runs) on the runs gem5 did not saturate, how many of those the
model wrongly calls saturated, and how many gem5 saturations it predicts.

usage: summarize_compare.py [MESH ...]   -> results/model_compare_summary.csv
"""

import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEM5_SAT = 200          # synthetic runs: gem5 latency above this = saturated
KINDS = ("standard", "sweep", "sweep4B", "synth_mc", "synth_uni")


def load(mesh):
    d = os.path.join(ROOT, "results") if mesh == 4 else \
        os.path.join(ROOT, "results", f"mesh{mesh}")
    f = os.path.join(d, "model_compare.csv")
    return pd.read_csv(f) if os.path.exists(f) else None


def table(t):
    out = []
    for (mesh, kind, model), g in t.groupby(["mesh", "kind", "model"]):
        key = "uni" if kind == "synth_uni" else "compl"
        meas, pred = g[f"{key}_meas"], g[f"{key}_pred"]
        gsat = meas >= GEM5_SAT if kind.startswith("synth") else meas < 0
        ok = ~gsat & meas.notna()
        e = (100 * (pred - meas) / meas)[ok & ~g.saturated]
        out.append(dict(mesh=mesh, kind=kind, model=model, runs=int(ok.sum()),
                        mean_abs_err=e.abs().mean(), median_abs_err=e.abs().median(),
                        mean_signed_err=e.mean(),
                        false_sat=int((ok & g.saturated).sum()),
                        gem5_sat=int(gsat.sum()),
                        sat_caught=int((gsat & g.saturated).sum())))
    return pd.DataFrame(out)


def main():
    meshes = [int(m) for m in sys.argv[1:]] or [4, 6, 8]
    t = pd.concat([x for x in map(load, meshes) if x is not None])
    s = table(t)
    s["kind"] = pd.Categorical(s.kind, KINDS, ordered=True)
    s = s.sort_values(["kind", "mesh", "model"])
    s.to_csv(os.path.join(ROOT, "results", "model_compare_summary.csv"),
             index=False, float_format="%.2f")
    pd.set_option("display.width", 200)
    print(s.to_string(index=False, float_format=lambda x: f"{x:.1f}"))


if __name__ == "__main__":
    main()
