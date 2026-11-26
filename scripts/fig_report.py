"""Figures and numbers for docs/paper/bmq_report.tex."""
import os, sys
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.ticker
import os; ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = f"{ROOT}/results"
OUT = f"{ROOT}/paper/figs"
plt.rcParams.update({"font.size": 7, "axes.linewidth": .6, "lines.linewidth": 1.1,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "pdf.fonttype": 42, "legend.frameon": False})
C = {"bmq": "#1f5aa6", "bmq_nocorr": "#7aa6d8", "mandal": "#e08a1e", "moadeli": "#b03a3a", "gem5": "k"}
NAME = {"bmq": "BMQ (corr.)", "bmq_nocorr": "BMQ", "mandal": "Mandal", "moadeli": "Moadeli"}
D = {4: pd.read_csv(f"{R}/model_compare.csv"), 6: pd.read_csv(f"{R}/mesh6/model_compare.csv"),
     8: pd.read_csv(f"{R}/mesh8/model_compare.csv")}
def err(d, k="compl"):
    d = d.copy(); d["e"] = 100 * (d[f"{k}_pred"] - d[f"{k}_meas"]) / d[f"{k}_meas"]; return d

# ---- Fig 1: copy schedule
sys.path.insert(0, f"{ROOT}/scripts"); sys.argv = ["x"]
import mesh_cfg, bmq
tau = bmq.ni_schedule(15, 1, 1) if hasattr(bmq, "ni_schedule") else None
print("schedule", tau)
s_ = np.array(tau[1])
fig, ax = plt.subplots(figsize=(3.4, 1.7))
j = np.arange(1, 16)
ax.step(j, s_, where="post", color=C["bmq"], label="BMQ schedule $s_j$ (V=4, R=5)")
ax.axhline(0, color=C["moadeli"], label="Moadeli (parallel ports)")
ax.axhline(s_.mean(), color=C["mandal"], ls="--", label="Mandal (mean wait)")
ax.set_xlabel("copy index $j$ ($K{=}15$, 1 flit)"); ax.set_ylabel("cycles after first copy")
ax.legend(loc="upper left", fontsize=6); fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_schedule.pdf")

# ---- Fig 2: predicted vs measured, 4x4 coherence runs (no correction)
d = err(D[4]); d = d[d.kind.isin(["standard", "sweep", "sweep4B"]) & ~d.saturated & d.compl_pred.notna()]
fig, ax = plt.subplots(figsize=(3.4, 2.6))
mk = {"standard": "o", "sweep": "s", "sweep4B": "^"}
for m in ["moadeli", "mandal", "bmq_nocorr"]:
    for k in mk:
        g = d[(d.model == m) & (d.kind == k)]
        ax.scatter(g.compl_meas, g.compl_pred, s=8, marker=mk[k], color=C[m], alpha=.8, lw=0,
                   label=NAME[m] if k == "standard" else None)
lim = [15, 300]; ax.plot(lim, lim, "k-", lw=.5); ax.plot(lim, [x * 1.2 for x in lim], "k:", lw=.4); ax.plot(lim, [x / 1.2 for x in lim], "k:", lw=.4)
ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlim(lim); ax.set_ylim(lim)
ax.set_xticks([20,50,100,200]); ax.set_yticks([20,50,100,200]); ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter()); ax.get_yaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter()); ax.minorticks_off()
ax.set_xlabel("gem5 completion latency (cycles)"); ax.set_ylabel("model prediction (cycles)")
ax.legend(loc="upper left", fontsize=6); fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_scatter.pdf")

# ---- Fig 3: high load, 4x4
gs = pd.read_csv(f"{R}/garnet_synth_mc.csv")
b0 = D[4][(D[4].kind == "synth_mc")]
def get(m):
    x = b0[b0.model == m].copy(); x["run"] = x.run.str.replace("synth_mc/", "", regex=False)
    return x.set_index("run")
