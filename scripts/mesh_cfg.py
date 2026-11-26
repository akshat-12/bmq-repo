"""Mesh size for the gem5 runners: --mesh N on the command line (read
before argparse, since tables are built at import), N*N cores, and Ruby's
power-of-two count of directories / L2 banks. A 6x6 mesh therefore has
32 home nodes, on routers 0-31 (topology Mesh_XY_Partial)."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mesh():
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
NHOME = pow2_below(NCPU)           # directories (and L2 banks)
TOPOLOGY = "Mesh_XY" if NHOME == NCPU else "Mesh_XY_Partial"
RESULTS = os.path.join(ROOT, "results") if MESH == 4 else \
    os.path.join(ROOT, "results", f"mesh{MESH}")


def use_mesh(bmq):
    """Point the model at this mesh: cores 0..NCPU-1, then directories."""
    bmq.set_mesh(MESH, {0: NCPU, NCPU: NHOME})


def rate_scale(bmq, params):
    """Factor that gives this mesh the 4x4 mesh's utilization at the same
    setting: every NI and link load is linear in the rate, so the ratio of
    the busiest queue's load per unit rate (4x4 / this mesh)."""
    def load(mesh, layers):
        bmq.set_mesh(mesh, layers)
        m = bmq.Model(params(0.01, mesh))
        return max(max(m.ni_rho.values()), max(m.link_rho.values()))
    base = load(4, {})
    here = load(MESH, {0: NCPU, NCPU: NHOME})
    use_mesh(bmq)
    return base / here


def gem5_mesh_args():
    return [f"--topology={TOPOLOGY}", f"--mesh-rows={MESH}",
            "--routing-algorithm=1",  # XY
            f"--num-cpus={NCPU}", f"--num-dirs={NHOME}"]
