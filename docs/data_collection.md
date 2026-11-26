# Data collection: multicast coherence traffic in a mesh NoC

## 1. Purpose

We are building an analytical model of bursty multicast traffic in a
network-on-chip (NoC). The data collection serves two purposes:

1. **Motivation:** show that multicast is a significant and costly part of
   real on-chip traffic, and that it is bursty.
2. **Validation:** provide ground truth (latencies, arrival times, fan-outs)
   to check the analytical model against a cycle-accurate simulator.

Cache-coherence protocols are the natural source of multicast on chip:
directories send invalidations to all sharers, and broadcast protocols send
every request to every cache.

**Collected:** 28 runs (14 benchmarks × 2 coherence protocols), covering
181 million coherence messages in the measured regions. Every run keeps its
full message-level trace (4.3 GB compressed in total).

## 2. Motivation

### Why multicast matters on chip

**Cache coherence is a built-in source of multicast.** A directory protocol
invalidates every sharer of a line before a write; a broadcast protocol
sends every miss to every cache. Many NoCs, including gem5's Garnet,
implement a multicast as **source replication**: the sending NI makes one
copy per destination and injects them one after another. Our data shows
what that costs (section 8):

- **Multicast inflates traffic far beyond its message count.** Under the
  broadcast protocol, about 5% of coherence messages become 42-47% of all
  packets in the network, on every one of our workloads.
- **It delays the operations that wait on it.** A write can't complete
  until every sharer is invalidated, so what matters is when the *last*
  copy arrives. Under MOESI_hammer that is 6.4-11.8 cycles (30-55%) later
  than the average copy, mostly from serializing 15 copies at one NI.
- **It is bursty.** Arrivals are close to Poisson within a program phase,
  but whole runs alternate busy and nearly silent phases (CV up to 55).
  And each multicast is itself a burst: K packets created in the same cycle.

### Why an analytical model

Cycle-accurate simulation answers these questions, but slowly.

- **Simulation cost:** our 28 runs took 7.8 hours of simulation, a median
  of 17 minutes each, for at most 50M cycles of parallel execution per
  benchmark.
- **Model cost:** a first prototype of our model evaluates the same
  network in **38 ms** (MOESI_hammer Barnes: 38 ms vs. 870 s simulated,
  about 23,000× faster).

Design-space exploration is the motivating use: sweeping topologies,
protocols, fan-outs and loads, or putting a NoC latency estimate inside a
faster, higher-level simulator. Both need a model that is fast *and*
accurate for this traffic.

### The gap in existing models

| Model | Multicast | Bursty traffic | Fits source-replicated multicast? |
|---|---|---|---|
| Moadeli & Vanderbauwhede, IPDPS 2009 | Yes | No (Poisson) | No: assumes copies leave through parallel router ports, with no serialization |
| Mandal et al., 2020 / 2021 (GGeo) | No (unicast only) | Yes | Only by treating copies as independent packets: no batch size, no copy order |
| Shahrabi et al., SAC 2001 (hypercube broadcast) | Broadcast via unicast tree | No (Poisson) | No: models only multiple-port routers; names single-port (sequential) injection as an adaptation it doesn't model |

Our literature search found no analytical model of source-replicated
multicast in a mesh NoC, with or without bursty traffic. Fan-out as batch
service does appear in ATM multicast-switch queueing (Hui & Renner, 1990),
which we treat as related prior work.

**Preliminary evidence** (one trace, MOESI_hammer p-swaptions;
multicast completion latency vs. Garnet):

| Model | Error |
|---|---|
| Moadeli | -52% |
| Mandal-style GGeo | -27% |
| Our batch-aware model (prototype) | -15% |

The published models miss the cost of the last copy. Validating our
model across loads and workloads is the project's next phase.

## 3. Simulation setup

