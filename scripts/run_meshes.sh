#!/bin/bash
# The 6x6 and 8x8 validation campaign: every 4x4 experiment again on the
# larger meshes, now the 10 standard benchmarks under both protocols only. Finished runs are skipped, so it can
# be restarted. Launch detached, so it outlives the shell:
#
#   systemd-run --user --unit=bmq-meshes -p MemoryMax=12G \
#       bash ~/workspace/Modeling-Project/scripts/run_meshes.sh
#
# Progress: results/mesh<N>/runs.log and journalctl --user -u bmq-meshes

cd "$(dirname "$0")/.." || exit 1
STD='^(p-(blackscholes|fluidanimate|streamcluster|swaptions)|s4-(barnes|fft|lu-cont|ocean-cont|radix|water-nsquared))$'

# 2026-11-23: cut to the standard benchmarks only. The synthetic sweeps
# were already finished on both meshes; the coherence microbenchmark
# sweeps (16 B and 4 B) are dropped (29 of 30 16 B runs on 6x6 had
# finished and are kept in results/mesh6, but are not used).
# Sped up: 20M-cycle measurement window (4x4 used 50M) and 6 runs at a time.
for m in 6 8; do
    python3 scripts/run_suite.py --mesh $m -j 6 --skip-done --only "$STD" \
        --roi-max-cycles 20000000
done
echo "campaign done $(date)"
