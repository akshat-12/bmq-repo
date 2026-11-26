# BMQ: queueing model of bursty multicast traffic in mesh NoCs

BMQ predicts the completion latency of multicast messages (cache-coherence
invalidations and broadcasts) in a mesh network-on-chip. Where the NoC has no
multicast hardware, as in gem5's Garnet, the source network interface (NI)
sends the copies one after another through a few virtual channels (VCs). BMQ
models this as a batch queue at the NI with bursty (generalized geometric)
arrivals, plus a link-wait model for the mesh. It is validated against
gem5/Garnet on 4x4, 6x6 and 8x8 meshes, with two published models
(Mandal et al., Moadeli and Vanderbauwhede) re-implemented as baselines.

| Mesh | BMQ | Mandal | Moadeli |
|---|---|---|---|
| 4x4, standard benchmarks | **6.4 %** | 24.5 % | 34.3 % |
| 6x6 | **13.4 %** | 34.5 % | 48.2 % |
| 8x8 | **17.6 %** | 37.3 % | 37.1 % |

Mean error of multicast completion latency on 20 runs per mesh (10 SPLASH-4 and
PARSEC benchmarks under two coherence protocols). The paper also covers synthetic traffic at high
load and a correction for it.

## Layout

| Path | Contents |
|---|---|
| `scripts/bmq.py` | The model |
| `scripts/baselines.py`, `moadeli_baseline.py` | Mandal and Moadeli models on BMQ's inputs |
| `scripts/run_suite.py`, `run_synth*.py`, `run_meshes.sh` | Run gem5 benchmarks and synthetic traffic |
| `scripts/fit_params.py`, `fit_pbsvc.py` | Fit model inputs to a gem5 trace |
| `scripts/compare_models.py`, `summarize_compare.py` | Model vs. gem5 error tables |
| `scripts/fig_report.py` | Regenerates the paper figures from `results/` |
| `patches/gem5-garnet-multicast.patch` | Our gem5 changes: per-message trace hook, synthetic multicast tester, partial mesh |
| `benchmarks/` | Our microbenchmark, PARSEC hooks, SPLASH-4 build changes |
| `results/` | Final comparison CSVs (per run and model) |
| `paper/` | `bmq_report.tex` / `.pdf` and its figures |
| `docs/` | `model_guide.md` (full derivation and code walkthrough), `running_one_benchmark.md`, `data_collection.md` |

## Reproducing

The model and figures need only Python 3 with numpy, pandas and matplotlib:

    python3 scripts/fig_report.py        # rebuilds paper/figs/ from results/
    python3 scripts/summarize_compare.py # error table from results/*/model_compare.csv

Re-running the simulations needs gem5 (about 12 GB built) and the benchmarks:

1. Check out gem5 v26.0.0.0 into `gem5/` and apply the patch:
   `git -C gem5 apply ../patches/gem5-garnet-multicast.patch`.
   Build `X86_MESI_hammer` (both coherence protocols) and `Garnet_standalone` (synthetic traffic).
2. Fetch SPLASH-4 into `benchmarks/splash4/` and copy in `benchmarks/splash4-changes/`;
   fetch PARSEC 3.0 and build with `scripts/build_parsec.sh`; build `benchmarks/micro` with `make`.
3. Run and validate (each step skips finished runs):

       python3 scripts/run_suite.py -j 6 [--mesh 6|8]   # gem5 runs and traces
       python3 scripts/run_synth_mc.py [--mesh N]       # synthetic multicast sweeps
       python3 scripts/compare_models.py [--mesh N]     # BMQ and baselines vs. gem5
       python3 scripts/summarize_compare.py             # error table

Raw traces (tens of GB) are not in the repo; `results/` keeps only the per-run
comparison tables. `docs/running_one_benchmark.md` walks through a single run by hand.

## Model in one paragraph

A message arrives at the source NI as a batch of K copies. The VC and credit rules fix when each copy
leaves (`tau_j`); the NI queue, with bursty arrivals, gives the wait of the first copy; each link adds an M/G/1-style
wait reduced by a merge factor; router constants are derived from the pipeline, not fitted. Completion latency is
the maximum over copies. At high injection, a correction accounts for VCs held at the NI and finite router
buffers (`bmq.VC_HOLD`, on by default). Details: `docs/model_guide.md`, `paper/bmq_report.pdf`.

## Limitations

Validated on Garnet only; XY routing, one traffic class per virtual network, source replication.
MOESI_hammer on large meshes is the main error source, because its broadcast traffic is uneven in time.