| Parameter | Value | Why |
|---|---|---|
| Simulator | gem5 v26.0, syscall-emulation (SE) mode | Cycle-accurate; SE runs a single program without booting an OS, so runs are fast |
| ISA, cores | X86, 16 in-order cores (`X86TimingSimpleCPU`), 2 GHz | Simple, fast core model; 16 cores give a 4×4 mesh |
| Memory system | Ruby, 32 kB 8-way L1 I/D per core; 16 shared L2 banks of 256 kB (MESI); 16 directories | Ruby models coherence messages explicitly |
| Network | Garnet 3.0, 4×4 mesh, XY routing, 16-byte flits | Cycle-level router and link model; the standard NoC simulator in gem5 |
| Protocols | **MESI_Two_Level** (directory) and **MOESI_hammer** (broadcast) | Two regimes: multicast only when shared data is written (MESI), and on every miss (hammer) |
| Build | One binary with both protocols (`build/X86_MESI_hammer/gem5.opt`) | Same build for both; protocol chosen per run with `--protocol` |

`--access-backing-store` is required. In SE mode, gem5 serves some of the
program's system calls by reading memory directly, and Garnet can't serve
those reads while a cache line is in flight.

## 4. Instrumentation

We added a small trace hook to Garnet's network interface (NI), in
`NetworkInterface.cc` and `GarnetNetwork.cc`, enabled with
`--garnet-trace=trace.csv.gz`. It writes two files per run.

**`trace.csv.gz`: one line per coherence message**, logged *before* Garnet
splits a multicast into one unicast packet per destination. This is the
only place the original destination set is still visible.

| Column | Meaning |
|---|---|
| `enq_cyc` | Cycle the coherence controller created the message |
| `start_cyc`, `end_cyc` | Cycles the NI started injecting the first copy and finished queueing the last |
| `src`, `dests`, `ndest` | Sending node, all destinations, fan-out K (K > 1 means multicast) |
| `vnet`, `type` | Virtual network and protocol message type (`GETS`, `INV`, `ACK`, ...) |
| `bytes`, `flits` | Size: 1 flit (control) or 5 flits (data) |
| `pkts` | Garnet packet id of each copy |

The header lines give the node map (L1 caches 0-15, L2 banks 16-31,
directories 32-47 under MESI; directories 16-31 under hammer) and the
region-of-interest (ROI) markers.

**`eject_trace.csv.gz`:** for every delivered packet, its id, delivery
cycle and destination. Joining the two files on packet id gives the latency
of every copy of every message.

**Region of interest.** Each benchmark marks its parallel phase by calling
gem5's `m5_dump_reset_stats`, which the hook records as `# reset_stats`
lines. The analysis uses only the traffic between those markers.

**ROI cap.** `--roi-max-cycles` ends a run a fixed number of cycles after
the ROI begins; we used 50M. This bounds simulation time for long
benchmarks and leaves the trace complete.

**Timestamps.** `enq_cyc` is the message's *creation* time, which includes
a fixed protocol delay before the NI sees it. For example, MOESI_hammer L1
replies are held 10 cycles (`cache_response_latency`). For latencies
measured from NI arrival, we estimate that time without changing gem5.
For each message group (virtual network, type, sender kind), the delay is
the group's minimum `start - enq`, or minimum + 1 if that is more common.
NI arrival is then `enq + min(delay, start - enq)`. In most groups,
99-100% of messages share one delay value. The remaining uncertainty is at
most 1 cycle, on MESI's L2 data replies.

## 5. Workloads

### Benchmarks run

