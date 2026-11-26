#!/bin/bash
# Build the PARSEC 3.0 pthreads subset as static binaries for gem5 SE mode,
# with the ROI hooks replaced by gem5 m5ops (benchmarks/parsec-hooks).
# Mirrors the flags of each app's own Makefile for version=pthreads.
# Output: benchmarks/parsec-build/<app>/<app>, plus its simmedium inputs.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
P=$ROOT/benchmarks/parsec-3.0/pkgs
OUT=$ROOT/benchmarks/parsec-build
GEM5=$ROOT/gem5
HOOKS_INC=$P/libs/hooks/src/include

CFLAGS="-O2 -g -fno-strict-aliasing -pthread -DENABLE_PARSEC_HOOKS -I$HOOKS_INC -I$GEM5/include"
LIBS="$OUT/hooks.o $GEM5/util/m5/build/x86/out/libm5.a -lm"

mkdir -p "$OUT"
gcc $CFLAGS -c "$ROOT/benchmarks/parsec-hooks/hooks.c" -o "$OUT/hooks.o"

build() {  # build <app> <srcdir> <extra flags> <sources...>
    local app=$1 src=$2 extra=$3; shift 3
    local d=$OUT/$app
    mkdir -p "$d"
    (cd "$src" && g++ $CFLAGS $extra "$@" $LIBS -static -o "$d/$app")
    echo "built $d/$app"
}

inputs() {  # inputs <app> <pkgdir>
    tar xf "$2/inputs/input_simmedium.tar" -C "$OUT/$1"
}

# blackscholes: pthreads version is generated with m4 from the C source
BS=$P/apps/blackscholes/src
m4 "$BS/c.m4.pthreads" "$BS/blackscholes.c" > "$OUT/blackscholes.m4.cpp"
build blackscholes "$BS" "-DENABLE_THREADS" "$OUT/blackscholes.m4.cpp"
inputs blackscholes "$P/apps/blackscholes"

SW=$P/apps/swaptions/src
build swaptions "$SW" "-DENABLE_THREADS" \
    CumNormalInv.cpp MaxFunction.cpp RanUnif.cpp nr_routines.c icdf.cpp \
    HJM_SimPath_Forward_Blocking.cpp HJM.cpp HJM_Swaption_Blocking.cpp \
    HJM_Securities.cpp

FL=$P/apps/fluidanimate/src
build fluidanimate "$FL" "-D_GNU_SOURCE -D__XOPEN_SOURCE=600" \
    pthreads.cpp cellpool.cpp parsec_barrier.cpp
inputs fluidanimate "$P/apps/fluidanimate"

CN=$P/kernels/canneal/src
build canneal "$CN" "-DENABLE_THREADS" \
    annealer_thread.cpp rng.cpp netlist.cpp main.cpp netlist_elem.cpp
inputs canneal "$P/kernels/canneal"
mkdir -p "$OUT/canneal-simsmall"
cp "$OUT/canneal/canneal" "$OUT/canneal-simsmall/"
tar xf "$P/kernels/canneal/inputs/input_simsmall.tar" -C "$OUT/canneal-simsmall"

SC=$P/kernels/streamcluster/src
build streamcluster "$SC" "-DENABLE_THREADS -DGEM5_PARALLEL_ROI" \
    streamcluster.cpp parsec_barrier.cpp
