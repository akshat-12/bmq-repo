#!/usr/bin/env python3
"""BMQ, the batch-aware multicast queueing model.

Input: traffic parameters (Params). Output: the latency of every copy of a
multicast, its completion (last copy delivered), averages over the traffic,
and per-queue / per-link utilization. Equation numbers in brackets refer
to BMQ_COMPLETE_GUIDE.md section 2.

    source NI   GGeo arrivals per (source, vnet) queue; a multicast is a
                batch whose busy time comes from the copy schedule [3]-[13]
    links       classic queue wait x merge factor                  [14]-[17]
    latency     T_j = W_head + tail_j + sum of link waits + head    [18]-[20]

Router timing (pipeline stages, link and credit delays) is an input,
not fitted: see the constants below.

Fitting the parameters to a gem5 trace (validation) is in fit_params.py.

usage: bmq.py [--demo | --sweep]
  from bmq import Model, Params
  m = Model(Params(lam=0.02, alpha=0.05, K=15, pb=0.3))
  m.summary(); m.multicast(21)
"""

import argparse
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

# NoC: router microarchitecture (Garnet's configuration). Every timing
# constant below is derived from these; none is fitted to measured latency.
# Call ni_schedule.cache_clear() after changing them.
ROWS = COLS = 4                    # 4x4 mesh, XY routing (set_mesh for others)
NROUTERS = ROWS * COLS
LAYERS = {}                        # node-id base -> controllers of that type;
                                   # empty: one per router at multiples of NROUTERS
NI_VCS = 4                         # V: output VCs per vnet at the NI
T_NI = 1                           # NI: a flit sent this cycle is on the link next cycle
T_ROUTER = 1                       # router pipeline stages (route, switch, traversal)
T_LINK = 1                         # link traversal
# a flit sent at cycle t reaches the router at t + T_NI + T_LINK, wins the
# switch T_ROUTER - 1 cycles later, and its credit goes back over a link
# (+1 to enter it, + T_LINK); the NI reads credits after sending, so the
# credit is usable one cycle after it arrives
CREDIT_RTT = T_NI + T_ROUTER + 2 * T_LINK + 1      # 5 cycles
VC_RELEASE = CREDIT_RTT            # R: the tail's credit carries the VC-free signal
# between routers the loop has no NI stage, and routers apply credits
# before switch allocation (Router::wakeup), so a router VC is free
# T_ROUTER + 2 T_LINK = 3 cycles after the upstream switch sent its tail;
# into the destination NI, which returns the credit on receipt, 2 cycles
R_LINK = T_ROUTER + 2 * T_LINK
R_EJECT = 2 * T_LINK
# High-load correction: a copy keeps its NI VC from acquiring it until R
# cycles after its tail leaves, plus however long the tail waits at the
# first router. Between messages the V VCs then limit the NI to
# V / (VC time) copies per cycle, and the vnets share one injection link.
# False gives the original model (every message finds all VCs free).
VC_HOLD = True


def head_latency(h):
    """Zero-load cycles from the NI sending a flit to the destination NI
    receiving it, h router-to-router hops: h + 1 routers, h + 2 links."""
    return T_NI + (h + 1) * T_ROUTER + (h + 2) * T_LINK


# ------------------------------------------------------------- the mesh

def set_mesh(rows, layers=None):
    """Use a rows x rows mesh. layers {node-id base: count} lists the
    controller types; the j-th controller of a type sits on router j
    (Garnet Mesh_XY, or Mesh_XY_Partial when a type has fewer controllers
    than routers, as the 32 home nodes on a 6x6 mesh)."""
    global ROWS, COLS, NROUTERS, LAYERS
    ROWS = COLS = rows
    NROUTERS = rows * rows
    LAYERS = {b: n for b, n in (layers or {}).items() if n}


def base_of(node):
    """Node-id base of the controller type that node belongs to."""
    if LAYERS:
        return max(b for b in LAYERS if b <= node)
    return node // NROUTERS * NROUTERS