| Benchmark | Suite | Sharing pattern | Input |
|---|---|---|---|
| `s4-fft` | SPLASH-4 | All-to-all transpose phases | 64K points |
| `s4-lu-cont` | SPLASH-4 | Pivot blocks read by many cores, then overwritten | 512×512 |
| `s4-radix` | SPLASH-4 | Shared prefix sums, all-to-all key writes | 1M keys |
| `s4-ocean-cont` | SPLASH-4 | Nearest-neighbor | 258×258 grid |
| `s4-water-nsquared` | SPLASH-4 | Read by all cores, lock-protected updates | 512 molecules |
| `s4-barnes` | SPLASH-4 | Irregular tree sharing | 16K bodies |
| `p-blackscholes` | PARSEC 3.0 | Mostly private data | simmedium |
| `p-swaptions` | PARSEC 3.0 | Mostly private data | simmedium |
| `p-fluidanimate` | PARSEC 3.0 | Neighbor cells, locks | simmedium |
| `p-streamcluster` | PARSEC 3.0 | Shared centers, barriers | simmedium |
| `micro-prodcons` | **Ours** | One writer, 15 readers | [cohmicro.c](benchmarks/micro/cohmicro.c) |
| `micro-readmostly` | **Ours** | Shared table, occasional writes | |
| `micro-lock` | **Ours** | Contended spinlock | |
| `micro-barrier` | **Ours** | Repeated centralized barrier | |

- **SPLASH-4 and PARSEC** are standard published suites and supply the
  motivation evidence.
- **The microbenchmarks** are our own synthetic kernels, built to produce
  specific sharing patterns. Use them for controlled validation, not as
  evidence of real-world behavior.

Inputs follow each suite's recommended simulation sizes. SPLASH runs use
16 threads. PARSEC runs use 15 worker threads (8 for fluidanimate, which
needs a power of two): in these apps the main thread only spawns and joins
workers, and SE mode needs one core per thread.

### Changes to the benchmark code

None of these change what a program computes.

- **ROI markers.**
  - SPLASH: in the thread-create and join macros.
  - PARSEC: a replacement for its hooks library
    ([hooks.c](benchmarks/parsec-hooks/hooks.c)).
- **Static linking** with gem5's `libm5.a`, since SE mode has no dynamic
  loader.
- **Barnes:** fixed an off-by-one heap overflow in SPLASH's `getparam`
  (`malloc(leng)` became `malloc(leng + 1)`). glibc's overflow check
  aborted the original.
- **Streamcluster:** PARSEC places the serial generation of input points
  inside the ROI, so a 50M-cycle window saw only one active core. The ROI
  now begins at the first parallel phase (`-DGEM5_PARALLEL_ROI`).

### Not run, and why

| Left out | Reason |
|---|---|
| SPLASH-3 (all 14) | Same programs as SPLASH-4; they differ only in synchronization code |
| SPLASH-4 radiosity, raytrace, volrend | Mostly read-only data (little invalidation multicast); slowest to simulate |
| SPLASH-4 fmm, cholesky, water-spatial, ocean-noncont, lu-noncont | Similar pattern to an included program, or a variant of one |
| PARSEC canneal | Its serial netlist parsing ran 99+ minutes without reaching the ROI |
| Other PARSEC apps | Need full-system mode, OpenMP, or have unpredictable thread counts |
| `micro-counter`, `micro-falseshare` | Produce unicast (migratory) traffic only |

## 6. Running the experiments

- **Runner:** [run_suite.py](scripts/run_suite.py) builds each gem5
  command, runs benchmarks in parallel, and logs every run to
  `results/runs.log`. It flags runs where the *simulated program* failed:
  gem5 itself exits 0 in that case.
- **Launching:** runs go in their own systemd user service, so they survive
  restarts of the Claude app (`STATUS.md` has the command).
- **Time per run:** 1-45 minutes; most 12-30 minutes, 4 runs at a time.

**Problems hit, and what fixed them:**

| Problem | Fix |
|---|---|
| Runs killed within seconds | A hidden cryptocurrency miner on the lab machine killed every process whose command line contained "benchmark". The user removed it (an evidence archive was kept for the admin) |
| `fatal: Functional read not implemented` | `--access-backing-store` |
| Barnes crashed (`buffer overflow detected`), but the run was marked ok | Fixed the SPLASH bug; the runner now checks the simulated program's exit status |
| Long benchmarks would take 12-20 hours | 50M-cycle ROI cap, plus a reduced benchmark subset |
| Streamcluster window saw one active core | Moved its ROI start (above); the analysis now also counts active cores |
| Analysis ran out of memory on 300 MB traces | Rewrote it to read traces in chunks (peak ~3 GB) |