n, c = get("bmq_nocorr"), get("bmq")
fig, ax = plt.subplots(1, 2, figsize=(7.1, 1.8))
a = gs[(gs.vnet == 0) & (gs.alpha == .05) & (gs.pb == 0)].sort_values("rate")
ax[0].plot(a.rate, a.compl_meas, "ko-", ms=3, label="gem5")
ax[0].plot(a.rate, n.compl_pred.reindex(a.run).values, "s--", ms=3, color=C["bmq_nocorr"], label="BMQ")
ax[0].plot(a.rate, c.compl_pred.reindex(a.run).values, "^-", ms=3, color=C["bmq"], label="BMQ + correction")
ax[0].set_yscale("log"); ax[0].set_xlabel("injection rate (msgs/node/cycle)"); ax[0].set_ylabel("completion (cycles)")
ax[0].set_title("config. A: 1-flit, $\\alpha$=0.05", fontsize=7); ax[0].legend(fontsize=6)
u = gs[gs.compl_meas < 200].copy()          # gem5 not saturated
u["e0"] = 100 * (n.compl_pred.reindex(u.run).values - u.compl_meas) / u.compl_meas
u["e1"] = 100 * (c.compl_pred.reindex(u.run).values - u.compl_meas) / u.compl_meas
ax[1].axhline(0, color="k", lw=.4)
ax[1].scatter(u.max_link_rho, u.e0, s=10, color=C["bmq_nocorr"], marker="s", label="BMQ")
ax[1].scatter(u.max_link_rho, u.e1, s=10, color=C["bmq"], marker="^", label="BMQ + correction")
ax[1].set_xlabel("busiest-link utilization (model)"); ax[1].set_ylabel("signed error (%)"); ax[1].legend(fontsize=6)
ax[1].set_title("all unsaturated runs (17)", fontsize=7)
fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_highload.pdf")
print("highload bins")
u["bin"] = pd.cut(u.max_link_rho, [0, .25, .45, 1])
print(u.groupby("bin").agg(n=("e0", "size"), nocorr=("e0", lambda x: x.abs().mean()), corr=("e1", lambda x: x.abs().mean())).round(1))
print("signed", u.e0.mean().round(1), u.e1.mean().round(1), "abs", u.e0.abs().mean().round(1), u.e1.abs().mean().round(1), "n", len(u))
g2 = gs.copy(); g2["gsat"] = g2.compl_meas >= 200
g2["m0"] = ~n.compl_pred.reindex(g2.run).notna().values; g2["m1"] = ~c.compl_pred.reindex(g2.run).notna().values
print("sat gem5", g2.gsat.sum(), "caught nocorr", (g2.gsat & g2.m0).sum(), "corr", (g2.gsat & g2.m1).sum())
print(g2[g2.gsat].groupby(["vnet", "alpha", "pb"]).agg(n=("run", "size"), minrate=("rate", "min")))

# ---- Fig 4: multi-mesh
fig, ax = plt.subplots(1, 2, figsize=(7.1, 1.8))
w = .2
for i, m in enumerate(["bmq", "bmq_nocorr", "mandal", "moadeli"]):
    v = []
    for M in (4, 6, 8):
        x = err(D[M]); x = x[(x.kind == "standard") & (x.model == m) & ~x.saturated & x.compl_pred.notna()]
        v.append(x.e.abs().mean())
    ax[0].bar(np.arange(3) + (i - 1.5) * w, v, w, color=C[m], label=NAME[m])
ax[0].set_xticks(range(3)); ax[0].set_xticklabels(["4x4", "6x6", "8x8"]); ax[0].set_ylabel("mean |error| (%)")
ax[0].set_title("standard benchmarks (20 runs/mesh)", fontsize=7); ax[0].set_ylim(0, 66); ax[0].legend(fontsize=6, ncol=4, loc="upper left", columnspacing=.8, handlelength=1)
S = pd.read_csv(f"{R}/model_compare_summary.csv")
for i, m in enumerate(["bmq_nocorr", "bmq"]):
    v = [S[(S.mesh == M) & (S.kind == "synth_mc") & (S.model == m)].mean_abs_err.iloc[0] for M in (4, 6, 8)]
    ax[1].bar(np.arange(3) + (i - .5) * .3, v, .3, color=C[m], label=NAME[m])
    print("synth_mc", m, v)