def layer_size(base):
    """Controllers of the type starting at base: they sit on routers 0..n-1."""
    return LAYERS.get(base, NROUTERS)


def router(node):
    """The j-th controller of each type sits on router j."""
    return node - base_of(node)


def hops(src, dst):
    """Manhattan distance between the two nodes' routers (XY routing)."""
    a, b = router(src), router(dst)
    return abs(a % COLS - b % COLS) + abs(a // COLS - b // COLS)


def path(src, dst):
    """Channels after the NI: ("link", r, n) along the row, then the
    column, then ("ej", dst) into the destination's NI."""
    ch, r, d = [], router(src), router(dst)
    while r % COLS != d % COLS:                       # X first
        n = r + (1 if d % COLS > r % COLS else -1)
        ch.append(("link", r, n))
        r = n
    while r // COLS != d // COLS:                     # then Y
        n = r + (COLS if d // COLS > r // COLS else -COLS)
        ch.append(("link", r, n))
        r = n
    return ch + [("ej", dst)]


# ---------------------------------------------------- copy schedule [11]

@lru_cache(maxsize=None)
def ni_schedule(K, L, b=1):
    """Garnet's NI sending K copies of L flits from cycle 0, replayed cycle
    by cycle: free VCs take the next copies in order; the link sends one
    flit per cycle, round-robin over VCs with a flit and a credit (b =
    router buffer depth, each credit back CREDIT_RTT cycles after its flit);
    a VC is free again R cycles after its copy's tail. Returns per copy
    (VC acquired, tail sent)."""
    acq, tail = [None] * K, [None] * K
    vc_copy = [None] * NI_VCS              # copy on each VC
    vc_left = [0] * NI_VCS                 # its flits still to send
    credits = [b] * NI_VCS                 # downstream buffer slots
    free_at = [0] * NI_VCS                 # when each VC can be reused
    returns, nxt, t, rr = [], 0, 0, 0      # credits in flight, next copy, cycle, RR
    while None in tail:
        for v in [v for (rt, v) in returns if rt == t]:   # credits come back
            credits[v] += 1
        returns = [x for x in returns if x[0] > t]
        for v in range(NI_VCS):                           # free VCs take copies
            if vc_copy[v] is None and nxt < K and free_at[v] <= t:
                vc_copy[v], vc_left[v], acq[nxt] = nxt, L, t
                nxt += 1
        for i in range(NI_VCS):                           # one flit on the link
            v = (rr + i) % NI_VCS
            if vc_copy[v] is not None and vc_left[v] and credits[v]:
                credits[v] -= 1
                vc_left[v] -= 1
                returns.append((t + CREDIT_RTT, v))
                if vc_left[v] == 0:                       # tail sent
                    tail[vc_copy[v]] = t
                    vc_copy[v] = None
                    free_at[v] = t + VC_RELEASE
                rr = (v + 1) % NI_VCS
                break
        t += 1
    return tuple(acq), tuple(tail)


def copy_offsets(K, L, b=1):
    """s_j [12]: cycles after copy 1 that copy j leaves."""
    tail = ni_schedule(K, L, b)[1]
    return tuple(x - tail[0] for x in tail)


def busy(K, L, b=1):
    """B(K, L) [13]: cycles a message holds its NI queue."""
    return ni_schedule(K, L, b)[1][-1] + 1


def vc_time(K, L, b=1):
    """VC-cycles a message uses: each copy holds its VC from acquiring it
    until R cycles after its tail leaves."""
    acq, tail = ni_schedule(K, L, b)
    return sum(t + VC_RELEASE - a for a, t in zip(acq, tail))


def service(K, L, b=1, extra=0.0):
    """Effective NI service time [13a]: the copy schedule's B, or, when
    VC_HOLD, at least the message's VC time spread over the V VCs (extra =
    cycles each copy also waits at the first router while holding its VC)."""
    B = busy(K, L, b)
    return max(B, (vc_time(K, L, b) + K * extra) / NI_VCS) if VC_HOLD else B


# ---------------------------------------------- source NI queue [3]-[10]

def head_wait(lam, pb, mix, b=1, extra=0.0):
    """W_head of a queue with lam messages/cycle, burst probability pb and
    mix {(K, L): probability}; extra as in service(). Returns (W_head,
    rho); inf if saturated."""
    pb = min(pb, 0.99)
    EB = sum(q * service(k, l, b, extra) for (k, l), q in mix.items())
    EB2 = sum(q * service(k, l, b, extra) ** 2 for (k, l), q in mix.items())
    ENN = 2 * lam * pb / (1 - pb)                     # same-cycle pairs [3]
    rho = lam * EB                                    # utilization [5]
    if rho >= 1:
        return float("inf"), rho
    EAA = lam * (EB2 - EB) + ENN * EB ** 2            # lumpiness [6]
    EU = EAA / (2 * (1 - rho))                        # backlog [8]
    return EU + pb * EB / (1 - pb), rho               # + same-cycle [9-10]


# ------------------------------------------------------- links [14]-[17]

def link_waits(flows):
    """Wait and utilization per channel, from flows {(src, dst, L, vnet):
    rate}. W = (1-h) rho T (1+Cs2) / (2(1-rho)), h = sum(port rate^2) /
    rate^2: packets through the same input port never collide. With
    VC_HOLD the NI's injection link ("inj", src) is a channel too, whose
    input ports are the vnets (one vnet's copies are already serialized by
    the copy schedule)."""
    lam, fl, fl2 = defaultdict(float), defaultdict(float), defaultdict(float)
    into = defaultdict(lambda: defaultdict(float))    # channel <- input port
    for (s, d, L, v), r in flows.items():
        up = ("ni", s, v)
        for ch in ([("inj", s)] if VC_HOLD else []) + path(s, d):
            lam[ch] += r
            fl[ch] += r * L
            fl2[ch] += r * L * L
            into[ch][up] += r
            up = ch
    W, rho = {}, {}
    for ch, la in lam.items():
        T = fl[ch] / la                               # mean length
        cs2 = fl2[ch] / la / T ** 2 - 1               # length spread
        rho[ch] = p = la * T
        h = sum(x * x for x in into[ch].values()) / la ** 2
        W[ch] = float("inf") if p >= 1 else \
            (1 - h) * p * T * (1 + cs2) / (2 * (1 - p))  # [17]
    return W, rho


def erlang_c(V, a):
    """Probability that all V servers are busy (M/M/V, offered load a)."""
    if a >= V:
        return 1.0
    t, tot = 1.0, 1.0
    for k in range(1, V):
        t *= a / k
        tot += t
    t *= a / V
    top = t * V / (V - a)
    return top / (tot + top)


def vc_waits(flows, W, iters=20):
    """VC_HOLD: wait for a free downstream VC per channel. A packet crossing
    channel c needs one of the V VCs of its vnet at the next router (or the
    destination NI) and holds it from its head until R_LINK (R_EJECT)
    cycles after its tail, plus however long it then waits at that router (switch wait W and
    VC wait of its next channel): S = L - 1 + R + D. Each (channel, vnet) is
    an M/D/V pool, Wvc = (1 - h) C(V, a) S / (V - a) / 2 with a = rate S
    (Erlang C, deterministic hold) and h the merge factor: packets through
    one input port come at most one per cycle, so they alone do not
    queue for VCs. D depends on the downstream waits, so iterate
    from D = 0 (a backward pass). Returns (Wvc, vc utilization) per
    channel, the utilization the busiest vnet's a / V."""
    rate, flit = defaultdict(float), defaultdict(float)
    nxt = defaultdict(lambda: defaultdict(float))     # (ch, v) -> next ch -> rate
    into = defaultdict(lambda: defaultdict(float))    # (ch, v) <- input port
    for (s, d, L, v), r in flows.items():
        p = path(s, d)
        for i, ch in enumerate(p):
            rate[(ch, v)] += r
            flit[(ch, v)] += r * (L - 1)
            into[(ch, v)][p[i - 1] if i else ("ni", s)] += r
            if i + 1 < len(p):
                nxt[(ch, v)][p[i + 1]] += r
    h = {cv: sum(x * x for x in into[cv].values()) / la ** 2
         for cv, la in rate.items()}
    wvc = defaultdict(float)
    for _ in range(iters):
        new, util = defaultdict(float), defaultdict(float)
        for cv, la in rate.items():
            D = sum(r * (W[n] + wvc[(n, cv[1])]) for n, r in nxt[cv].items()) / la
            S = flit[cv] / la + D + (R_EJECT if cv[0][0] == "ej" else R_LINK)
            a = la * S
            util[cv[0]] = max(util[cv[0]], a / NI_VCS)
            new[cv] = float("inf") if a >= NI_VCS else \
                (1 - h[cv]) * erlang_c(NI_VCS, a) * S / (NI_VCS - a) / 2
        if all(abs(new[k] - wvc[k]) < 1e-6 for k in new):
            break
        wvc = new
    return dict(new), dict(util)


# ------------------------------------------------- traffic parameters

@dataclass
class Params:
    """Traffic description. `nodes` overrides lam, alpha, K, packets,
    mc_packets per source.

    lam         messages per cycle per source
    alpha       fraction of messages that are multicasts
    K           fan-out: int or {K: probability}
    packets     unicast {(L flits, vnet): probability}
    mc_packets  multicast mix (default: packets)
    pattern     uniform, hotspot (hot, hot_frac), transpose,
                bit_complement, neighbor, tornado, shuffle
    pb          burst probability: one value or {(src, vnet): pb}
    sources     sending node ids
    dst_base    destination node = base + router; int or {base: prob}
    mc_dst_base multicast destination base (default: likeliest dst_base)
    vbuf        {vnet: buffer depth}; default 4 on vnets with longer
                (data) packets, else 1
    """
    lam: float
    alpha: float = 0.0
    K: object = 15
    packets: dict = field(default_factory=lambda: {(1, 0): 1.0})
    mc_packets: dict = None
    pattern: str = "uniform"
    pb: object = 0.0
    sources: tuple = None              # default: one per router
    dst_base: object = None            # default: NROUTERS
    mc_dst_base: int = None
    hot: int = 0
    hot_frac: float = 0.0
    nodes: dict = field(default_factory=dict)
    vbuf: dict = None
    samples: int = 200                 # random multicast sets per source
    pb_pkt: dict = None                # p_b fitted on copies (Mandal baseline only)
    seed: int = 1


def destinations(pattern, src, hot=0, hot_frac=0.0):
    """Unicast destination routers {router: prob} for a source router
    (gem5 synthetic-traffic definitions; uniform includes the source)."""
    n, x, y = NROUTERS, src % COLS, src // COLS
    if pattern in ("uniform", "uniform_random"):
        return {d: 1 / n for d in range(n)}
    if pattern == "hotspot":
        p = {d: (1 - hot_frac) / n for d in range(n)}
        p[hot] += hot_frac
        return p
    one = {"transpose": lambda: x * COLS + y,
           "bit_complement": lambda: (ROWS - 1 - y) * COLS + (COLS - 1 - x),
           "neighbor": lambda: y * COLS + (x + 1) % COLS,
           "tornado": lambda: y * COLS + (x + COLS // 2 - 1) % COLS,
           "shuffle": lambda: src * 2 if src < n // 2 else src * 2 - n + 1}
    if pattern not in one:
        raise ValueError(f"unknown pattern {pattern!r}")
    return {one[pattern](): 1.0}


def expand(p):
    """Expected rate {(src, dests, L, vnet): messages/cycle} implied by p.
    Multicasts go to K distinct routers other than the source's; their
    rate is spread over `samples` random sets (one set for a broadcast)."""
    rng = random.Random(p.seed)
    dst_base = NROUTERS if p.dst_base is None else p.dst_base
    bases = dst_base if isinstance(dst_base, dict) else {dst_base: 1.0}
    mc_base = p.mc_dst_base if p.mc_dst_base is not None else \
        max(bases, key=bases.get)
    rates = Counter()
    for s in p.sources or range(NROUTERS):
        o = p.nodes.get(s, {})
        lam, alpha = o.get("lam", p.lam), o.get("alpha", p.alpha)
        K = o.get("K", p.K)
        packets = o.get("packets", p.packets)
        mc_packets = o.get("mc_packets", p.mc_packets) or packets
        r = router(s)
        dist = destinations(p.pattern, r, p.hot, p.hot_frac)
        for (L, v), pl in packets.items():                # unicasts
            for base, pbase in bases.items():
                # only routers that have a node of this type
                n = layer_size(base)
                tot = sum(q for d, q in dist.items() if d < n)
                if not tot:
                    continue
                for d, pd_ in dist.items():
                    rate = lam * (1 - alpha) * pl * pd_ / tot * pbase \
                        if d < n else 0
                    if rate:
                        rates[(s, (base + d,), L, v)] += rate
        if not alpha:
            continue
        others = [d for d in range(layer_size(mc_base)) if d != r]  # multicasts
        for k, pk in ({K: 1.0} if isinstance(K, int) else K).items():
            sets = [tuple(others)] if k >= len(others) else \
                [tuple(sorted(rng.sample(others, k))) for _ in range(p.samples)]
            for (L, v), pl in mc_packets.items():
                for ds in sets:
                    rates[(s, tuple(mc_base + d for d in ds), L, v)] += \
                        lam * alpha * pk * pl / len(sets)
    return rates


# ------------------------------------------------------------ the model

class Model:
    """BMQ for one Params. After construction: rates, w_head and ni_rho
    per (src, vnet) queue, W and link_rho per channel."""

    def __init__(self, params):
        self.p = p = params
        self.rates = expand(p)
        self.vbuf = dict(p.vbuf or {})
        if not p.vbuf:                        # 4 flits where data travels
            lmin = min(L for (_, _, L, _) in self.rates)
            for (_, _, L, v) in self.rates:
                self.vbuf[v] = max(self.vbuf.get(v, 1), 4 if L > lmin else 1)
        per_q, flows = defaultdict(Counter), Counter()
        for (s, ds, L, v), r in self.rates.items():
            per_q[(s, v)][(len(ds), L)] += r
            for d in ds:                      # source replication: K flows
                flows[(s, d, L, v)] += r
        self.W, self.link_rho = link_waits(flows)
        self.Wvc, self.vc_rho = vc_waits(flows, self.W) if VC_HOLD else ({}, {})
        # VC_HOLD: mean wait of a queue's copies at the injection link and
        # the first router, during which they keep their NI VC
        first = defaultdict(float)
        for (s, d, L, v), r in flows.items():
            c = path(s, d)[0]
            first[(s, v)] += r * (self._inj(s) + self.W[c] + self.Wvc.get((c, v), 0.0))
        self.w_head, self.ni_rho = {}, {}
        for q, m in per_q.items():
            lam = sum(m.values())
            pb = p.pb.get(q, 0.0) if isinstance(p.pb, dict) else p.pb
            ncopy = sum(r * k for (k, _), r in m.items())
            self.w_head[q], self.ni_rho[q] = head_wait(
                lam, pb, {kl: r / lam for kl, r in m.items()},
                self.vbuf.get(q[1], 1),
                first[q] / ncopy if VC_HOLD and ncopy else 0.0)

    def _inj(self, src):
        """Wait at the shared injection link (0 without VC_HOLD)."""
        return self.W.get(("inj", src), 0.0)

    def T0(self, h, L, vnet=0):
        """Zero-load latency of a lone L-flit packet over h hops: its tail
        leaves the NI (credit-limited on shallow buffers), then travels."""
        return ni_schedule(1, L, self.vbuf.get(vnet, 1))[1][0] + head_latency(h)

    def copies(self, src, dests, L=1, vnet=0):
        """Each copy's latency [19], in send order: NI wait, the cycle its
        tail leaves the NI (copy schedule), link waits, zero-load travel."""
        w = self.w_head.get((src, vnet), 0.0)
        tails = ni_schedule(len(dests), L, self.vbuf.get(vnet, 1))[1]
        return [w + tj + self._inj(src)
                + sum(self.W[c] + self.Wvc.get((c, vnet), 0.0) for c in path(src, d))
                + head_latency(hops(src, d)) for tj, d in zip(tails, dests)]

    def multicast(self, src, dests=None, L=1, vnet=0):
        """Copies of one multicast; default a broadcast to every other router."""
        if dests is None:
            b = self.p.mc_dst_base
            if b is None:
                b = NROUTERS if self.p.dst_base is None else self.p.dst_base
                b = b if not isinstance(b, dict) else max(b, key=b.get)
            dests = tuple(b + d for d in range(layer_size(b)) if d != router(src))
        return self.copies(src, dests, L, vnet)

    def latency(self, src, dests, L=1, vnet=0):
        """(mean copy latency, completion = max over copies [20])."""
        t = self.copies(src, dests, L, vnet)
        return sum(t) / len(t), max(t)

    def saturated(self):
        return (any(r >= 1 for r in self.ni_rho.values())
                or any(r >= 1 for r in self.link_rho.values())
                or any(r >= 1 for r in self.vc_rho.values()))

    def summary(self):
        """Rate-weighted means: uni, copy, compl, ni_lastvc (W_head + last
        copy's VC time), and peak NI / link utilization."""
        acc = defaultdict(float)
        for (s, ds, L, v), n in self.rates.items():
            mean, last = self.latency(s, ds, L, v)
            kind = "mc" if len(ds) > 1 else "uni"
            acc[kind + "_n"] += n
            acc[kind + "_copy"] += n * mean
            acc[kind + "_compl"] += n * last
            if kind == "mc":
                acc["mc_ni_last"] += n * (self.w_head[(s, v)] + ni_schedule(
                    len(ds), L, self.vbuf.get(v, 1))[0][-1])
        out = {}
        if acc["uni_n"]:
            out["uni"] = acc["uni_copy"] / acc["uni_n"]
        if acc["mc_n"]:
            out["copy"] = acc["mc_copy"] / acc["mc_n"]
            out["compl"] = acc["mc_compl"] / acc["mc_n"]
            out["ni_lastvc"] = acc["mc_ni_last"] / acc["mc_n"]
        out["max_ni_rho"] = max(self.ni_rho.values())
        out["max_link_rho"] = max(self.link_rho.values())
        return out


# ------------------------------------------------------------------ CLI

def demo():
    """A 1-flit broadcast from node 21 on an idle mesh (completion 29)."""
    m = Model(Params(lam=1e-9, sources=(21,), alpha=1.0, K=15, dst_base=0))
    t = m.multicast(21)
    print("copy offsets s_j:", copy_offsets(15, 1))
    print("copy latency:    ", [round(x, 1) for x in t])
    print(f"mean copy {sum(t) / len(t):.1f}, completion {max(t):.1f} cycles")


def sweep():
    """Latency vs. load: uniform, 5% broadcasts, p_b = 0.3."""
    print(f"{'lam':>6s} {'unicast':>8s} {'mc copy':>8s} {'complete':>9s}"
          f" {'max link rho':>13s}")
    for lam in (0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06):
        m = Model(Params(lam=lam, alpha=0.05, K=15, pb=0.3,
                         packets={(1, 0): 0.6, (5, 1): 0.4},
                         mc_packets={(1, 0): 1.0}))
        if m.saturated():
            print(f"{lam:6.3f}   saturated")
            break
        r = m.summary()
        print(f"{lam:6.3f} {r['uni']:8.1f} {r['copy']:8.1f} {r['compl']:9.1f}"
              f" {r['max_link_rho']:13.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--demo", action="store_true")
    sweep() if ap.parse_args().sweep else demo()