## 7. Analysis: four key metrics

All metrics come from the ROI, using
[analyze_traces.py](scripts/analyze_traces.py).

1. **Multicast share.** The fraction of *messages* with more than one
   destination, and the fraction of *packets* they become. Garnet sends
   one packet per destination, so a K-way multicast is one message but K
   packets.
2. **Fan-out K.** The distribution of destinations per multicast. This is
   the batch size the model needs.
3. **Multicast cost.** Mean per-copy latency (creation to delivery of each
   copy) vs. completion latency (creation to delivery of the *last* copy).
   The gap is the price of sending copies one after another.
4. **Burstiness.** Coefficient of variation (CV) of the gaps between
   multicast messages; CV = 1 is Poisson. Measured two ways: over the whole
   ROI, and within active phases (median over 50k-cycle windows with at
   least 200 multicasts).

`results/summary_standard.csv` (SPLASH-4 + PARSEC, 20 rows) and
`results/summary_custom.csv` (our microbenchmarks, 8 rows) hold these and a
few more statistics per run. Per-run details, such as the fan-out histogram
and message types, are in `results/<protocol>/<benchmark>/analysis.json`.

## 8. Results

### 8.1 Multicast is a few messages but many packets

![Multicast share of messages and packets](docs/figures/fig1_multicast_share.png)

- **MOESI_hammer:** about 5% of messages are multicast on *every*
  workload, but they become **42-47% of all packets**, because each
  broadcast is copied 15 times.
- **MESI_Two_Level:** multicast in the standard benchmarks is rare, at
  0.01-2.75% of packets. Barnes is highest, from its tree sharing. Data
  that is mostly private (blackscholes, swaptions) produces almost none.
- **Our microbenchmarks under MESI:** 12-22% of packets. They give a
  controlled middle ground between the two regimes.

So the protocol sets the multicast level (high and steady for broadcast),
while under a directory protocol it depends on the workload.

### 8.2 Fan-out: the batch size

![Fan-out distribution](docs/figures/fig2_fanout.png)

- **MOESI_hammer:** 99.5% of multicasts go to all 15 other caches. K is
  effectively a constant.
- **MESI_Two_Level:** 82.5% of multicasts have K = 2. There is a small
  tail out to full broadcasts (15-16), from widely read lines that get
  written.

The model therefore needs a real fan-out *distribution*, not a single
value and not a geometric shape.

### 8.3 The cost of multicast: waiting for the last copy

![Per-copy vs completion latency](docs/figures/fig3_multicast_cost.png)

Under MOESI_hammer, the last copy of a multicast arrives **6.4-11.8 cycles
after the average copy**: completion is 28-35 cycles, against 20-24 per
copy. The main cause is at the source NI, which injects the 15 copies one
after another. In our traces that takes about 17 cycles from the first copy
to the last.

Completion latency is what coherence waits on: a write stalls until every
sharer is invalidated. So a model that predicts only average packet latency
misses this cost.

(These latencies count from message creation. Directory multicasts carry
only 1-2 cycles of controller delay, so the gap is unaffected.)

### 8.4 Burstiness: near-Poisson within phases, bursty overall

![Burstiness of multicast arrivals](docs/figures/fig4_burstiness.png)

- **Within active phases:** multicast arrivals are close to Poisson, with
  CV 0.5-1.4 for most benchmarks; blackscholes reaches 2.1.
- **Over the whole ROI:** CV reaches **14-55** for FFT, LU, Water,
  blackscholes and swaptions. Programs alternate busy phases with nearly
  silent ones; FFT's transposes are the clearest example.

Two consequences:

- **Model inputs:** the in-phase values, because a queueing model assumes
  stationary traffic.
- **Motivation:** the phase-level burstiness explains why models built on
  average rates fail on real programs.