for M, t in zip(range(3), ("2 vs 9 of 14", "2 vs 7 of 14", "2 vs 7 of 19")):
    ax[1].text(M, 1, t, ha="center", fontsize=5.5, color="w")
ax[1].set_xticks(range(3)); ax[1].set_xticklabels(["4x4", "6x6", "8x8"]); ax[1].set_ylabel("mean |error| (%)")
ax[1].set_title("synthetic multicast, unsaturated runs", fontsize=7); ax[1].set_ylim(0, 17); ax[1].legend(fontsize=6, ncol=2, loc="upper left")
fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_meshes.pdf")

# ---- stats tables
print("\nSTATS")
for M in (4, 6, 8):
    for k in ["standard", "sweep", "sweep4B", "synth_mc", "synth_uni"]:
        x = err(D[M], "uni" if k == "synth_uni" else "compl"); x = x[x.kind == k]
        if x.empty: continue
        for m in ["bmq_nocorr", "bmq", "mandal", "moadeli"]:
            y = x[x.model == m]; ok = y[~y.saturated & y.e.notna()]
            print(M, k, m, "n", len(ok), "mean|e|", round(ok.e.abs().mean(), 1), "med", round(ok.e.abs().median(), 1),
                  "signed", round(ok.e.mean(), 1), "falsesat", int((y.saturated & (y.compl_meas if k != "synth_uni" else y.uni_meas) < 200).sum()) if k.startswith("synth") else int(y.saturated.sum()))
x = err(D[4]); x = x[(x.kind == "standard") & (x.model == "bmq_nocorr")]
x["copy_e"] = 100 * (x.copy_pred - x.copy_meas) / x.copy_meas
x["uni_e"] = 100 * (x.uni_pred - x.uni_meas) / x.uni_meas
print("4x4 std uni |e|", x.uni_e.abs().mean().round(1), "copy |e|", x.copy_e.abs().mean().round(1))
print(x.sort_values("e")[["run", "e"]].head(4).to_string(), "\n", x.sort_values("e")[["run", "e"]].tail(2).to_string())

# ======== split results figures ========
def lab(r):
    b = r.split("/")[-1].replace("water-nsquared", "water").replace("streamcluster", "stream").replace("blackscholes", "blacksch.").replace("fluidanimate", "fluid.").replace("swaptions", "swapt.").replace("ocean-cont", "ocean").replace("lu-cont", "lu").replace("s4-", "").replace("p-", "")
    return ("M " if "MESI" in r else "H ") + b
fig, ax = plt.subplots(1, 2, figsize=(7.1, 1.95), gridspec_kw={"width_ratios": [2.3, 1]})
x = err(D[4]); x = x[(x.kind == "standard")]
order = x[x.model == "bmq_nocorr"].sort_values(["run"]).run.tolist()
order = sorted(order, key=lambda r: ("MESI" not in r, r))
pos = {r: i for i, r in enumerate(order)}
for m, mk in (("moadeli", "v"), ("mandal", "s"), ("bmq_nocorr", "o")):
    y = x[(x.model == m) & ~x.saturated & x.e.notna()]
    ax[0].scatter([pos[r] for r in y.run], y.e, s=11, marker=mk, color=C[m], label=NAME[m], lw=0, zorder=3)
    ys = x[(x.model == m) & x.saturated]
    if len(ys): ax[0].scatter([pos[r] for r in ys.run], [-98] * len(ys), s=14, marker="x", color=C[m], lw=.8)
