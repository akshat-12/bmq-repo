#!/usr/bin/env python3
"""Run benchmarks under gem5 SE mode (Ruby + Garnet mesh, 4x4 by default)
and collect message-level Garnet traces.

Each run writes to results/<protocol>/<bench>/ (4x4) or
results/mesh<N>/<protocol>/<bench>/ (--mesh N):
    trace.csv.gz   per-message injection trace (see GarnetNetwork trace_file)
    stats.txt      gem5 stats; dumps delimited by the ROI markers
    simout/simerr  program and simulator output
    cmd.txt        exact gem5 command line

usage: run_suite.py [-j N] [--mesh N] [--subset] [--skip-done]
                    [--roi-max-cycles N] [--protocols P,..] [--only REGEX]
                    [--dry-run]

--mesh N: an N x N mesh with N*N cores. Ruby needs a power of two of L2
banks and directories, so a 6x6 mesh gets 32 of each, on routers 0-31
(topology Mesh_XY_Partial); SPLASH runs a power of two of threads.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEM5 = os.path.join(ROOT, "gem5")
GEM5_BIN = os.path.join(GEM5, "build/X86_MESI_hammer/gem5.opt")
SE_PY = os.path.join(GEM5, "configs/deprecated/example/se.py")
S3 = os.path.join(ROOT, "benchmarks/splash3/codes")
S4 = os.path.join(ROOT, "benchmarks/splash4/Splash-4")
PARSEC = os.path.join(ROOT, "benchmarks/parsec-build")
MICRO = os.path.join(ROOT, "benchmarks/micro")

def _mesh():
    """--mesh is needed before the benchmark table below is built."""
    for i, a in enumerate(sys.argv):
        if a.startswith("--mesh="):
            return int(a.split("=")[1])
        if a == "--mesh" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return 4


def pow2_below(n):
    return 1 << (n.bit_length() - 1)


MESH = _mesh()
NCPU = MESH * MESH                 # one core per router
NHOME = pow2_below(NCPU)           # L2 banks and directories
TOPOLOGY = "Mesh_XY" if NHOME == NCPU else "Mesh_XY_Partial"
RESULTS = os.path.join(ROOT, "results") if MESH == 4 else \
    os.path.join(ROOT, "results", f"mesh{MESH}")

# name -> (working dir, binary, args, stdin file or None)
# Inputs follow the SPLASH-3 recommended set where simulation time allows;
# reductions are noted.
def splash(prefix, base, names):
    p = pow2_below(NCPU)           # fft, radix, ocean need a power of two
    d = {
        "barnes": ("barnes", "BARNES", "", f"inputs/n16384-p{p}"),
        "fmm": ("fmm", "FMM", "", f"inputs/input.{p}.16384"),
        "ocean-cont": ("ocean/contiguous_partitions", "OCEAN",
                       f"-p{p} -n258", None),
        "ocean-noncont": ("ocean/non_contiguous_partitions", "OCEAN",
                          f"-p{p} -n258", None),
        "radiosity": ("radiosity", "RADIOSITY",
                      f"-p {p} -ae 5000 -bf 0.1 -en 0.05 -room -batch", None),
        "raytrace": ("raytrace", "RAYTRACE", f"-p{p} -m64 inputs/car.env",
                     None),
        "volrend": ("volrend", "VOLREND", f"{p} inputs/head 8", None),
        "water-nsquared": ("water-nsquared", "WATER-NSQUARED", "",
                           f"inputs/n512-p{p}"),
        "water-spatial": ("water-spatial", "WATER-SPATIAL", "",
                          f"inputs/n512-p{p}"),
        "cholesky": ("cholesky", "CHOLESKY", f"-p{p}", "inputs/tk15.O"),
        "fft": ("fft", "FFT", f"-p{p} -m16", None),
        "lu-cont": ("lu/contiguous_blocks", "LU", f"-p{p} -n512", None),
        "lu-noncont": ("lu/non_contiguous_blocks", "LU", f"-p{p} -n512",
                       None),
        "radix": ("radix", "RADIX", f"-p{p} -n1048576", None),
    }
    out = {}
    for n in names:
        sub, binary, args, inp = d[n]
        sub = names[n] if isinstance(names, dict) and names[n] else sub
        if isinstance(sub, tuple):
            sub, binary = sub
        out[f"{prefix}-{n}"] = (os.path.join(base, sub), binary, args, inp)
    return out


S3_DIRS = {
    "barnes": "apps/barnes", "fmm": "apps/fmm",
    "ocean-cont": "apps/ocean/contiguous_partitions",
    "ocean-noncont": "apps/ocean/non_contiguous_partitions",
    "radiosity": "apps/radiosity", "raytrace": "apps/raytrace",
    "volrend": "apps/volrend", "water-nsquared": "apps/water-nsquared",
    "water-spatial": "apps/water-spatial", "cholesky": "kernels/cholesky",
    "fft": "kernels/fft", "lu-cont": "kernels/lu/contiguous_blocks",
    "lu-noncont": "kernels/lu/non_contiguous_blocks",
    "radix": "kernels/radix",
}
S4_DIRS = {
    "barnes": None, "fmm": None,
    "ocean-cont": ("ocean-contiguous_partitions", "OCEAN-CONT"),
    "ocean-noncont": ("ocean-non_contiguous_partitions", "OCEAN-NOCONT"),
    "radiosity": None, "raytrace": None, "volrend": None,
    "water-nsquared": None, "water-spatial": None, "cholesky": None,
    "fft": None,
    "lu-cont": ("lu-contiguous_blocks", "LU-CONT"),
    "lu-noncont": ("lu-non_contiguous_blocks", "LU-NOCONT"),
    "radix": None,
}

BENCHES = {}
BENCHES.update(splash("s3", S3, S3_DIRS))
BENCHES.update(splash("s4", S4, S4_DIRS))
for k, args in {
    "prodcons": "2000 0",
    "readmostly": "20000 16",
    "counter": "2000 20",
    "lock": "1000 20",
    "barrier": "4000 50",
    "falseshare": "10000 10",
}.items():
    BENCHES[f"micro-{k}"] = (MICRO, "cohmicro", f"{k} {NCPU} {args}", None)

# PARSEC 3.0 pthreads apps, simmedium inputs (scripts/build_parsec.sh). The
# main thread only spawns and joins, so SE mode needs workers + 1 cores:
# 15 workers on 16 cores; fluidanimate requires a power of two, so 8 (32
# on the larger meshes); swaptions needs at least one swaption per thread.
PW = NCPU - 1
FW = 8 if MESH == 4 else pow2_below(PW)
for name, args in {
    "blackscholes": f"{PW} in_16K.txt prices.txt",
    "swaptions": f"-ns {32 if PW < 32 else 64} -sm 20000 -nt {PW}",
    "fluidanimate": f"{FW} 5 in_100K.fluid out.fluid",
    "canneal": f"{PW} 15000 2000 200000.nets 64",
    "streamcluster": f"10 20 64 8192 8192 1000 none output.txt {PW}",
}.items():
    BENCHES[f"p-{name}"] = (os.path.join(PARSEC, name), name, args, None)
# Phase 1 load sweep: the spin work between accesses sets the injection
# rate (0 = most traffic). Iterations are effectively unbounded; the
# --roi-max-cycles cap ends each run.
SWEEP_KERNELS = ("prodcons", "barrier", "lock")
SWEEP_SPIN = (0, 25, 100, 400, 1600)
SWEEP = []
for k in SWEEP_KERNELS:
    for spin in SWEEP_SPIN:
        name = f"sweep-{k}-s{spin}"
        BENCHES[name] = (MICRO, "cohmicro", f"{k} {NCPU} 1000000 {spin}", None)
        SWEEP.append(name)

# Narrow-link variant of the sweep (run with -- --link-width-bits=32): the
# in-order microbenchmarks barely load the NIs with 16-byte flits, so
# 4-byte flits (4x more flits per packet) raise NI and link utilization
NSWEEP = []
for k in SWEEP_KERNELS:
    for spin in (0, 400):
        name = f"nsweep-{k}-s{spin}"
        BENCHES[name] = (MICRO, "cohmicro", f"{k} {NCPU} 1000000 {spin}", None)
        NSWEEP.append(name)

# canneal's simmedium netlist parse takes over 80 min in SE mode; simsmall
# (100K elements) as a fallback
BENCHES["p-canneal-simsmall"] = (os.path.join(PARSEC, "canneal-simsmall"),
                                 "canneal", f"{PW} 10000 2000 100000.nets 32",
                                 None)

# Working subset: one suite (SPLASH-4) covering distinct sharing patterns,
# plus the microbenchmarks that produce multicast.
SUBSET = [
    "s4-fft", "s4-lu-cont", "s4-radix", "s4-ocean-cont",
    "s4-water-nsquared", "s4-barnes",
    "micro-prodcons", "micro-readmostly", "micro-lock", "micro-barrier",
    "p-blackscholes", "p-swaptions", "p-fluidanimate", "p-streamcluster",
]
# Left out: p-canneal / p-canneal-simsmall. Their serial netlist parse ran
# 99 min (simmedium) without reaching the ROI, and the other ten standard
# benchmarks already cover the result.


def gem5_cmd(protocol, bench, outdir, cpu_type, roi_max_cycles, extra):
    wd, binary, args, inp = BENCHES[bench]
    cmd = [
        GEM5_BIN,
        f"--outdir={outdir}",
        "--redirect-stdout",
        "--redirect-stderr",
        SE_PY,
        f"--protocol={protocol}",
        "--ruby",
        "--network=garnet",
        f"--topology={TOPOLOGY}",
        f"--mesh-rows={MESH}",
        "--routing-algorithm=1",  # XY
        f"--num-cpus={NCPU}",
        f"--num-dirs={NHOME}",
        f"--num-l2caches={NHOME}",
        f"--cpu-type={cpu_type}",
        "--cpu-clock=2GHz",
        "--ruby-clock=2GHz",
        "--mem-size=2GB",
        # SE syscalls do functional reads Garnet cannot serve mid-flight
        "--access-backing-store",
        "--l1d_size=32kB", "--l1d_assoc=8",
        "--l1i_size=32kB", "--l1i_assoc=8",
        "--l2_size=256kB", "--l2_assoc=8",
        "--garnet-trace=trace.csv.gz",
        f"--roi-max-cycles={roi_max_cycles}",
        f"--cmd={os.path.join(wd, binary)}",
    ]
    if args:
        cmd.append(f"--options={args}")
    if inp:
        cmd.append(f"--input={os.path.join(wd, inp)}")
    cmd += extra
    return cmd, wd


def done(protocol, bench):
    """A run counts as done if it logged ok and the program exited cleanly."""
    log = os.path.join(RESULTS, "runs.log")
    simout = os.path.join(RESULTS, protocol, bench, "simout.txt")
    if not (os.path.exists(log) and os.path.exists(simout)):
        return False
    with open(log) as f:
        last = [l.split()[4] for l in f
                if l.split()[2:4] == [protocol, bench]]
    with open(simout) as f:
        clean = "Simulated exit code not 0" not in f.read()
    return bool(last) and last[-1] == "ok" and clean


def run(protocol, bench, cpu_type, roi_max_cycles, extra, timeout, dry):
    outdir = os.path.join(RESULTS, protocol, bench)
    cmd, wd = gem5_cmd(protocol, bench, outdir, cpu_type, roi_max_cycles,
                       extra)
    if dry:
        print(f"(cd {wd} && {shlex.join(cmd)})")
        return bench, protocol, "dry", 0
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "cmd.txt"), "w") as f:
        f.write(f"cd {wd}\n{shlex.join(cmd)}\n")
    t0 = time.time()
    try:
        rc = subprocess.run(cmd, cwd=wd, timeout=timeout,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
        status = "ok" if rc == 0 else f"rc={rc}"
        # gem5 exits 0 even when the simulated program fails
        with open(os.path.join(outdir, "simout.txt")) as f:
            m = re.search(r"Simulated exit code not 0! Exit code is (\d+)",
                          f.read())
        if status == "ok" and m:
            status = f"app_rc={m.group(1)}"
    except subprocess.TimeoutExpired:
        status = "timeout"
    dt = time.time() - t0
    with open(os.path.join(RESULTS, "runs.log"), "a") as f:
        f.write(f"{time.strftime('%F %T')} {protocol} {bench} {status} "
                f"{dt:.0f}s\n")
    print(f"[{status:>8}] {protocol:15s} {bench:22s} {dt/60:6.1f} min",
          flush=True)
    return bench, protocol, status, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--mesh", type=int, default=4,
                    help="N x N mesh (read before argparse, see _mesh)")
    ap.add_argument("--protocols", default="MESI_Two_Level,MOESI_hammer")
    ap.add_argument("--only", default=".*", help="regex on benchmark name")
    ap.add_argument("--subset", action="store_true",
                    help="restrict to the SUBSET list")
    ap.add_argument("--skip-done", action="store_true",
                    help="skip runs already completed cleanly")
    ap.add_argument("--roi-max-cycles", type=int, default=50_000_000,
                    help="stop each run this many cycles into its ROI "
                    "(0 = run to completion)")
    ap.add_argument("--cpu-type", default="X86TimingSimpleCPU")
    ap.add_argument("--timeout", type=float, default=6 * 3600)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("extra", nargs="*", help="extra se.py args after --")
    a = ap.parse_args()

    benches = [b for b in BENCHES if re.search(a.only, b)
               and (not a.subset or b in SUBSET)]
    jobs = [(p, b) for b in benches for p in a.protocols.split(",")
            if not (a.skip_done and done(p, b))]
    os.makedirs(RESULTS, exist_ok=True)
    with ThreadPoolExecutor(a.j) as ex:
        futs = [ex.submit(run, p, b, a.cpu_type, a.roi_max_cycles, a.extra,
                          a.timeout, a.dry_run) for p, b in jobs]
        res = [f.result() for f in futs]
    bad = [r for r in res if r[2] not in ("ok", "dry")]
    if bad:
        print("failed:", *[f"{p}/{b}:{s}" for b, p, s, _ in bad], sep="\n  ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