The microbenchmarks have no phase structure, so the two measures coincide.

### 8.5 Key numbers per benchmark (MOESI_hammer)

| Benchmark | Mcast % msgs | Mcast % pkts | Mean K | Per-copy lat. | Completion lat. | CV in-phase | CV whole ROI |
|---|---|---|---|---|---|---|---|
| s4-barnes | 5.3 | 45.2 | 14.7 | 20.3 | 27.6 | 0.61 | 0.76 |
| s4-fft | 5.4 | 46.0 | 15.0 | 22.6 | 34.0 | 1.38 | 55.3 |
| s4-lu-cont | 4.9 | 43.5 | 14.8 | 21.0 | 31.6 | 1.21 | 14.4 |
| s4-ocean-cont | 5.0 | 44.2 | 15.0 | 21.6 | 32.7 | 1.26 | 2.4 |
| s4-radix | 4.7 | 42.3 | 15.0 | 24.0 | 34.9 | 1.11 | 3.7 |
| s4-water-nsquared | 5.4 | 46.2 | 15.0 | 21.3 | 31.9 | 1.09 | 14.5 |
| p-blackscholes | 5.4 | 45.9 | 15.0 | 19.7 | 30.1 | 2.14 | 15.5 |
| p-fluidanimate | 4.8 | 42.8 | 15.0 | 20.2 | 30.7 | 1.01 | 3.4 |
| p-streamcluster | 5.3 | 45.4 | 15.0 | 20.1 | 30.3 | 1.00 | 10.0 |
| p-swaptions | 5.4 | 46.0 | 15.0 | 20.6 | 31.3 | 1.05 | 29.4 |

Latencies are in cycles. The MESI_Two_Level numbers are in
`results/summary_standard.csv`. Its multicasts are too rare in most
benchmarks to estimate in-phase burstiness.

### 8.6 Two observations for the model

- **Copy spacing.** The NI spaces copies about 1.2 cycles apart, not 1,
  likely from virtual-channel allocation. This is a parameter the model
  needs.
- **Incast.** The replies to a broadcast converge on the requester and
  queue there, even when the network is otherwise idle. In hammer
  swaptions, `ACK`, `ACK_SHARED` and `DATA_SHARED` replies arrive 1.0, 2.0
  and 7.9 cycles beyond zero-load latency. Requests arrive within 0.15.
  This is the mirror image of the source batch.

## 9. Limitations

- **Light network load.** Coherence traffic from in-order cores keeps
  links lightly loaded; the busiest link is below 8% utilization. Queueing
  inside the network barely shows up in these traces. Validation under
  load needs load sweeps.
- **One CPU model.** Out-of-order cores would overlap more misses, likely
  raising load and burstiness.
- **Capped windows.** The SPLASH and PARSEC runs, except FFT, cover the
  first 50M cycles of their parallel region; the microbenchmarks and FFT
  cover all of it.
- **Closed-loop traffic.** A stalled core sends less, so arrivals depend on
  the network's own latency. The traces capture what happened, not an
  independent arrival process.

## 10. Data and reproduction

| Path | Contents |
|---|---|
| `results/<protocol>/<benchmark>/trace.csv.gz`, `eject_trace.csv.gz` | Raw traces: every message and every delivered packet |
| `results/<protocol>/<benchmark>/stats.txt`, `simout.txt`, `cmd.txt` | gem5 statistics (one dump per ROI marker), program output, exact command |
| `results/<protocol>/<benchmark>/analysis.json` | Per-run analysis |
| `results/summary_standard.csv`, `summary_custom.csv`, `summary.csv` | Summary tables |
| `docs/figures/` | The figures above (`scripts/make_figures.py`) |
| `STATUS.md`, `RUNNING.md` | Setup details, launch commands, a walkthrough for running one benchmark by hand |

```bash
python3 scripts/run_suite.py --subset --skip-done -j4      # simulate
python3 scripts/analyze_traces.py                          # analyze
python3 scripts/make_figures.py                            # figures
```