ax[0].axhline(0, color="k", lw=.4); ax[0].axvline(9.5, color="gray", lw=.4, ls=":")
ax[0].set_xticks(range(len(order))); ax[0].set_xticklabels([lab(r) for r in order], rotation=70, ha="right", fontsize=5.5)
ax[0].set_ylim(-100, 40); ax[0].set_ylabel("signed error (%)")
ax[0].set_title("20 standard benchmarks (M = MESI, H = hammer; x = false saturation)", fontsize=7)
ax[0].legend(fontsize=6, loc="lower left", ncol=3)
d = err(D[4]); d = d[d.kind.isin(["standard", "sweep", "sweep4B"]) & ~d.saturated & d.compl_pred.notna()]
for m in ["moadeli", "mandal", "bmq_nocorr"]:
    for k in mk_ if False else {"standard": "o", "sweep": "s", "sweep4B": "^"}:
        g = d[(d.model == m) & (d.kind == k)]
        ax[1].scatter(g.compl_meas, g.compl_pred, s=7, marker={"standard": "o", "sweep": "s", "sweep4B": "^"}[k], color=C[m], alpha=.8, lw=0)
lim = [15, 300]; ax[1].plot(lim, lim, "k-", lw=.5); ax[1].plot(lim, [v * 1.2 for v in lim], "k:", lw=.4); ax[1].plot(lim, [v / 1.2 for v in lim], "k:", lw=.4)
ax[1].set_xscale("log"); ax[1].set_yscale("log"); ax[1].set_xlim(lim); ax[1].set_ylim(lim)
for a_ in (ax[1].xaxis, ax[1].yaxis):
    a_.set_major_locator(matplotlib.ticker.FixedLocator([20, 50, 100, 200])); a_.set_major_formatter(matplotlib.ticker.ScalarFormatter()); a_.set_minor_locator(matplotlib.ticker.NullLocator())
ax[1].set_xlabel("gem5 completion (cycles)"); ax[1].set_ylabel("model (cycles)")
ax[1].set_title("standard + sweeps (62 runs)", fontsize=7)
fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_coh.pdf")

# synthetic
fig, ax = plt.subplots(1, 3, figsize=(7.1, 1.75))
U = D[4][D[4].kind == "synth_uni"].copy(); U["rate"] = U.run.str.extract(r"-i([\d.]+)$")[0].astype(float)
for a_, pat in zip(ax[:2], ("uniform_random", "transpose")):
    g = U[U.run.str.contains(pat + "-vmix")]
    for m, mk in (("moadeli", "v"), ("mandal", "s"), ("bmq_nocorr", "o")):
        h = g[g.model == m].sort_values("rate")
        a_.plot(h.rate, h.uni_pred, mk + "-", ms=2.5, color=C[m], label=NAME[m], lw=.9)
    h = g[g.model == "bmq_nocorr"].sort_values("rate")
    a_.plot(h.rate, h.uni_meas, "k-", marker="o", ms=2, mfc="w", label="gem5", lw=.9)
    a_.set_yscale("log"); a_.set_xlabel("injection rate (pkts/node/cycle)"); a_.set_title(pat.replace("_", " ") + ", mixed 1/5-flit", fontsize=7)
ax[0].set_ylabel("unicast latency (cycles)"); ax[0].legend(fontsize=5.5, loc="upper left")
for a_ in ax[:2]: a_.set_ylim(8, 1.5e4)
ax[2].set_ylim(8, 5e4)
M = D[4][D[4].kind == "synth_mc"].copy()
gsA = gs[(gs.vnet == 0) & (gs.alpha == .05) & (gs.pb == 0)]
M["run"] = M.run.str.replace("synth_mc/", "", regex=False)
for m, mk in (("moadeli", "v"), ("mandal", "s"), ("bmq_nocorr", "o")):
    h = M[(M.model == m) & M.run.isin(gsA.run)].merge(gsA[["run", "rate"]], on="run").sort_values("rate")
    ax[2].plot(h.rate, h.compl_pred, mk + "-", ms=2.5, color=C[m], lw=.9)
ax[2].plot(gsA.sort_values("rate").rate, gsA.sort_values("rate").compl_meas, "k-", marker="o", ms=2, mfc="w", lw=.9)
ax[2].set_yscale("log"); ax[2].set_xlabel("injection rate (msgs/node/cycle)"); ax[2].set_ylabel("completion (cycles)")
ax[2].set_title("multicast, 1-flit, $\\alpha$=0.05", fontsize=7)
fig.tight_layout(pad=.3); fig.savefig(f"{OUT}/f_synth.pdf")
