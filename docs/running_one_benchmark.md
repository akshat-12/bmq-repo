# Running one benchmark by hand

Example: SPLASH-4 FFT with a small input (4,096 points), MESI_Two_Level,
16 cores on the 4x4 Garnet mesh. About 25 s, 0.5 GB of memory.

## 1. The benchmark binary

    cd ~/workspace/Modeling-Project/benchmarks/splash4/Splash-4/fft
    make                                   # already built; rebuilds if needed
    nm FFT | grep m5_dump_reset_stats      # ROI marker is linked in

Two changes from stock SPLASH-4, both in `splash4/`:

- `pthread.m4`: the `CREATE` and `WAIT_FOR_END` macros call
  `m5_dump_reset_stats(0, 0)`, marking the start and end of the parallel
  region (ROI) for gem5
- `Makefile.config`: static linking (`-static`) plus `libm5.a`, since gem5
  SE mode loads the binary directly, with no dynamic loader

## 2. Run gem5

    mkdir -p ~/workspace/Modeling-Project/manual-runs
    ~/workspace/Modeling-Project/gem5/build/X86_MESI_hammer/gem5.opt \
      --outdir=$HOME/workspace/Modeling-Project/manual-runs/MESI_Two_Level/fft-small \
      ~/workspace/Modeling-Project/gem5/configs/deprecated/example/se.py \
      --protocol=MESI_Two_Level --ruby --network=garnet \
      --topology=Mesh_XY --mesh-rows=4 --routing-algorithm=1 \
      --num-cpus=16 --num-dirs=16 --num-l2caches=16 \
      --cpu-type=X86TimingSimpleCPU --cpu-clock=2GHz --ruby-clock=2GHz \
      --mem-size=2GB --access-backing-store \
      --l1d_size=32kB --l1d_assoc=8 --l1i_size=32kB --l1i_assoc=8 \
      --l2_size=256kB --l2_assoc=8 \
      --garnet-trace=trace.csv.gz \
      --cmd=./FFT --options="-p16 -m12"

Run it from the benchmark's folder: the simulated program's working
directory is wherever gem5 was launched.

| Flag | Meaning |
|---|---|
| `gem5.opt` | gem5 binary built with both protocols (`X86_MESI_hammer`) |
| `--outdir` | where outputs go; keep `<protocol>/<bench>` so the analysis finds it |
| `se.py` | syscall-emulation config script: runs one Linux binary, no OS |
| `--protocol` | `MESI_Two_Level` (directory) or `MOESI_hammer` (broadcast) |
| `--ruby --network=garnet` | Ruby cache-coherence model, Garnet cycle-level NoC |
| `--topology=Mesh_XY --mesh-rows=4` | 4x4 mesh; one L1, L2 bank and directory per router |
| `--routing-algorithm=1` | XY routing |
| `--num-cpus/dirs/l2caches=16` | 16 cores, directories, L2 banks |
| `--cpu-type=X86TimingSimpleCPU` | in-order core, one memory access at a time |
| `--access-backing-store` | required in SE mode: syscalls read memory directly, which Garnet cannot serve mid-transfer |
| `--garnet-trace=trace.csv.gz` | our hook: per-message trace (also writes `eject_trace.csv.gz`) |
| `--roi-max-cycles=N` | our hook (optional): stop N cycles after the ROI begins |
| `--cmd`, `--options` | the program and its arguments (`-p16` threads, `-m12` = 2^12 points) |

Ends with: `Exiting @ tick ... because exiting with last active thread context`.

## 3. Look at the outputs

    cd ~/workspace/Modeling-Project/manual-runs/MESI_Two_Level/fft-small
    ls

- `stats.txt`: one statistics dump per ROI marker. FFT has 3: start to
  ROI begin (serial init), the ROI itself, and after the ROI.

      grep -E "^(simTicks|simInsts|hostSeconds) " stats.txt

  Ticks are picoseconds: 2 GHz means 500 ticks per cycle.
- `trace.csv.gz`: header lines (`#`) map node ids to caches and give the
  ROI markers, then one line per coherence message.

      zcat trace.csv.gz | grep "^#" | head -30
      zcat trace.csv.gz | grep -v "^#" | head -5
      zcat trace.csv.gz | grep -v "^#" | awk -F, '$9 > 1' | head -5   # multicasts

  Columns: `enq_cyc` (generated), `start_cyc`/`end_cyc` (injection at the
  NI), `src`, `vnet`, `bytes`, `flits`, `type`, `ndest`, `dests`, `pkts`.
  Node ids: 0-15 L1s, 16-31 L2 banks, 32-47 directories.
- `eject_trace.csv.gz`: `pkt,cyc,ni` for each delivered packet, matched to
  messages through `pkts`.

## 4. Analyze

    cd ~/workspace/Modeling-Project
    python3 scripts/analyze_traces.py --results manual-runs
    python3 -m json.tool manual-runs/MESI_Two_Level/fft-small/analysis.json | less

Writes `analysis.json` next to the trace and `summary*.csv` in
`manual-runs/`.

## 5. Variations

- Broadcast protocol: `--protocol=MOESI_hammer`, and use
  `manual-runs/MOESI_hammer/fft-small` as the outdir
- Full-size FFT: `--options="-p16 -m16"` (about 4 min)
- See the exact command for any benchmark in the sweep:

      python3 scripts/run_suite.py --only '^s4-radix$' --dry-run
