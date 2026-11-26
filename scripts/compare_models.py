#!/usr/bin/env python3
"""BMQ (with and without the high-load correction, bmq.VC_HOLD) against
the Mandal and Moadeli baselines (baselines.py) on one mesh size: the
coherence runs (traffic fitted per node, cached as bmq_fit.pkl next to
each trace) and the synthetic runs (model inputs = gem5 options; measured
values from run_synth*.py --compare).

usage: compare_models.py [--mesh N] [-j N]
  -> results[/mesh<N>]/model_compare.csv, one row per run and model
"""

import argparse
import copy
import glob
import os
import pickle
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

import mesh_cfg                     # reads --mesh
import baselines
import bmq
import fit_params

KEYS = ("uni", "copy", "compl")
MODELS = ("bmq", "bmq_nocorr", "bmq_A", "mandal", "moadeli")
# bmq_A: fix A (guide 7.10), p_b fitted at the NI service timescale
# (fit_pbsvc.py -> bmq_pbsvc.pkl); coherence runs only


def predict(p, mesh):
    """{model: (prediction dict, saturated)} for one Params."""
    out = {}
    for name in MODELS:
        bmq.set_mesh(*mesh)
        bmq.VC_HOLD = name != "bmq_nocorr"
        if name == "bmq_A":
            if getattr(p, "pb_svc", None) is None:
                continue
            q = copy.copy(p)
            q.pb = p.pb_svc
            m = bmq.Model(q)
            sat = m.saturated()
            out[name] = ({} if sat else m.summary(), sat)
        elif name.startswith("bmq"):
            m = bmq.Model(p)
            sat = m.saturated()
            out[name] = ({} if sat else m.summary(), sat)
        else:
            out[name] = getattr(baselines, name)(p)
    bmq.VC_HOLD = True
    return out


def fitted(run):
    cache = os.path.join(run, "bmq_fit.pkl")
    if os.path.exists(cache) and os.path.getmtime(cache) > \
            os.path.getmtime(os.path.join(run, "trace.csv.gz")):
        with open(cache, "rb") as f:
            return pickle.load(f)
    _, node, meas = fit_params.fit(run)
    res = ((bmq.ROWS, dict(bmq.LAYERS)), node, meas)
    with open(cache, "wb") as f:
        pickle.dump(res, f)
    return res


def rows(run, kind, mesh, p, meas):
    out = []
    for name, (pred, sat) in predict(p, mesh).items():
        r = dict(run=run, kind=kind, mesh=mesh[0], model=name, saturated=sat)
        for k in KEYS:
            r[f"{k}_meas"], r[f"{k}_pred"] = meas.get(k), pred.get(k)
        out.append(r)
    return out


def coherence(run):
    mesh, node, meas = fitted(run)
    f = os.path.join(run, "bmq_pbsvc.pkl")
    if os.path.exists(f):
        with open(f, "rb") as fh:
            node.pb_svc = pickle.load(fh)
    kind = "standard" if os.path.basename(run).startswith(("p-", "s4-")) else \
        "sweep4B" if os.path.basename(run).startswith("nsweep") else "sweep"
    return rows(os.path.relpath(run, mesh_cfg.ROOT), kind, mesh, node, meas)


def synthetic():
    import run_synth as ru
    import run_synth_mc as rm
    mesh_cfg.use_mesh(bmq)
    mesh = (bmq.ROWS, dict(bmq.LAYERS))
    out = []
    t = pd.read_csv(os.path.join(mesh_cfg.RESULTS, "garnet_synth_mc.csv"))
    for _, r in t.iterrows():
        p = rm.params(int(r.vnet), r.alpha, r.pb, r.rate, mesh=mesh_cfg.MESH)
        meas = {k: r[f"{k}_meas"] for k in KEYS}
        out += rows("synth_mc/" + r.run, "synth_mc", mesh, p, meas)
    uni = "garnet_synth.csv" if mesh_cfg.MESH > 4 else "garnet_synth_linkmodels.csv"
    t = pd.read_csv(os.path.join(mesh_cfg.RESULTS, uni))
    for _, r in t.iterrows():
        name = r["run"] if "run" in r else r["bench"]
        pat, v, _ = name.rsplit("-", 2)
        p = ru.params(pat, -1 if v == "vmix" else int(v[1:]), r.rate,
                      mesh=mesh_cfg.MESH)
        meas = {"uni": r["uni_meas"] if "uni_meas" in r else r["uni_adj"]}
        out += rows("synth/" + name, "synth_uni", mesh, p, meas)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=int, default=4)
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--with-sweeps", action="store_true",
                    help="include the coherence microbenchmark sweeps "
                         "(default only on 4x4; dropped on 6x6/8x8)")
    a = ap.parse_args()
    pre = ("sweep-", "nsweep-", "p-", "s4-") \
        if (a.with_sweeps or a.mesh == 4) else ("p-", "s4-")
    runs = sorted(d for d in glob.glob(os.path.join(mesh_cfg.RESULTS, "MO*", "*"))
                  + glob.glob(os.path.join(mesh_cfg.RESULTS, "ME*", "*"))
                  if os.path.basename(d).startswith(pre)
                  and os.path.exists(os.path.join(d, "eject_trace.csv.gz")))
    # skip runs whose last runs.log entry is not "ok" (e.g. a 6 h timeout
    # leaves a truncated trace.csv.gz that cannot be fitted)
    last = {}
    try:
        for ln in open(os.path.join(mesh_cfg.RESULTS, "runs.log")):
            f = ln.split()
            if len(f) >= 5:
                last[(f[2], f[3])] = f[4]
    except OSError:
        pass
    bad = [d for d in runs if last.get((os.path.basename(os.path.dirname(d)),
                                        os.path.basename(d)), "ok") != "ok"]
    for d in bad:
        print("skip (not ok in runs.log):", os.path.relpath(d, mesh_cfg.ROOT))
    runs = [d for d in runs if d not in bad]
    out = synthetic()
    with ProcessPoolExecutor(a.j) as ex:
        for r in ex.map(coherence, runs):
            out += r
            print(r[0]["run"], flush=True)
    csv = os.path.join(mesh_cfg.RESULTS, "model_compare.csv")
    pd.DataFrame(out).to_csv(csv, index=False, float_format="%.4g")
    print("wrote", csv)


if __name__ == "__main__":
    main()
