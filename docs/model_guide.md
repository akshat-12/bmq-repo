# BMQ: the complete project guide

**BMQ** (batch-aware multicast queueing model) is an analytical model of
how long a multicast takes in a mesh network-on-chip (NoC) under bursty
traffic. Its input is a handful of traffic parameters (injection rate,
multicast fraction, fan-out, packet mix, destination pattern,
burstiness), as in other analytical NoC models: no trace or simulator is
needed to run it. We use gem5 traces only to validate it, with
cache-coherence workloads as the source of realistic multicast. This
document collects everything in one place:

1. **NoC background**, and the assumptions we make about the NoC
2. **The model**, derived step by step
3. **The algorithm**: the model as a procedure
4. **Trace generation**: how we run benchmarks on gem5
5. **Validation inputs**: fitting the parameters to a gem5 trace
6. **Implementation**: how the model is coded
7. **Results**: what has been validated, and where it breaks
8. **Limitations and next steps**

The appendix has a glossary, references and a file map. Equations are
numbered (0)-(20) throughout.

**The one-sentence version:** a multicast's copies leave the source one
by one, on a schedule set by the NI's virtual channels, so the last copy
arrives much later than the average. BMQ models that schedule exactly,
inside a bursty queue, and adds a one-line link model for the network.
That cuts completion-latency error from 26-34% (published models) to
6.4% on standard benchmarks and 9.5% on our coherence load sweep, with
traffic parameters fitted to each trace (section 7). Router timing is an
input taken from the router design; no constant is fitted to measured
latency.

---

## 1. NoC background and our assumptions

### 1.1 What a NoC is

A many-core chip connects its cores, caches and memory controllers with a
**network-on-chip**: a grid of small routers joined by links. Messages
travel hop by hop, like packets on the internet, but inside one chip and
measured in clock cycles.

| Term | Meaning |
|---|---|
| **Node / tile** | One core with its caches, attached to one router |
| **Router** | Switches traffic between its ports: north, south, east, west, and local |
| **Link** | A wire bundle between two routers. Moves one **flit** per cycle |
| **Flit** | The unit a link moves per cycle (16 bytes in our main setup) |
| **Packet** | One message's worth of flits: head flit, body flits, tail flit. Control messages are 1 flit; data messages (a 64-byte cache line plus header) are 5 |
| **NI (network interface)** | A node's on-ramp: turns a controller's message into packets and injects them into the router |
| **Mesh** | Routers in a 2-D grid, each linked to its neighbors. Ours is 4×4 = 16 routers |
| **XY routing** | A packet first moves along its row (X), then its column (Y). Deterministic: the path depends only on source and destination |
| **Hop** | One router-to-router link crossing. On a 4×4 mesh, 0-6 hops |
| **Wormhole switching** | A packet's flits follow its head flit through the network like a train; a router forwards flits before the whole packet has arrived |
| **Virtual channel (VC)** | Several independent queues sharing one physical link, so one blocked packet doesn't block everyone behind it |
| **Virtual network (vnet)** | A class of traffic with its own VCs: requests, forwarded requests, responses. Keeps protocol message classes from deadlocking each other |
| **Credit flow control** | A router sends a packet only when the next router has buffer space; credits returned upstream report that space |
| **Zero-load latency** | Time through an empty network: NI stage + router pipelines + link hops + the packet's extra flits |

### 1.2 Where multicast comes from

Cache coherence keeps every core's cached copy of a memory line
consistent. It creates **multicast** (one message to several
destinations) in two ways:

- **Directory protocols** (our MESI_Two_Level) invalidate every sharer
  before a write. A write to a line shared by K caches sends K
  invalidations.
- **Broadcast protocols** (our MOESI_hammer) send every miss to every
  other cache: always 15 copies on 16 cores.

A write can't complete until **every** copy has arrived. So the latency
that matters is the **completion latency**: when the last copy arrives.

### 1.3 How a multicast is sent: source replication

Many NoCs, including gem5's Garnet, have no multicast hardware in the
routers. The **sending NI makes one unicast copy per destination**, and
pushes them into the network one after another through its single
injection link. Each copy then travels its own XY path.

This is the mechanism BMQ is built around: the copies can't all leave at
once, so the last copy starts late before it even enters the network.

### 1.4 The NoC we model

| Parameter | Value |
|---|---|
| Topology | 4×4 mesh (`Mesh_XY`), one L1, one L2 bank and one directory per router |
| Routing | XY, deterministic |
| Flit size | 16 bytes (main); 4 bytes for the narrow-link runs |
| Packet sizes | Control 1 flit, data 5 flits (16 B); 2 and 18 flits (4 B) |
| VCs | 4 per vnet |
| Router buffers | 1 flit per VC for control vnets, 4 flits for the data vnet |
| Multicast | Source replication at the NI, copies in ascending destination order |
| Clock | Network and cores at 2 GHz; all times in network cycles |
| Simulator | gem5 v26 Garnet 3.0 (cycle-level router and link model) |

### 1.5 Modeling assumptions

| # | Assumption | Why | Where it can break |
|---|---|---|---|
| A1 | Time is slotted; links and NIs move ≤ 1 flit per cycle | How Garnet works | — |
| A2 | Each NI has one FIFO queue per vnet, served by message | Garnet buffers each vnet separately | Vnets share one injection link (failure mode 3 in section 7.5) |
| A3 | Messages arrive at an NI queue as a **GGeo** process (rate λ, burst probability p_b) | Mandal et al.'s bursty-traffic model | Strongly phased traffic; fitted per run |
| A4 | A message is unicast with probability 1 - α, or a K-way multicast, K from the measured fan-out distribution | Measured from traces | — |
| A5 | A multicast's copies leave in a **fixed order**, limited by the NI's VCs (V = 4) and credits, each returned R = 5 cycles after use | Derived from Garnet's pipeline; matches measurement | VCs still busy from the previous multicast (failure mode 1) |
| A6 | Different queues see independent arrivals | Standard decomposition approximation | Correlated phases across nodes |
| A7 | Buffers are infinite: no backpressure from full routers | Accurate below saturation | Near saturation: with multicast, Garnet saturates at about 70% of the model's utilization (section 7.4) |
| A8 | Traffic is open-loop: arrivals don't depend on the network's state | Simplification | Self-paced traffic such as lock handoffs (failure mode 2) |
| A9 | Routing is deterministic (XY), so every flow's path is known | Garnet `--routing-algorithm=1` | Adaptive routing would need path probabilities |
| A10 | A link's arrivals are about as random as coin flips each cycle; only packets from **different input ports** can collide | Each input delivers ≤ 1 flit per cycle (section 2.5) | Very high load; long packets |
| A11 | Router-internal effects (switch allocation, head-of-line blocking) are not modeled separately | Keeps one formula per link | Under-predicts by up to ~8% at link load 0.5 (section 7.3) |

**Not modeled:** priority or weighted arbitration (Garnet is round-robin),
adaptive routing, wormhole backpressure, and incast (replies converging
on the requester; removed from scope).


---

## 2. The model

This part derives every equation step by step. You need only basic
probability (means, variances).

### 2.0 The question

A cache sends one message to K other caches: a **multicast**. In
cache coherence this happens when a line is written while others share
it (invalidations), or, under a broadcast protocol, on every miss. A write
finishes only when *every* copy has arrived. So the question is:

> **When does the last copy of a multicast arrive, under realistic,
> bursty traffic?**

Many NoCs, including gem5's Garnet, send a multicast by **source
replication**. The sending network interface (NI) makes K copies and
pushes them into the network one after another, through its single
injection link. So the latency of copy j has three parts:

```
T_j  =  [wait at the sending NI]  +  [waits at links on its path]  +  [zero-load trip]
          sections 2.2-2.4            section 2.5                    section 2.7
```

and the multicast completes when the slowest copy arrives:

```
C  =  max over j of T_j                                                     (0)
```

### 2.1 Setting and notation

**The network:**

- A 4×4 mesh of routers with XY routing: a packet goes along its row,
  then along its column.
- Each node has one NI. The NI connects a cache controller to its router.
- Time is **slotted**: one slot is one clock cycle.
- A link moves at most **one flit per cycle**. A packet is L flits:
  L = 1 for control messages, 5 for data with 16-byte flits.

The modeling assumptions A1-A11 are in section 1.5.

**Symbols:**

| Symbol | Meaning |
|---|---|
| λ | messages arriving per cycle at a queue |
| p_b | GGeo burst probability: chance the next message comes in the *same* cycle |
| K, L | copies per multicast, flits per copy |
| N | number of messages arriving in one cycle |
| B | busy time one message puts on its NI queue (cycles) |
| A | busy time arriving in one cycle (sum of the B's of that cycle's messages) |
| U | backlog: work left in the queue at the start of a cycle |
| ρ | utilization: fraction of cycles the server is busy |
| C² | squared coefficient of variation: variance / mean² of a quantity. For gaps between arrivals: 1 = Poisson-like, > 1 = bursty |

**How the quantities feed each other.** Every intermediate quantity below
exists to feed the next one. Each equation is followed by a short
**Why we need it** note.

```
λ, p_b ──► σ, E[N], E[N(N-1)] ─┐
K, L ──► copy schedule τ_j ──► B, E[B], E[B²] ─┤
                                               ▼
                       ρ, E[A(A-1)] ──► E[U] ──► W_head ──► W_j = W_head + τ_j ─┐
                                                                              ▼
link flows ──► λ_i, λ_ℓ, T_ℓ, C²_S ──► h_ℓ, W_ℓ ─────────────────► T_j ──► C = max T_j
```

### 2.2 Arrivals: bursty traffic, counted per cycle

#### 2.2.1 What GGeo means

After a message arrives, one of two things happens:

- with probability p_b, the next one arrives **in the same cycle** (a burst
  continues);
- otherwise, the next one arrives after a geometrically distributed
  number of cycles (a fresh, memoryless wait).

p_b = 0 gives ordinary random (Bernoulli) arrivals; larger p_b means
clumpier traffic.

#### 2.2.2 The same process, counted per cycle (derivation)

Waits are memoryless, so each cycle independently either **has arrivals**
(with some probability σ) or not. If a cycle has arrivals, their number G
keeps growing by one with probability p_b each time, so G is geometric on
{1, 2, 3, …}:

```
P(G = n) = (1 - p_b) p_b^(n-1),    E[G] = 1/(1 - p_b)
```

The average rate must be λ, so σ · E[G] = λ:

```
σ = λ (1 - p_b)                                                             (1)
```

**Why we need it:** GGeo is defined by gaps between messages, but a queue
evolves cycle by cycle. σ turns the gap description into a per-cycle one:
"does this cycle bring any messages?" Everything in section 2.3 is computed
per cycle, so we need this translation first.

Let N be the number of messages arriving in a cycle: N = 0 with
probability 1 - σ, and N = G otherwise. The model needs N's mean, and the
mean number of ordered pairs of messages that share a cycle, E[N(N-1)].
From G's generating function G(z) = (1 - p_b) z / (1 - p_b z), whose second
derivative at z = 1 is 2p_b/(1 - p_b)²:

```
E[N]      = σ E[G]          = λ                                             (2)
E[N(N-1)] = σ E[G(G-1)]     = 2 λ p_b / (1 - p_b)                           (3)
```

**Why we need E[N]:** it is only a consistency check: the per-cycle
description must give back the rate λ we measured.

**Why we need E[N(N-1)]:** N(N-1) counts the ordered pairs of messages
that land in the *same* cycle. Waiting happens only when two things want
the server at once, so this is the raw measure of "collisions" caused by
burstiness. It is used twice:

- in (6), to compute how lumpy the arriving work is, which sets the
  backlog E[U] in (8);
- in (9), divided by E[N], to give how many *other* messages a typical
  message shares its arrival cycle with.

Why not just the variance? A queue's wait depends on pairs arriving
together, and E[N(N-1)] counts exactly those (Var[N] mixes in the mean).
E[N(N-1)] is zero without bursts: with p_b = 0 at most one message comes
per cycle, nobody collides, and a 1-flit queue never waits.

*Check:* the gaps of this process have C² = 2/(1 - p_b) - λ - 1, exactly
Mandal et al.'s formula for GGeo.

### 2.3 The source NI: a queue that receives batches

#### 2.3.1 The work a message brings

A unicast occupies its NI queue for its L flits. A multicast occupies it
until its **last copy is sent**, which section 2.4 computes as B(K, L). In
general, a message's busy time is

```
B = L         (unicast, probability 1 - α)
B = B(K, L)   (multicast, probability α)                                    (4)
```

with mean E[B] and second moment E[B²] over the message mix.

**Why we need B:** the NI queue does not care how many *messages* wait,
only how many *cycles of work* are ahead. A 15-way multicast blocks the
messages behind it for B(15, 1) = 18 cycles, not 1, so we measure load in
busy cycles.

**Why we need E[B] and E[B²]:**

- **E[B]** gives the average load: utilization ρ = λE[B] in (5).
- **E[B²]** captures how *unequal* the messages are. A rare 18-cycle
  multicast among 1-cycle unicasts barely moves E[B] but raises E[B²] a
  lot, and the messages that arrive behind it wait a long time. Queueing
  waits always depend on second moments, not just means.

#### 2.3.2 The work arriving in one cycle (derivation)

The work arriving in a cycle is a random sum, A = B_1 + … + B_N.
Conditioning on N, the sum has N squared terms B_i², each with mean E[B²],
and N(N-1) cross terms B_i·B_j, each with mean E[B]²:

```
E[A]  = E[N] E[B] = λ E[B] = ρ                                              (5)
E[A²] = E[N] E[B²] + E[N(N-1)] E[B]²
E[A(A-1)] = E[A²] - E[A] = λ (E[B²] - E[B]) + E[N(N-1)] E[B]²               (6)
```

ρ = E[A] is the NI's utilization. The queue is stable only if ρ < 1.

**Why we need A:** the queue is fed per cycle, so we lump everything that
arrives in a cycle into one amount of work A. This lets us write the whole
queue as one simple recursion, (7).

**Why we need E[A] = ρ:** it is the fraction of cycles the NI is busy. It
tells us whether the queue is stable (ρ < 1) and how close it is to
saturation, which appears as the 1/(1 - ρ) blow-up in (8).

**Why we need E[A(A-1)]:** it measures how *lumpy* the work arriving per
cycle is. The server removes exactly 1 unit per cycle, so if work always
came in single units (A ∈ {0, 1}), A(A-1) = 0 and nothing would ever wait.
Every unit beyond the first in a cycle has to wait, and E[A(A-1)] counts
those excess pairs. Its two terms name the two causes:

- **λ(E[B²] - E[B]):** a *single* message bringing many cycles of work
  (a multicast).
- **E[N(N-1)]E[B]²:** *several* messages arriving together (a burst).

#### 2.3.3 Backlog: how much work is waiting (derivation)

Let U be the work in the queue at the start of a cycle, before that
cycle's arrivals. Add the arrivals, V = U + A. One unit of work is served
if there is any:

```
U' = V - 1 + 1{V = 0}        (next cycle's backlog)                         (7)
```

1{V = 0} is 1 when the queue is empty, and 0 otherwise. A is independent
of U: U depends only on earlier cycles.

**Why we need U:** under first-come-first-served, a new message must wait
for all work already in the queue. So the backlog it finds on arrival is
the main part of its wait.

**Mean of (7).** In steady state E[U'] = E[U]. So ρ - 1 + P(V = 0) = 0,
and P(V = 0) = 1 - ρ: the server is idle a fraction 1 - ρ of the time.

**Why the mean step:** it gives P(V = 0), the idle probability, which we
need to get rid of the awkward 1{V = 0} term in the next step. (The mean
of (7) alone cannot give E[U]: the U's cancel.)

**Square (7).** *Why square:* the U's cancel in the mean, but squaring
leaves a term 2ρE[U] that does not cancel, so we can solve for E[U]
without ever finding U's full distribution. When V ≥ 1, U'² = (V - 1)². When V = 0, U' = 0 but
(V - 1)² = 1. So U'² = (V - 1)² - 1{V = 0}. Taking means, using
E[U'²] = E[U²] and E[V²] = E[U²] + 2ρE[U] + E[A²]:

```
0 = 2ρE[U] + E[A²] - 2(E[U] + ρ) + 1 - (1 - ρ)
```

Solving for E[U]:

```
E[U] = E[A(A-1)] / (2 (1 - ρ))                                              (8)
```

This is the standard discrete-time queue result, derived directly. Two
things in it make waits grow:

- **E[A(A-1)]:** big batches and bursts.
- **1/(1 - ρ):** waiting explodes as the NI nears full utilization.

#### 2.3.4 How long the first copy waits (derivation)

A message arriving in some cycle waits for the backlog U, plus the work of
*other* messages that arrived in the same cycle and are served first.

- **Same-cycle messages.** A message is more likely to land in a busy
  cycle (length bias). The expected number of *other* messages in its
  cycle is E[N(N-1)]/E[N] = 2p_b/(1 - p_b).
- **Order within the cycle.** It is random, so each of those is ahead with
  probability ½, and each brings E[B]:

```
Δ = p_b E[B] / (1 - p_b)                                                    (9)
W_head = E[U] + Δ                                                           (10)
```

W_head is the wait of a message's **first copy**, unicast or multicast.

**Why we need Δ:** E[U] is the backlog at the *start* of a cycle, before
its arrivals. But a bursty message usually arrives together with others,
and some of them are queued ahead of it. E[U] alone misses that, so Δ adds
it. Without bursts (p_b = 0), Δ = 0.

**Why we need W_head:** it is the common starting point for every copy of
the message. Section 2.4 then adds each copy's own time τ_j on top.

### 2.4 The copies of a multicast: who leaves when

#### 2.4.1 The mechanism in Garnet

When an idle NI sends a K-way multicast, the time until its last copy gets
a virtual channel (VC) is, measured by K:

| K | 2-4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 15 |
|---|---|---|---|---|---|---|---|---|---|
| cycles | 0 | 5 | 6 | 7 | 8 | 10 | 11 | 12 | 17 |

The jumps at K = 5 and 9 reveal the mechanism:

1. Each copy needs one of the vnet's **V = 4 virtual channels**.
2. The link sends **one flit per cycle**, rotating over the VCs that have
   a flit ready.
3. Each flit needs a **credit** (a free slot in the router's input buffer:
   b = 1 flit on control vnets, 4 on the data vnet). The credit comes back
   **R = 5 cycles** after the flit was sent.
4. A VC is free again when its copy's **tail flit's credit** comes back,
   also R = 5 cycles after the tail was sent: that credit carries the
   "VC free" signal.

R is not fitted: it follows from the router pipeline (section 2.6.1). So
the first 4 copies grab VCs at once and go out back to back. Copy 5 waits
for the first VC to come back, and so on.

#### 2.4.2 The schedule (derivation)

For 1-flit copies, let a_j be the cycle copy j gets a VC, and s_j the
cycle it is sent, both counted from the first copy:

```
a_j = 0                       for j ≤ V           (a free VC is available)
a_j = s_(j-V) + R             for j > V           (reuse the VC of copy j-V)
s_1 = 0
s_j = max(a_j, s_(j-1) + 1)                       (need a VC and a free link cycle)   (11)
```

For longer copies, the same rules are applied flit by flit, with each
flit also waiting for a credit (`ni_schedule` in the code). On a vnet with
1-flit buffers a VC can send only one flit every R = 5 cycles, so a 5-flit
packet's tail leaves 20 cycles after its head.

**Why we need a_j:** a copy cannot be sent until it holds a VC. With only
V = 4 VCs, copies 5 and later must wait for an earlier copy's VC to be
released, and a_j records when that happens.

**Why we need s_j:** the question is when the *last* copy arrives, so we
need to know when *each* copy leaves, not just the first. s_j is the
extra delay of copy j over copy 1, caused by the VC limit and by the link
sending one flit per cycle. It is the part that is the same at zero load:
even an idle NI releases copy 15 seventeen cycles after copy 1.

**Example: K = 15, L = 1, V = 4, R = 5:**

```
s = 0, 1, 2, 3,  5, 6, 7, 8,  10, 11, 12, 13,  15, 16, 17
```

Four copies go out at once, then a pause for VCs, again and again. The
last copy leaves at cycle 17. This reproduces *every* value in the table
above, and the 2-flit case (K = 15 → 32 cycles), where each copy's second
flit also waits for a credit.

#### 2.4.3 What the NI model uses from the schedule

```
τ_j    = cycle copy j's tail flit leaves the NI, counted from the first flit
W_j    = W_head + τ_j              time until copy j has fully left the NI  (12)
B(K,L) = τ_K + 1                   busy time, used in (4)                   (13)
```

For 1-flit copies τ_j = s_j. For longer copies τ_j also includes the
copy's own flits, interleaved with the other copies' flits on the link:
for K = 15 five-flit copies on the data vnet, τ = 16 17 18 19 | 37-40 |
58-61 | 75 76 77.

**Why we need W_j:** it is the NI part of copy j's latency in (19). It
ends when the copy's tail leaves, because a copy is delivered when its
tail arrives.

**Why we need B(K, L):** the schedule also tells us how long a multicast
holds the NI queue: the messages behind it can't start until its last
copy is out. That busy time feeds back into E[B] and E[B²] in section 2.3.
So the schedule affects both the multicast's own copies and everyone
queued behind it.

This is the step earlier models lack:

- **Moadeli:** every s_j = 0; copies leave through parallel ports.
- **Mandal-style:** no s_j at all; every copy gets the same average wait.
- **BMQ:** the *last* copy leaves 17 cycles after the first.

### 2.5 The network: waiting at links

After leaving the NI, a copy crosses links. A link sends one flit per
cycle, so it can make a packet wait only when **another packet wants the
same link at the same moment**. Think of each link as a checkout counter
with a few doors: its input ports.

#### 2.5.1 Load on a link

Walk every flow (unicast flows and multicast copy flows) along its XY
path. For each link ℓ, record the packet rate entering it through each
input port i (up to three neighbor routers, plus the local NI). With
r = a flow's packets per cycle:

```
λ_i    = Σ r over flows entering ℓ through port i      packets per cycle  (14)
λ_ℓ    = Σ_i λ_i                                       total rate on ℓ
T_ℓ    = Σ r·L / λ_ℓ                                   mean packet length = service time
C²_S,ℓ = (Σ r·L² / λ_ℓ) / T_ℓ² - 1                     spread of packet lengths
ρ_ℓ    = λ_ℓ T_ℓ                                       link utilization
```

**Why we need these:** they are everything the link-wait formula (17)
uses.

- **λ_i per port:** which door each packet comes through. This decides
  which packets can collide (2.5.2).
- **T_ℓ:** how long each packet holds the link (L cycles for L flits).
- **C²_S,ℓ:** how unequal the packets are. A 5-flit data packet among
  1-flit control packets makes the ones behind it wait, as E[B²] did at
  the NI.
- **ρ_ℓ:** how close the link is to saturation.

#### 2.5.2 Which packets can collide (derivation, single-flit packets)

Each input port delivers **at most one flit per cycle**. Let A_i ∈ {0, 1}
be port i's flit in a cycle (P = λ_i), and A = Σ A_i.

This is the same queue as section 2.3.3, so (8) applies. With independent
ports, A(A-1) counts pairs of *different* ports delivering in the same
cycle (A_i² = A_i, so no port pairs with itself):

```
E[A(A-1)] = (Σλ_i)² - Σλ_i²                                                 (15)
```

**Why we need it:** waiting needs two flits wanting the link in the same
cycle, and (15) counts those collisions. (Σλ_i)² counts all pairs;
subtracting Σλ_i² removes the impossible pairs of a port with itself.

A flit waits for the backlog plus same-cycle flits served first. Adding
those gives the exact single-flit wait [Karol, Hluchyj & Morgan 1987]:

```
W_flit = [(Σλ_i)² - Σλ_i²] / (2 ρ (1 - ρ))                                  (16)
```

**Key consequence: a link fed by only one port never queues.** With one
port, (Σλ)² = Σλ², so W = 0. Packets through the same door arrive already
in single file. Queueing happens only where traffic **merges**.

#### 2.5.3 The link wait: merge factor × classic wait

Define the link's **concentration**

```
h_ℓ = Σ_i λ_i² / (Σ_i λ_i)²
```

h is the chance that two randomly picked packets on ℓ came through the
same port. Such pairs can't collide, so **1 - h_ℓ** is the fraction of
pairs that can: the **merge factor**. The link wait is

```
W_ℓ = (1 - h_ℓ) · ρ_ℓ T_ℓ (1 + C²_S,ℓ) / (2 (1 - ρ_ℓ))                      (17)
      └ merge ┘   └────── classic queue wait ──────┘
```

**Reading (17):**

- **ρT(1 + C²_S)/(2(1 - ρ)):** the textbook wait for random arrivals
  (Pollaczek-Khinchine). T(1 + C²_S)/2 is the remaining time of the packet
  being served; you're more likely to arrive during a long one, so mixed
  lengths raise it. ρ/(1 - ρ) is how much work tends to be ahead; it
  explodes as the link nears full use.
- **(1 - h), the merge factor:** removes the pairs that come through the
  same port and so can't collide.

| Ports feeding ℓ | h | 1 - h |
|---|---|---|
| One | 1 | **0**: no waiting |
| Two, equal rates | ½ | ½ |
| n, equal rates | 1/n | (n - 1)/n |
| One dominant (0.14 vs 0.01) | 0.88 | 0.12 |

*Check:* for single-flit packets (T = 1, C²_S = 0, ρ = Σλ_i), (17) is
exactly (16) rewritten: [(Σλ)² - Σλ²]/(2ρ(1-ρ)) = (1 - h)·ρ/(2(1 - ρ)).

**Example: link 5 → 6.** Fed by the local NI (λ₁ = 0.06) and the link from
router 4 (λ₂ = 0.09): λ = 0.15, T = 3 flits, C²_S = 0.6, ρ = 0.45.

```
h      = (0.06² + 0.09²) / 0.15²          = 0.52      merge factor 0.48
classic = 0.45 · 3 · 1.6 / (2 · 0.55)      = 1.96 cycles
W      = 0.48 · 1.96                       ≈ 0.94 cycles
```

**Why we need W_ℓ:** it is each link's contribution to a copy's latency,
summed along the path in (18)-(19).

**Solving:** each link uses only its own rates and lengths, so every W_ℓ
is computed once. There is no iteration.

#### 2.5.4 What (17) assumes, and the alternative we tested

**Assumptions:**

- **Arrivals at a link are random** (as random as coin flips each cycle).
  A link's inputs are other links, each delivering at most one flit per
  cycle, so a burst at the source can't reach a link as a same-cycle
  clump. Even the NI's 15-copy batch leaves one flit per cycle.
- **Exact** for 1-flit packets with independent inputs; an approximation
  for multi-flit packets, whose flits arrive on consecutive cycles.
- **Not modeled:** burstiness carried from link to link at high load, and
  blocking that spreads back upstream (wormhole backpressure).

**The alternative: a two-moment decomposition** (`scripts/bmq_model.py --net full`, Phase 1 code).
Following Mandal et al., each link is a GGeo/G/1 queue,
W = (T/2)[(C²_A - 1) + ρ(C²_A + C²_S)/(1 - ρ)] [Kouvatsos 1994; Mandal
WRR paper eqs. 4-5]. Its arrival burstiness C²_A is carried through the
network: departure smoothing [Mandal WRR eq. 18], splitting
C'² = pC² + (1 - p), rate-weighted merging (Whitt; Mandal WRR eq. 19),
and a cap C² ≤ 1 - λ per input, iterated to a fixed point. We replaced it
with (17) because it is much harder to explain and was less accurate where
the two differ:

**High-load test.** Garnet_standalone synthetic traffic on the same 4×4
XY mesh (`scripts/run_synth.py`, `scripts/compare_linkmodels_synth.py`):

- **Patterns:** uniform random and transpose.
- **Packets:** 1-flit only, or a mix of 1- and 5-flit.
- **Load:** injection swept from light to past saturation.
- **Settings:** Bernoulli injection, 200k cycles per run.
- **Zero-load latency:** on the lightest run (load 0.005) the derived
  timing of section 2.6.1 matches Garnet to within 0.01 cycles.

Mean unicast latency error on the 24 runs below saturation (peak link ρ
up to 0.97):

| Runs | (17), our model | Two-moment | No link waits |
|---|---|---|---|
| uniform random, 1-flit (7) | 2.5% | 2.5% | 5.9% |
| uniform random, mixed (7) | **2.4%** | 4.3% | 10.4% |
| transpose, 1-flit (4) | 0.7% | 0.7% | 3.8% |
| transpose, mixed (6) | **2.0%** | 37.2% | 15.6% |
| **All 24** | **2.0%** (max 8.9%) | 11.4% (max 131%) | 9.3% |
| Link-wait error, cycles | **0.27** | 2.06 | — |

- **1-flit packets:** both link models coincide (both reduce to the exact
  (16)).
- **Mixed packets:** (17) stays within 3% on average. Transpose loads a
  few links heavily, and many of them have one input. (17) correctly
  gives no wait there, while the two-moment model predicts congestion from
  its C²_S term: +131% at ρ = 0.97.
- **Saturation:** all six runs that saturate Garnet (latency in the
  thousands) are predicted saturated (ρ ≥ 1). No unsaturated run was
  predicted saturated.
- **Remaining gap:** (17) under-predicts as load grows, by about 8% at
  uniform-random ρ = 0.5. The likely cause is router effects it leaves
  out: switch allocation and head-of-line blocking at input ports.

**On the 41 coherence load-sweep runs** (multicast completion, mean
absolute error, with each trace's exact traffic as input, in the Phase 1
trace-driven code with the earlier fitted T0): 11.6% with
(17), 10.4% with the two-moment model, and 12.1% with no link waits. The two differ by under 1.6 cycles on 35 of the
41 runs. The rest are narrow-link runs with 18-flit packets.

The synthetic tester injects random, not bursty, traffic, and holds
packets back while its NI is busy. So these runs test the link model, not
burstiness or source queueing.

### 2.6 Putting it together: latency of every copy

**A unicast packet** s → d, H hops, L flits:

```
T_sd = W_head + τ(L) + Σ_(links ℓ on path) W_ℓ + T_hop(H)                   (18)
```

where τ(L) is when a lone L-flit packet's tail leaves the NI (0 for 1
flit; 5 for 5 flits on the data vnet; 20 for 5 flits on a 1-flit-buffer
vnet).

**Copy j of a multicast** s → {d_1 < … < d_K}:

```
T_j = W_head + τ_j + Σ_(ℓ on path to d_j) W_ℓ + T_hop(H_j)                  (19)
T_hop(H) = t_NI + (H + 1) t_router + (H + 2) t_link = 4 + 2H
```

T_hop is the zero-load time from a flit leaving the NI to the destination
NI receiving it: one NI stage, H + 1 routers and H + 2 links (NI → router,
H router-to-router links, router → NI).

**Completion of the multicast:**

```
C = max over j of T_j                                                       (20)
```

The maximum is over the *whole* latency, so a copy that left early but
travels a long path can be the last to arrive.

**Why each term is there:**

- **W_head:** time waiting behind other messages at the NI (section 2.3).
- **τ_j:** time pushing out this multicast's own earlier copies, and this
  copy's own flits (section 2.4).
- **Σ W_ℓ:** time waiting behind other packets at links (section 2.5).
- **T_hop(H_j):** the trip itself with no waiting: NI stage, router
  pipelines and link hops.

**Why the max:** the coherence action (e.g. a write's invalidations)
finishes only when *every* copy has arrived, so the slowest copy decides.
This is why the per-copy times τ_j matter: the average copy can look
fine while the last one is late.

**Caveat.** (20) is a maximum of averages. Real waits vary, and the
expected maximum is at least the maximum of the expectations. So (20)
slightly underestimates when link waits are large. At our loads they are
small.

#### 2.6.1 Router timing from the pipeline

Every timing constant comes from the router design, not from measured
latencies. Garnet's configuration: an NI stage of t_NI = 1 cycle, a
t_router = 1-stage router (route, switch allocation and traversal in one
cycle), t_link = 1 cycle per link. Following one flit sent by the NI at
cycle t:

| Cycle | What happens |
|---|---|
| t | The NI sends the flit (it is on the link the next cycle) |
| t + 2 | It reaches the first router and wins the switch the same cycle |
| t + 3 | It is on the next link; the router sends a credit back upstream |
| t + 4 | The next router has it (2 cycles per hop); the credit reaches the NI |
| t + 5 | The NI can use the credit (it reads credits after sending each cycle) |

So:

```
T_hop(H) = t_NI + (H + 1) t_router + (H + 2) t_link           = 4 + 2H
R        = t_NI + t_router + 2 t_link + 1                      = 5
```

The credit round trip R limits a VC to b flits every R cycles, which is
why packets longer than the buffer stall at the NI.

**Check against Garnet.** The minimum latency Garnet measured for each
packet type, at every hop count 0-6, matches T_hop + τ exactly:

| Packet | Buffer b | Derived | Measured minimum |
|---|---|---|---|
| 1 flit, any vnet | 1 or 4 | 4 + 2H | 4 + 2H |
| 5-flit data (16 B flits) | 4 | 9 + 2H | 9 + 2H |
| 5-flit writeback, request vnet (16 B) | 1 | 24 + 2H | 24 + 2H |
| 18-flit data (4 B flits) | 4 | 25 + 2H | 25 + 2H |
| 2-flit control (4 B flits) | 1 | 9 + 2H | 9 + 2H |

An earlier version fitted T0 = 4 + 2H + 1.25(L - 1) and R = 5 + (L - 1)
to calibration runs. That formula was wrong for long packets on 1-flit
buffers (9 + 2H instead of 24 + 2H for writebacks), which the derivation
gets right. Switching to the derived timing improved every result
(section 7).

#### 2.6.2 High-load correction: VCs held between messages

Sections 2.3-2.5 assume every message finds all V VCs at its NI free, and
that a packet waits at a router only for the switch. Both fail at high
load (section 7.4): tracing gem5's source NIs showed the missing time is
waiting at the NI. A copy keeps its VC until R cycles after its tail
leaves, *plus* however long the tail waits in the first router, so
between messages the V VCs cap an NI at V / (VC time) copies per cycle
(0.8 at zero load). The correction (`bmq.VC_HOLD`, on by default) adds
three pieces, all derived from the router pipeline, none fitted:

1. **NI service time limited by VCs.** A message's VC time is
   H = Σ_j (τ_j + R − a_j) + K·D, where a_j is when copy j got its VC
   (from the schedule) and D the mean wait of the queue's copies at the
   injection link and the first router. The NI service time becomes

   ```
   B_eff = max(B, H / V)                                              (13a)
   ```

   and B_eff replaces B in (5)-(10), so ρ = λE[B_eff] also saturates
   where the VCs run out.

2. **Shared injection link.** A node's vnets share one link into the
   router. It is a channel like the others, with the vnets as its input
   ports in the merge factor (one vnet's copies are already serialized
   by the schedule).

3. **Waiting for a downstream VC (backpressure).** A packet crossing
   channel c needs one of the V VCs of its vnet at the next router and
   holds it for S = L − 1 + R_c + D_next cycles: its flits, the credit
   turnaround R_c, and the time it then waits at that router (switch
   wait plus its own VC wait at the next channel). Garnet applies
   credits before switch allocation, so R_link = t_router + 2 t_link = 3
   between routers and R_eject = 2 t_link = 2 into the destination NI.
   Each (channel, vnet) is an M/D/V pool:

   ```
   W_vc = (1 − h) · C(V, a) · S / (V − a) / 2,     a = λ S             (17a)
   ```

   with C the Erlang C probability that all V VCs are busy and h the
   merge factor (packets through one input port arrive at most one per
   cycle, so on their own they never queue for VCs). D_next depends on
   the downstream waits, so (17a) is iterated from D = 0: this is the
   backward pass. A channel with a ≥ V saturates.

W_vc is added to every link term of (18)-(19), and the injection-link
wait to every copy. With the correction off, all three pieces vanish and
the model is exactly sections 2.3-2.6.

### 2.7 Inputs: traffic and router parameters

The model's input is a **parametric description of the traffic**, the
same kind of input as other analytical NoC models (Mandal: rate and
burstiness per traffic class; Moadeli: rate, multicast fraction, message
length, uniform destinations):

| Parameter | Meaning |
|---|---|
| λ | Messages per cycle per source node |
| α | Fraction of messages that are multicasts |
| K | Multicast fan-out: a number, or a distribution |
| Packet mix | Packet length L and vnet: {(L, vnet): probability}, separately for unicasts and multicasts |
| Pattern | Unicast destinations: uniform, hotspot, transpose, bit-complement, neighbor, tornado, shuffle (gem5's synthetic patterns) |
| p_b | Burstiness: one value, or one per (source, vnet) queue |
| Per-node values *(optional)* | λ, α, K and the packet mixes for individual nodes, for non-uniform traffic such as a hotspot bank |

From these the model computes the expected rate of every kind of
message: unicasts split over the pattern's destinations, and multicasts
to K random distinct routers (all 15 others for a broadcast). Everything
in sections 2.2-2.6 follows from those rates.

**Router parameters**, from the router design (Garnet's configuration
here), never fitted:

| Parameter | Value |
|---|---|
| V | 4 VCs per vnet |
| Buffer depth b | 1 flit per VC for control vnets, 4 for the data vnet |
| t_NI, t_router, t_link | 1 cycle each |
| Derived | R = 5 (credit and VC return), T_hop(H) = 4 + 2H (section 2.6.1) |

**Where parameter values come from:** a designer picks them for a
what-if study. For validation we set the router parameters to Garnet's
configuration and fit the traffic parameters to a gem5 trace (section 5).
Garnet's measured latencies are only ever used as ground truth.

### 2.8 Checks that the math is right

| Check | Result |
|---|---|
| No multicast, single flits | (8)-(10) give W = p_b/((1 - p_b)(1 - λ)): exactly the GGeo/D/1 result used by Mandal et al. |
| Bernoulli arrivals, single flits | W = 0: a queue serving 1 flit per cycle never backs up with ≤ 1 arrival per cycle |
| Monte Carlo of the assumed NI queue (`scripts/ni_montecarlo.py`) | Head, average-copy and last-copy waits match (8)-(12) within ≤ 0.1 cycles, for p_b = 0-0.6 and K = 4-15 |
| Single-flit links | (17) equals the exact wait (16) [Karol, Hluchyj & Morgan]; a link with one input gives W = 0 |
| Copy schedule (11) | Reproduces every measured idle-NI VC acquisition time in Garnet (K = 2-15, L = 1 and 2) |
| Zero-load latency (18) | Matches Garnet's minimum measured latency for every packet type and hop count (section 2.6.1) |
| Fixed K vs. geometric K | The intra-batch wait (E[K²] - E[K])/(2E[K]) is 7 for fixed K = 15, but 14 for a geometric batch of mean 15. That's why plain GGeo over-counts batch waiting |

### 2.9 Worked example: one broadcast under MOESI_hammer

Directory node 21 (router 5, near the mesh center) broadcasts a 1-flit
`GETS` to the 15 other caches. The NI is idle, so W_head ≈ 0 and links are
almost empty.

**Step 1. Copy send times, from (11):**

```
s = 0 1 2 3 | 5 6 7 8 | 10 11 12 13 | 15 16 17
```

**Step 2. Hops to each destination** (XY, from router 5), in copy order:

```
2 1 2 3 1 0 1 2 2 1 2 3 3 2 4
```

**Step 3. Each copy's arrival, from (19) with W ≈ 0:**
τ_j + T_hop = s_j + 4 + 2H (1-flit copies, so τ_j = s_j):

```
8 7 10 13 11 10 13 16 18 17 20 23 25 24 29
```

- **Mean copy latency:** about 16 cycles.
- **Last copy (completion, (20)):** **29 cycles**. Here the last copy is
  also the farthest (4 hops).

**Compare:**

| | Completion |
|---|---|
| Garnet, hammer swaptions (mostly idle NIs) | **29.2** |
| BMQ on that trace | 27.6 |
| Moadeli (all copies leave at 0) | 14.1 |
| Mandal-style (every copy waits the average) | 21.3 |

---

## 3. The algorithm

Section 2 derives each equation. Here is the whole model as one
procedure, the way `scripts/bmq.py` runs it.

### 3.1 Inputs and outputs

- **Inputs:**
  - traffic parameters: λ, α, K, packet mixes, pattern, p_b, and
    optionally per-node values (section 2.7);
  - router parameters: mesh size, XY routing, V = 4 VCs, buffer depth b,
    t_NI = t_router = t_link = 1 cycle; from them R = 5 and
    T_hop(H) = 4 + 2H (section 2.6.1).
- **Outputs:**
  - for any multicast, each copy's latency T_j and the completion C;
  - averages over the traffic: unicast latency, mean copy latency, mean
    completion;
  - per NI queue and per link: utilization, so saturation is flagged.

### 3.2 The procedure

```
INPUT:  traffic parameters (λ, α, K, packet mixes, pattern, p_b), router parameters
OUTPUT: T_j for every copy, C = max_j T_j for every multicast, averages

── 0. Expected traffic (section 2.7) ──────────────────────── once
 0. for each source s:
        unicasts:   rate λ(1 - α) · P(L, vnet) · P(pattern sends s → d)
        multicasts: rate λα · P(K) · P(L, vnet), destinations = K distinct
                    routers other than s's (random sets; all 15 if K = 15)
    → expected rate of every message kind (src, dests, L, vnet)
      and the queues (src, vnet) they use

── A. Copy schedule (section 2.4) ─────────── for each (K, L, b) that occurs
 1. 1-flit copies (L = 1):
    s_1 = 0
    for j = 2..K:
        a_j = 0                    if j ≤ V        (a free VC)
        a_j = s_(j-V) + R          otherwise       (reuse copy j-V's VC)
        s_j = max(a_j, s_(j-1) + 1)                (VC ready and link free)   (11)
    τ_j = s_j;  B(K, 1) = s_K + 1                  (batch busy time)          (13)
    L > 1: the same rules applied flit by flit; each flit also needs a
    credit (b per VC), returned R cycles after it was sent (ni_schedule);
    τ_j = send time of copy j's tail flit;  B(K, L) = τ_K + 1

── B. Source NI queue (sections 2.2-2.3) ──────────────── for each queue q
 2. λ_q = total rate into q;  P(K, L) = its mix
    E[B]  = Σ_(K,L) P(K,L) · B(K, L)        E[B²] = Σ P(K,L) · B(K, L)²
 3. ρ = λ_q E[B]                             if ρ ≥ 1: report "saturated"
 4. E[N(N-1)] = 2 λ_q p_b / (1 - p_b)                                     (3)
 5. E[A(A-1)] = λ_q (E[B²] - E[B]) + E[N(N-1)] E[B]²                      (6)
 6. E[U]   = E[A(A-1)] / (2 (1 - ρ))                                      (8)
 7. Δ      = p_b E[B] / (1 - p_b)                                         (9)
 8. W_head = E[U] + Δ                                                     (10)
    W_j    = W_head + τ_j                     copy j fully left the NI    (12)

── C. Network links (section 2.5) ──────────────────────── once, no iteration
 9. for each flow (s, d, L, rate r):                # a multicast adds K flows
        walk its XY path; on each link ℓ add r to λ_(ℓ, port it enters by),
        r·L and r·L² to the length sums
10. for each link ℓ:
        λ_ℓ = Σ_i λ_i,  T_ℓ, C²_S,ℓ from the sums,  ρ_ℓ = λ_ℓ T_ℓ
        if ρ_ℓ ≥ 1: report "saturated"
        h_ℓ = Σ_i λ_i² / λ_ℓ²
        W_ℓ = (1 - h_ℓ) · ρ_ℓ T_ℓ (1 + C²_S,ℓ) / (2 (1 - ρ_ℓ))           (17)

── D. Latency (section 2.6) ────────────────────────── for each message kind
11. unicast s → d:     T = W_head + τ(L) + Σ_(ℓ on path) W_ℓ + T_hop(H)  (18)
    multicast copy j:  T_j = W_head + τ_j + Σ_(ℓ on path to d_j) W_ℓ + T_hop(H_j)   (19)
                       T_hop(H) = 4 + 2H
    completion:        C = max_j T_j                                      (20)
12. averages over message kinds, weighted by their rates
```

### 3.3 The same thing as a picture

```
 parameters (λ, α, K, packets, pattern, p_b) ─► 0. expected traffic
        │                                            │
        │      ┌──── per-queue rate, (K, L) mix ─────┤
        │      ▼                                     │
 V, b, R ─► A. copy schedule ─► B. NI queue ─► W_head ─┐│
                  └─────────────────────── τ_j ────────┤│
                    flows ◄─────────────────────────┼┘
                      ▼                             │
         C. per-link λ_i, T, C²_S ─► W_ℓ ───────────┤
 router pipeline ─► T_hop(H) = 4 + 2H ──────────────┤
                                                    ▼
                           D. T_j = W_head + τ_j + ΣW_ℓ + T_hop
                              C  = max_j T_j
```

### 3.4 Cost

| Step | Work |
|---|---|
| 0 | One entry per (source, destination, packet type) for unicasts; for random multicast sets, 200 sampled sets per source and fan-out |
| A | A short cycle-by-cycle replay, once per distinct (K, L, b): 31 replays on FFT, at most 95 cycles each |
| B | Constant work per queue: at most 48 sources × 3 vnets |
| C | One pass over each flow's path (≤ 6 hops), then one formula per link (≤ 48 mesh links plus injection and ejection) |
| D | One sum over each message type's path |

The model runs in milliseconds for broadcasts and uniform unicast
traffic, and in under 0.1 s with random multicast destination sets (75 ms
for 4- and 15-way multicasts from 16 sources). One gem5 run of the same
system takes minutes to hours. A whole load sweep of the model, like
`bmq.py --sweep`, takes about a second.

### 3.5 The algorithm in a 10-minute talk

| Time | Step | What to say |
|---|---|---|
| 0:00-1:00 | Overview | "A multicast is done when its last copy arrives. Latency = NI wait + copy offset + link waits + trip." |
| 1:00-2:30 | Inputs | A handful of traffic parameters (λ, α, K, packet mix, pattern, p_b), like any analytical NoC model, plus the router's pipeline; traces are only for validation |
| 2:30-4:30 | A | **The main contribution:** 4 VCs, freed 5 cycles after use (the credit round trip), so copies leave at 0,1,2,3 \| 5,6,7,8 \| … \| 17 |
| 4:30-6:30 | B | The batch in a bursty queue. Two causes of waiting: one big message, and many messages at once |
| 6:30-8:00 | C | One line per link: a classic queue wait times a merge factor. A link with one input never queues |
| 8:00-9:00 | D | Add it up; take the max over copies. Worked example: completion 29 vs Garnet 29.2 |
| 9:00-10:00 | Results | Standard benchmarks: 6.4% completion error vs Garnet; load sweep 9.5%; synthetic high load: 2.0% |

---

## 4. Generating traces with gem5

### 4.1 The pipeline

```
benchmark source ──► static binary with ROI markers ──► gem5 SE mode (Ruby + Garnet 4×4)
                                                             │  our trace hook in the NI
                                                             ▼
                          results/<protocol>/<bench>/trace.csv.gz + eject_trace.csv.gz
                                                             │
                     analyze_traces.py (motivation metrics)  │  fit_params.py (fitted    
                                                             ▼   parameters + measured latencies)
```

### 4.2 Simulator setup

| Parameter | Value | Why |
|---|---|---|
| Simulator | gem5 v26.0, syscall-emulation (SE) mode | Cycle-accurate; SE runs one Linux program without booting an OS, so runs are fast |
| Cores | 16 × X86 `X86TimingSimpleCPU`, 2 GHz | Simple in-order core, one memory access at a time; 16 cores fill a 4×4 mesh |
| Caches | Ruby; 32 kB 8-way L1 I and D per core; 16 shared L2 banks of 256 kB (MESI); 16 directories | Ruby models every coherence message explicitly |
| Network | Garnet 3.0, `Mesh_XY`, 4 rows, XY routing (`--routing-algorithm=1`), 16-byte flits | Cycle-level router/link model; gem5's standard NoC |
| Protocols | **MESI_Two_Level** (directory) and **MOESI_hammer** (broadcast) | Multicast only on writes to shared data (MESI) vs. on every miss (hammer) |
| Build | One binary with both protocols: `gem5/build/X86_MESI_hammer/gem5.opt` (`build_opts/X86_MESI_hammer`) | Protocol chosen per run with `--protocol` |
| Required flag | `--access-backing-store` | SE system calls read memory directly, which Garnet can't serve while a line is in flight |

### 4.3 What we added to gem5: the trace hook

154 lines in six files (`git -C gem5 diff`). Enabled with
`--garnet-trace=trace.csv.gz`; it writes two files into the run's output
directory.

**`NetworkInterface.cc`: one line per coherence message.**
- `flitisizeMessage()` is where Garnet turns one message into packets,
  one per destination. The hook records the message **before** the split,
  the only place the full destination set is still visible. It keeps
  pending state per vnet (`PendingTrace`): first-copy start cycle,
  destinations, packet ids. A multicast may need several calls to
  `flitisizeMessage()` while VCs free up.
- `traceMessage()` writes the line once the last copy is queued. It
  pulls the protocol message type (`GETS`, `INV`, …) out of the
  SLICC-generated `print()`.
- `traceEject()`, called where the NI consumes a tail flit (`wakeup()`
  and `checkStallQueue()`), writes one line per delivered packet.

**`GarnetNetwork.cc`: header, ROI markers, ROI cap.**
- `init()` writes the header: node map (which ids are L1s, L2s,
  directories), vnet names, column names.
- `resetStats()` runs whenever the workload calls `m5_dump_reset_stats`,
  and writes `# reset_stats <cycle>`. These lines mark the region of
  interest (ROI). Resets at cycle 0 are simulator startup and are ignored.
- `--roi-max-cycles N` schedules the end of the run N cycles after the
  ROI starts (`exitSimulationLoopClassic`), and writes `# roi_limit`.
  We used 50M cycles to bound long benchmarks.

**`Network.py`, `GarnetNetwork.py/.hh`:** the two options and their
parameters.

**`trace.csv.gz` columns:**

| Column | Meaning |
|---|---|
| `enq_cyc` | Cycle the coherence controller **created** the message |
| `start_cyc` | Cycle the NI started injecting the first copy |
| `end_cyc` | Cycle the last copy got a VC and was queued |
| `src` | Sending node id (MESI: L1 0-15, L2 16-31, directory 32-47) |
| `vnet` | Virtual network |
| `bytes`, `flits` | Message size; flits per copy |
| `type` | Protocol message type |
| `ndest`, `dests` | Fan-out K and the destination ids (`;`-separated) |
| `pkts` | Garnet packet id of each copy, in send order |

**`eject_trace.csv.gz`:** `pkt,cyc,ni` for every delivered packet.
Joining it to `pkts` gives each copy's delivery time.

### 4.4 Workloads

| Benchmark | Suite | Sharing pattern | Input |
|---|---|---|---|
| `s4-fft` | SPLASH-4 | All-to-all transposes | 64K points |
| `s4-lu-cont` | SPLASH-4 | Pivot blocks read by many, then overwritten | 512×512 |
| `s4-radix` | SPLASH-4 | Shared prefix sums, all-to-all key writes | 1M keys |
| `s4-ocean-cont` | SPLASH-4 | Nearest neighbor | 258×258 |
| `s4-water-nsquared` | SPLASH-4 | Read by all, lock-protected updates | 512 molecules |
| `s4-barnes` | SPLASH-4 | Irregular tree sharing | 16K bodies |
| `p-blackscholes` | PARSEC 3.0 | Mostly private | simmedium |
| `p-swaptions` | PARSEC 3.0 | Mostly private | simmedium |
| `p-fluidanimate` | PARSEC 3.0 | Neighbor cells, locks | simmedium |
| `p-streamcluster` | PARSEC 3.0 | Shared centers, barriers | simmedium |
| `micro-*` | **Ours** (`benchmarks/micro/cohmicro.c`) | Controlled sharing patterns (below) | — |

**Why these suites:** SPLASH-4 and PARSEC are the standard published
shared-memory suites. Both are pthreads programs that run in SE mode, and
together they cover regular and irregular sharing, so they give credible
motivation evidence. SPLASH-4 replaces SPLASH-3 (same programs, modern
synchronization).

**Our microbenchmarks** (`cohmicro <kernel> <threads> <iters> [param]`):

| Kernel | What it does | Multicast it creates |
|---|---|---|
| `prodcons` | One producer rewrites a buffer that all others then read | Every write invalidates 15 sharers |
| `readmostly` | All read a shared table; one writer updates an entry now and then | Sparse, wide invalidations |
| `lock` | Test-and-test-and-set spinlock around a short critical section | Invalidation storms on each release |
| `barrier` | Centralized sense-reversing barrier, little work between | Periodic one-to-all bursts |
| `counter`, `falseshare` | Atomic counter; different words of the same lines | (built, not used) |

**Load sweeps for validation.** The microbenchmarks take a "spin" amount
of private work between shared accesses, which sets the injection rate:
- `sweep-{prodcons,barrier,lock}-s{0,25,100,400,1600}`: 15 runs per
  protocol, 16-byte flits.
- `nsweep-{…}-s{0,400}`: 6 runs per protocol with **4-byte flits**
  (`--link-width-bits=32`), so packets are 4× longer and the NIs and
  links are busier.
- Iterations are effectively unbounded; the 50M-cycle ROI cap ends each
  run.

**Changes to benchmark code** (none change what a program computes):
- ROI markers: SPLASH `pthread.m4` (`CREATE`, `WAIT_FOR_END` call
  `m5_dump_reset_stats`); PARSEC through a replacement hooks library
  (`benchmarks/parsec-hooks/hooks.c`).
- Static linking with `libm5.a` (SE mode has no dynamic loader).
- Barnes: fixed an off-by-one heap overflow in `getparam`
  (`malloc(leng)` → `malloc(leng + 1)`); glibc aborted the original.
- Streamcluster: the ROI now starts at the first parallel phase
  (`-DGEM5_PARALLEL_ROI`); PARSEC's ROI began with serial point
  generation, so a 50M-cycle window saw one active core.
- PARSEC uses 15 worker threads (main thread only spawns and joins; SE
  needs one core per thread), fluidanimate 8 (power of two).

**Left out:** all of SPLASH-3 (same programs as SPLASH-4); radiosity,
raytrace, volrend (mostly read-only, slowest); fmm, cholesky,
water-spatial, ocean-noncont, lu-noncont (similar to an included
program); canneal (its serial parse ran 99 min without reaching the ROI).

### 4.5 Running the suite

`scripts/run_suite.py` holds every benchmark's command and runs them in
parallel:

```bash
python3 scripts/run_suite.py --subset --skip-done -j4
```

- `--subset`: the 14 benchmarks above × 2 protocols. Sweeps are selected
  with `--only 'sweep-'`; the narrow sweep adds
  `-- --link-width-bits=32`.
- `--skip-done`: skip runs logged `ok` whose program exited cleanly.
- `--roi-max-cycles` (default 50M); `--dry-run` prints commands.
- Each run writes `results/<protocol>/<bench>/` with the two traces,
  `stats.txt`, `simout`/`simerr`, `config.ini` and `cmd.txt`. The runner
  flags a run whose program exited non-zero.

**One run by hand** (SPLASH-4 FFT, small input, about 25 s), from the
benchmark's folder:

```bash
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
  --garnet-trace=trace.csv.gz --cmd=./FFT --options="-p16 -m12"
```

**Practical notes:**
- Launch long sweeps in their own systemd user unit
  (`systemd-run --user --unit=… -p MemoryMax=11G …`). Jobs started from
  the Claude app die with it; 4 parallel jobs fit in memory, 8 did not.
- Cost: the 28 benchmark runs took 7.8 hours of simulation in total
  (median 17 min each).
- The lab machine had a hidden miner (`process-watchdog.service`) that
  ran `pkill -f benchmark` and killed runs with SIGTERM. It was removed;
  if runs die within seconds, check that it hasn't come back.

### 4.6 Synthetic traffic for high link load

The coherence traces load links at most about 40%, too lightly to test
the link model. `scripts/run_synth.py` runs gem5's synthetic traffic
tester on the same mesh:

- **Build:** `gem5/build/Garnet_standalone/gem5.opt`
  (`scons defconfig build/Garnet_standalone build_opts/Garnet_standalone`,
  then `scons build/Garnet_standalone/gem5.opt -j8`; about 25 minutes).
- **Config:** `configs/example/garnet_synth_traffic.py`, same mesh and XY
  routing; the same trace hook works unchanged.
- **Patterns:** uniform random and transpose; 1-flit packets only (vnet 0)
  or mixed 1- and 5-flit (all vnets); injection rate swept to past
  saturation; 200k cycles per run, about 25 s each.
- **Gotchas:** the tester's `--sim-cycles` is in **ticks**, not cycles
  (500 ticks per 2 GHz cycle), and `--sys-clock=2GHz` puts the tester on
  the network clock so rates are per network cycle.
- **Limits of the stock tester:** it injects random (Bernoulli), not
  bursty, traffic, unicast only, and a refused packet could be lost from
  its single retry slot. So it tests the link model, not burstiness or
  source queueing. Section 4.6.1 removes all three limits.

#### 4.6.1 Our extension: multicast and bursty injection

To test the source NI and the copy schedule at high load with open-loop
traffic, we extended `GarnetSyntheticTraffic` (part of the uncommitted
gem5 diff):

| Option | What it does |
|---|---|
| `--mc-prob α` | Each packet is, with probability α, a **15-way multicast** to every directory except the one on the sender's own router (like a hammer broadcast). The NI replicates it exactly as it does coherence multicasts |
| `--burst-prob p_b` | **GGeo bursts:** after injecting a packet, the tester injects another in the same cycle with probability p_b (so the burst size is geometric, mean 1/(1 - p_b)) |

**How it works:**
- The tester decides per packet whether it is a multicast. A multicast
  carries the sender's own id as its destination and sets address bit 26
  as a flag.
- `Garnet_standalone-cache.sm` (the dummy L1 controller) checks the flag:
  if set, the destination is `broadcast(Directory)` minus the sender's own
  directory; otherwise the usual single directory.
- A queue of generated packets in the tester replaces the single retry
  slot, so no packet is dropped or held back.
- Each packet's address carries a rolling packet number in bits 10-19, so
  no two packets in flight share a cache line.

**Two pitfalls the smoke test caught** (both fixed before any run):
- **Address hashing:** Ruby XORs address bits 20-23 into the directory
  choice (`--xor-low-bit=20`). A multicast flag in bit 20 silently sent
  each multicast to the wrong set (it skipped the neighboring router's
  directory). The flag lives in bit 26.
- **Sequencer aliasing:** the sequencer holds back a second request to a
  cache line that is already in flight. With equal addresses, bursts and
  repeated multicasts were serialized, and the offered load came out
  about 8% low. Unique addresses fix it.

**Smoke test** (20k cycles, rate 0.1, α = 0.2, p_b = 0.5): multicast
share 0.197 (set 0.2), every multicast has 15 destinations that exclude
exactly the sender's directory, mean burst size 1.998 (expected 2.0,
geometric), unicast destinations uniform.

**Running it:**

```bash
python3 scripts/run_synth_mc.py -j4          # the 30-run sweep (section 7.4)
python3 scripts/run_synth_mc.py --compare    # model vs. Garnet → results/garnet_synth_mc.csv
```

Rebuilding after changing the tester or the `.sm` file is incremental
(`scons build/Garnet_standalone/gem5.opt -j8`, a few minutes).

### 4.7 Motivation metrics

`scripts/analyze_traces.py` summarizes each run (chunked reading; peak
about 3 GB) into `analysis.json` and `results/summary*.csv`, and
`scripts/make_figures.py` draws the four motivation figures:

1. **Multicast share:** under MOESI_hammer, about 5% of messages become
   42-47% of packets (fan-out 15); MESI SPLASH is 0.1-2.8% of packets.
2. **Fan-out distribution.**
3. **Multicast cost:** the last copy arrives 6.4-11.8 cycles (30-55%)
   after the average copy under hammer.
4. **Burstiness:** near-Poisson within a program phase, but whole runs
   alternate busy and silent phases (CV up to 55).

---

## 5. Validation inputs: fitting the parameters to a trace

The model runs from traffic parameters (section 2.7). To validate it
against gem5, we **fit** those parameters to a trace and compare the
model's predictions with the latencies Garnet measured. This is
`fit()` in `scripts/fit_params.py`. The trace is reduced to parameters;
the model never sees individual messages. The running example is the
MESI_Two_Level trace `sweep-barrier-s400` (barrier microbenchmark).

### 5.1 Step 1: the region of interest

The `# reset_stats` and `# roi_limit` lines give the ROI: cycles
812,480 to 20,812,480, a window of **20M cycles**. Only messages created
inside it are used, and every rate is per ROI cycle. (Synthetic runs have
no markers; the whole run is used.)

### 5.2 Step 2: when each message reached the NI

`enq_cyc` is when the controller **created** the message. Before the NI
sees it, the protocol adds a fixed send delay (for example 10 cycles for
MOESI_hammer L1 replies, `cache_response_latency`). We estimate the NI
arrival without modifying gem5:

1. Group messages by (vnet, type, sender kind: L1 or home node).
2. The group's delay d_g is its **minimum** `start - enq`, or minimum + 1
   if that value is more common. Some types are sent by two protocol
   actions whose latencies differ by one cycle (MESI L2 DATA: 1 and 2).
3. NI arrival = `enq + min(d_g, start - enq)`.

**Why not the most common value?** At a busy L2 bank, queued replies made
25 the most common `start - enq`, and subtracting it would erase real NI
queueing. Queueing only ever adds to the delay, so the minimum is the
fixed part. NI arrival times are used to fit p_b (step 4) and as the start
of every measured latency (step 5).

### 5.3 Step 3: count, then fit the parameters

From the messages in the ROI:

| Parameter | Fitted as | Example (barrier trace) |
|---|---|---|
| Sources | Every node that sends | 22 nodes (16 L1s, 6 L2 banks) |
| λ | Messages per source per ROI cycle | 6.35×10⁻⁴ on average |
| α | Fraction of messages with `ndest > 1` | 0.009 |
| K | Histogram of `ndest` over multicasts | 15 for 99.9% |
| Unicast packet mix | Histogram of (`flits`, `vnet`) over unicasts | 1-flit on vnets 0, 1, 2 (72%), 5-flit data on vnet 1 (28%) |
| Multicast packet mix | The same over multicasts | 1-flit on vnet 2 (INV) |
| p_b | Per (source, vnet) queue, from its NI-arrival times (below) | 0.121 rate-weighted average |
| Destination node type | Share of unicasts going to L1s vs. home nodes, and the multicast destination type. Each node has its own ejection link, so this sets ejection load | Unicasts split between L1s and L2 banks; multicasts to L1s |
| Router buffer depth | From the vnet names: 4 flits on the data ("response") vnet, 1 elsewhere | |
| Pattern | Uniform | — |

**Two levels of detail:**

- **Global:** one value per parameter, the same for every source.
- **Per node:** λ, α, K and both packet mixes for each source, and p_b
  for each queue. That's still a parametric description (a few numbers per
  node), and it captures where traffic concentrates. In the example, all
  multicasts come from L2 bank 14 (node 30): λ = 2.1×10⁻³, α = 0.059,
  while every L1 has α = 0. Its reply queue (30, vnet 1) has p_b = 0.84:
  after each barrier release, many cores miss at once.

**Fitting p_b (Mandal et al.'s method).**
1. Sort the queue's NI-arrival times t_1 ≤ t_2 ≤ ….
2. Replay them through an imaginary queue that serves one message per
   cycle, and measure its mean wait W_v. For unit service, Lindley's
   recursion has a closed form: with x_k = k - t_k,
   w_n = x_n - min_(k≤n) x_k.
3. For GGeo arrivals, this queue's exact mean wait is
   W_v = p_b / ((1 - p_b)(1 - λ)). Invert it:

```
p_b = W_v (1 - λ) / (1 + W_v (1 - λ))
```

**Fit on messages, not copies.** Fitted on *copies*, the barrier's INV
queue gives p_b = 0.87: 15 copies created in one cycle look like an
extreme burst. BMQ fits on messages (p_b = 0 for that queue) and gets the
copies' timing from the schedule (section 2.4). Fitting p_b to the raw
inter-arrival C² instead was our first attempt; it over-estimated NI
waits 5-10×.

### 5.4 Step 4: the router parameters (not fitted)

| Parameter | Value | Source |
|---|---|---|
| VCs per vnet V | 4 | Garnet config (`vcs_per_vnet`) |
| Router buffers b | 1 flit (control vnets), 4 (data vnet) | Garnet config; the data vnet is the one named "response" in the trace header |
| t_NI, t_router, t_link | 1 cycle each | Garnet's NI stage, router `latency`, `link_latency` |
| R, T_hop(H) | 5 cycles, 4 + 2H | Derived from the above (section 2.6.1) |

Flit width needs no constant of its own: it only changes how many flits a
message has, and L is read from the trace in flits. Nothing here is
fitted to measured latency, so every run, including the ones earlier
used for calibration, is a validation run.

### 5.5 Step 5: what Garnet measured (the ground truth)

Joining each message's packet ids (`pkts`) with the eject trace gives:

| Measured | Definition |
|---|---|
| Unicast latency | NI arrival → delivery |
| Copy latency | NI arrival → delivery of each multicast copy |
| **Completion** | NI arrival → last copy delivered |
| Last copy's VC | `end_cyc` - NI arrival |

The model never sees these; they're only compared with its predictions.

### 5.6 Example: the barrier trace

| | Garnet | Per-node parameters | Global parameters |
|---|---|---|---|
| Multicast completion | 23.8 | **23.1 (-3%)** | 27.4 (+15%) |
| Mean copy latency | 18.0 | 17.8 (-1%) | 17.9 (0%) |
| Last copy's VC | 17.8 | 17.0 (-4%) | 17.2 (-3%) |
| Unicast latency | 16.0 | 14.7 (-8%) | 10.7 (-33%) |

Global parameters spread the multicasts over all 22 sources. The real
traffic sends them all from one bank near the mesh center, and its busy
reply queue makes the unicasts wait. Per-node parameters capture both.

**The NI part on these numbers**, for node 30's INV queue (λ = 2.49×10⁻⁴,
half INV broadcasts, half 1-flit forwards, p_b = 0):

```
copy schedule, K = 15, L = 1:  s = 0 1 2 3 | 5 6 7 8 | 10 11 12 13 | 15 16 17   → B(15,1) = 18
E[B]  = 0.5·1 + 0.5·18   = 9.5
E[B²] = 0.5·1 + 0.5·324  = 162.5
ρ     = λ E[B]           = 0.0024
E[N(N-1)] = 0            (p_b = 0)
E[A(A-1)] = λ (162.5 - 9.5) = 0.038
W_head = E[U] = 0.038 / (2 · 0.9976) = 0.02 cycles
```

## 6. How the model is coded

### 6.1 Files

| File | Lines | Role |
|---|---|---|
| `scripts/bmq.py` | ~370 | **The model.** Parameters in, latencies out (`Params`, `Model`). Needs no trace |
| `scripts/fit_params.py` | ~220 | **Validation:** fits the parameters to a gem5 trace (global and per node), measures Garnet, compares |
| `scripts/check_parametric_synth.py` | ~60 | Validation on the synthetic gem5 sweep, with parameters known from the gem5 options |
| `scripts/ni_montecarlo.py` | ~70 | Monte Carlo check of the NI equations |
| `scripts/run_suite.py`, `scripts/run_synth.py` | ~280, ~80 | gem5 runners (coherence workloads, synthetic traffic) |
| `scripts/run_synth_mc.py` | ~150 | High-injection sweep with multicast and bursts, and its model comparison (`--compare`) |
| `scripts/dse.py`, `scripts/fig_dse.py` | ~250, ~240 | Design-space exploration with the model (section 7.8) and its figures |
| `scripts/analyze_traces.py`, `scripts/make_figures.py` | ~400, ~220 | Motivation metrics and figures from the traces |
| `scripts/baselines.py` | — | The Mandal-style and Moadeli baselines on BMQ's inputs, any mesh (section 7.7) |
| `scripts/compare_models.py`, `scripts/summarize_compare.py` | — | Four-model comparison per mesh (`--mesh N`) and the summary table (section 7.9) |
| `scripts/mesh_cfg.py`, `scripts/run_meshes.sh` | — | Mesh-size switch (`--mesh N`, paths, rate scaling) and the 6×6/8×8 campaign |
| `scripts/bmq_model.py`, `scripts/moadeli_baseline.py`, `scripts/phase1_figures.py`, `scripts/compare_linkmodels_synth.py` | — | Phase 1 research code: the trace-driven predecessor of the model, the Mandal-style and Moadeli baselines, the link-model comparison |

Python 3 with numpy and pandas. Traces are read in chunks
(`pd.read_csv(..., chunksize=…)`) because the largest are several GB
uncompressed.

### 6.2 Structure

```
bmq.py: the model (no trace needed)
  router, hops, path         the mesh and XY routing                    (2.5.1)
  ni_schedule                copy schedule, cycle by cycle               (11)
  copy_offsets, busy         s_j and B(K, L)                             (12)-(13)
  T_NI, T_ROUTER, T_LINK     router parameters; CREDIT_RTT = VC_RELEASE = R,
  head_latency               T_hop(H), all derived                     (2.6.1)
  head_wait                  the NI queue → W_head                       (3)-(10)
  link_waits                 the links → W per channel                   (14)-(17)
  Params                     the traffic parameters                      (2.7)
  destinations, expand       parameters → expected rate of every message kind
  Model(params)              W_head per queue, W per link, then
    .copies / .multicast     each copy's latency                         (19)
    .latency                 (mean copy, completion = max)               (20)
    .summary()               rate-weighted averages and utilizations
    .saturated()             any ρ ≥ 1
  --demo, --sweep            one idle broadcast; latency vs. λ

fit_params.py: validation (section 5)
  roi, chunks                ROI window, trace in chunks
  send_delays, groups        fixed controller delay per message group
  fit_pb                     virtual-queue fit of p_b
  eject_times                delivery cycle per packet
  fit(run)                   → global Params, per-node Params, Garnet's latencies
  RUN_DIR ...  [--csv FILE]  fit, predict and compare, both parameter levels
```

### 6.3 The pieces, and the equations they implement

**`Params`: the input.** A dataclass with λ, α, K, `packets`,
`mc_packets`, `pattern`, p_b, the source and destination node ids, and
an optional `nodes` dict of per-node overrides. The router parameters are
module constants (`T_NI`, `T_ROUTER`, `T_LINK`, `NI_VCS`) plus the
per-vnet buffer depth.

**`expand(params)`: parameters to expected traffic.** For each source, it
splits λ(1 - α) over the unicast packet mix and the pattern's
destinations, and λα over the fan-outs and the multicast packet mix. A
K-way multicast goes to K distinct routers other than the source's: for
a broadcast that's one fixed set; otherwise the rate is spread over 200
random sets (seeded), because the copy order, and so completion, depends
on which routers are chosen. The result is the rate of every message kind
(src, dests, L, vnet).

**`ni_schedule(K, L, b)`: the copy schedule (11), simulated cycle by
cycle.** It replays Garnet's rules exactly: V = 4 output VCs; each cycle,
free VCs take the next copies in order; the link sends one flit per
cycle, round-robin over VCs that have a flit and a credit (b = the vnet's
router buffer depth; a flit's credit returns `CREDIT_RTT` = R = 5 cycles
later); a VC is free again `VC_RELEASE` = R cycles after its copy's tail
flit. It returns, per copy, the cycle its VC is acquired and the cycle its
tail is sent (τ_j), and is cached by (K, L, b). `busy()` gives B(K, L);
`copy_offsets()` gives τ_j relative to the first copy.

**`head_wait(λ, p_b, mix)`: the source queue, eqs. (3)-(10).**
```python
EB  = Σ q·busy(K, L)        EB2 = Σ q·busy(K, L)²
ENN = 2·λ·p_b / (1 - p_b)                     # (3)
ρ   = λ·EB                                    # (5)
EAA = λ·(EB2 - EB) + ENN·EB²                  # (6)
EU  = EAA / (2(1 - ρ))                        # (8)
w_head = EU + p_b·EB / (1 - p_b)              # (9)-(10)
```

**`link_waits(flows)`: the links, (14)-(17).** For each flow it walks
`path(s, d)` and accumulates, per channel: the rate, Σ r·L, Σ r·L², and
the rate from each input port. Then:
```python
h = Σ r_port² / λ²
W = (1 - h)·ρ·T·(1 + Cs2) / (2(1 - ρ))        # (17)
```

**`Model(params)`.** Calls `expand`, groups the message kinds by NI queue
(src, vnet) and calls `head_wait` for each, builds the flows (a K-way
multicast adds K flows) and calls `link_waits` once. After that, a
message's latency is lookups and sums:
`W_head + τ_j + Σ W on the path + head_latency(hops)` per copy, and the
max over copies for completion. `Model.T0(h, L, vnet)` gives a lone
packet's zero-load latency.

**`fit_params.fit(run)`** (validation only) does the steps of section 5
in two passes over the trace and returns both parameter levels plus
Garnet's measured latencies. It imports `Params` and `Model` from
`bmq.py`; `bmq.py` knows nothing about traces.

### 6.4 Running it

```bash
python3 scripts/bmq.py --demo
python3 scripts/bmq.py --sweep
python3 scripts/fit_params.py results/MOESI_hammer/s4-fft results/MESI_Two_Level/sweep-barrier-s400
python3 scripts/check_parametric_synth.py
```

From Python:
```python
from bmq import Model, Params
m = Model(Params(lam=0.02, alpha=0.05, K=15, pb=0.3,
                 packets={(1, 0): 0.6, (5, 1): 0.4}, mc_packets={(1, 0): 1.0}))
m.summary()          # unicast, mean copy, completion, peak utilizations
m.multicast(21)      # every copy of a broadcast from node 21
m.saturated()
```

### 6.5 How the model was built: the path, and what we fixed

The model was built prediction-first: write the equations, code them,
compare with Garnet on one trace at a time, find what's wrong, fix the
mechanism (never a fudge factor), and re-check all traces.

| Problem found | Fix |
|---|---|
| p_b fitted to the raw inter-arrival C² over-estimated NI waits 5-10× (phases look like bursts) | Mandal's virtual-queue fit |
| One FIFO per NI mixed vnets that Garnet keeps apart | One queue per (source, vnet) |
| The "most common delay" timestamp rule erased real queueing (25 cycles at a hot bank) | Minimum / minimum + 1 rule |
| A constant per-copy spacing missed the gaps every 4 copies | VC-limited schedule, simulated (`ni_schedule`) |
| A first 2-flit schedule recursion was wrong | VC release 5 + (L - 1) cycles after the tail (later replaced, below) |
| The NI's batch burstiness passed downstream gave phantom link waits (5 cycles at 2% load) | Single-link cap (full model), and then the simple link model, where one input never queues |
| The two-moment link model over-predicted at high load (+27% to +131% on transpose) | Replaced by the merge-factor model (17) |
| The model's input was a per-message traffic table taken from the trace | Replaced by traffic parameters (λ, α, K, packet mix, pattern, p_b), fitted from traces only for validation |
| One packet mix for unicasts and multicasts made hammer's 1-flit broadcasts partly 5-flit | Separate multicast packet mix |
| T0 = 4 + 2H + 1.25(L - 1) and R = 5 + (L - 1) were fitted to calibration runs, so the model depended on the simulator; T0 was also 15 cycles low for 5-flit writebacks on 1-flit buffers | Router timing derived from the pipeline (section 2.6.1): credit round trip R = 5, T_hop = 4 + 2H; every result improved |

**Checks used throughout** (section 2.8): exact reduction to known
results (Mandal's GGeo/D/1, Karol et al.'s single-flit wait); a Monte
Carlo of the assumed NI queue (≤ 0.1 cycle); the schedule against every
idle-NI measurement and every minimum latency; no constant fitted to
measured latency.

---

## 7. Results

All errors compare a model's prediction with Garnet, measured from the
estimated NI arrival (section 5.2). "Mean abs. error" is the mean of
|predicted - measured| / measured over runs. BMQ runs from parameters
fitted to each trace (section 5; `results/bmq_validation.csv`), at the
two levels of detail: **per node** (the main result, since it describes
where traffic comes from) and **global** (one value per parameter).

### 7.1 Standard benchmarks: multicast completion (20 runs)

SPLASH-4 and PARSEC, both protocols. Peak link utilization is at most
0.10 on these runs.

| Protocol | BMQ, per-node parameters | BMQ, global parameters | Mandal-style | Moadeli |
|---|---|---|---|---|
| MESI_Two_Level (10 runs) | **6.0%** | 6.6% | 24.3% | 27.7% |
| MOESI_hammer (10 runs) | **6.8%** | 7.2% | 26.7% | 41.5% (1 saturated) |
| **All 20** | **6.4%** (median 5.4%) | 6.9% (median 5.8%) | 25.5% | 34.2% (median 28.8%) |

- **BMQ is slightly low on most runs** (mean signed error -4.9%): it
  ignores some router effects and the spread of waits (section 2.6).
- **Worst cases:** hammer `s4-radix` (-15.2%), MESI `p-blackscholes`
  (-12.9%), hammer `s4-fft` (-12.8%), MESI `s4-water-nsquared` (-12.4%).
- **Unicast latency:** 7.2% mean error (signed -5.9%). The uniform
  destination pattern loses where coherence replies really go, and
  unicasts' small waits are where the missing router effects show most.
- **Derived vs. fitted router timing:** with the earlier fitted T0 and R,
  the same runs gave 6.8% (completion) and 11.8% (unicast). The derived
  timing improves both, mostly by modeling credit stalls of long packets
  on 1-flit buffers.
- The Mandal-style and Moadeli columns here are the Phase 1 versions,
  driven by each trace's exact traffic. The current versions
  (`scripts/baselines.py`) run on BMQ's own fitted inputs and give 24.5%
  and 34.3% on the same 20 runs (section 7.9). How they are implemented:
  section 7.7.

### 7.2 Coherence load sweep: multicast completion (41 runs)

Two protocols, two flit widths, three kernels at several loads: the load
was varied on purpose.

| Model | Mean abs. error | Median | Excluding the lock kernel |
|---|---|---|---|
| **BMQ, per-node parameters** | **9.5%** | 6.4% | **5.4%** |
| BMQ, global parameters | 10.3% | 9.1% | 8.5% |
| Mandal-style GGeo | 27.5% | 28.1% | 26.9% |
| Moadeli | 109.7% (14 runs saturated) | 32.7% | 37.7% |

- **Per node is the main result:** it is better on completion, and much
  better on the other predictions: the last copy's NI wait is off by
  10.8% per node vs. 35.0% global, and unicast latency by 17.0% vs.
  26.5%. Global parameters spread the multicasts over every source, while
  the real ones come from a few home nodes.
- **The former calibration run** (MESI `nsweep-prodcons-s400`) is now a
  validation run too (-0.7%); with it, 42 runs at 9.3%.
- **Derived vs. fitted router timing:** with the earlier fitted T0 and R,
  the same 41 runs gave 12.6% (median 7.2%). The narrow-link (4-byte)
  runs improved most, because their 2-flit control packets stall for
  credits on 1-flit buffers.
- **The source NI explains most of the gain** over the published models:
  the copy schedule applies even at zero load.
- **Moadeli fails in both directions.** At light load it is far too low
  (all copies leave at once). Under load its wormhole blocking recursion
  grows quickly, so lock runs come out 2-9× too high and 14 runs
  saturate where Garnet doesn't. Its mean error is dominated by those
  runs; the median (32.7%) is the fairer summary.
- **The largest errors are the known failure modes** (section 7.5): hammer
  4-byte lock spin 0 (+62%) and the MESI lock runs (-9 to -28%).
- **What parameters cost:** the earlier, trace-driven version of the model
  (driven by each trace's exact message table, with the fitted T0) gave
  11.6% here and 6.4% on the standard benchmarks. The parametric model
  with derived router timing now matches or beats it.

### 7.3 High link load: synthetic traffic (25 runs below saturation)

Unicast latency on Garnet_standalone traffic (section 4.6). Here the
parameters are known exactly from the gem5 options (λ, pattern, packet
mix; `scripts/check_parametric_synth.py`): **2.0% mean error** (max
11.5%) on the 25 runs below saturation, and all five runs that saturate
Garnet are predicted saturated (with the earlier fitted T0: 2.25% on 24
runs; the lightest run was then reserved for calibration). The same runs
also compared link models, in the Phase 1 trace-driven code:

| Runs | BMQ link model (17) | Two-moment | No link waits |
|---|---|---|---|
| uniform random, 1-flit (7) | 2.5% | 2.5% | 5.9% |
| uniform random, mixed (7) | **2.4%** | 4.3% | 10.4% |
| transpose, 1-flit (4) | 0.7% | 0.7% | 3.8% |
| transpose, mixed (6) | **2.0%** | 37.2% | 15.6% |
| **All 24** | **2.0%** (max 8.9%) | 11.4% (max 131%) | 9.3% |
| Link-wait error, cycles | **0.27** | 2.06 | — |

- **Saturation is predicted correctly:** all six runs that saturate
  Garnet (latency in the thousands) have a link at ρ ≥ 1 in the model,
  and no unsaturated run does.
- **The gap grows with load:** (17) is about 8% low at uniform-random
  link load 0.5 (link waits 0.7 vs 1.6 cycles). It doesn't model
  router-internal contention (switch allocation, head-of-line blocking).

### 7.4 High injection with multicast and bursty traffic (30 runs)

The coherence traces never load an NI heavily, and the stock synthetic
tester has no multicast or bursts. This experiment drives the source NI,
the copy schedule and the links together up to saturation, with
open-loop traffic.

**Methodology.**
- **Traffic:** our extended synthetic tester (section 4.6.1) on the same
  4×4 mesh, uniform random destinations, 200k cycles per run. Every
  multicast is a 15-way broadcast to the other directories.
- **Model inputs are the gem5 settings, nothing is fitted:**
  λ = rate / (1 - p_b) messages per cycle per node (GGeo mean), α =
  `--mc-prob`, K = 15, p_b = `--burst-prob`, the packet mix of the chosen
  vnets, buffers of 1 flit on vnets 0-1 and 4 on vnet 2
  (`params()` in `scripts/run_synth_mc.py`).
- **Measured:** the same way as for the coherence traces (section 5.5):
  from NI arrival to delivery, via the trace hook.
- **Load range:** chosen with the model's own utilization, from about
  0.05 to past saturation.

**Experiments:**

| Configuration | Packets | Multicast α | Burst p_b | Injection rates (per node per cycle) |
|---|---|---|---|---|
| A | 1 flit (vnet 0) | 0.05 | 0 | 0.02, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5 |
| B | 1 flit (vnet 0) | 0.2 | 0 | 0.02, 0.05, 0.1, 0.15, 0.18, 0.2, 0.22, 0.25 |
| C | 1 flit (vnet 0) | 0.05 | 0.5 | 0.02, 0.05, 0.1, 0.15, 0.2, 0.23, 0.26, 0.3 |
| D | mixed: 1 flit (vnets 0, 1), 5 flits (vnet 2) | 0.05 | 0 | 0.02, 0.05, 0.1, 0.15, 0.18, 0.2, 0.22 |

**Results, the 17 runs where Garnet does not saturate** (mean absolute
error; the model is low on every run, so the signed mean is the same
with a minus sign):

| Configuration | Runs | Completion | Max | Mean copy | Last copy's VC | Unicast |
|---|---|---|---|---|---|---|
| A: 1-flit, α 0.05 | 5 | 14.0% | 44.2% | 15.2% | 15.8% | 18.0% |
| C: 1-flit, α 0.05, bursty | 4 | 8.9% | 22.2% | 9.8% | 10.4% | 11.7% |
| B: 1-flit, α 0.2 | 4 | 10.6% | 31.1% | 11.5% | 12.0% | 13.2% |
| D: mixed, α 0.05 | 4 | 16.9% | 36.4% | 14.4% | 17.1% | 15.2% |
| **All 17** | 17 | **12.7%** | 44.2% | 12.9% | 13.9% | 14.7% |

By load (the model's busiest-link utilization):

| Link utilization | Runs | Completion | Max | Mean copy | Last copy's VC | Unicast |
|---|---|---|---|---|---|---|
| below 0.25 | 8 | **3.1%** | 8.8% | 2.6% | 3.2% | 1.6% |
| 0.25-0.45 | 4 | **10.0%** | 18.5% | 9.3% | 10.7% | 10.6% |
| 0.45-0.65 | 5 | **30.2%** | 44.2% | 32.3% | 33.8% | 39.1% |

**Saturation, the other 14 runs:**

| Configuration | Runs | Garnet saturated | Model predicts saturated | Garnet saturates from rate | Model's link utilization there |
|---|---|---|---|---|---|
| A | 8 | 3 | 0 | 0.40 | 0.70 |
| B | 8 | 4 | 1 | 0.18 | 0.72 |
| C | 8 | 4 | 1 | 0.20 | 0.70 |
| D | 7 | 3 | 0 | 0.18 | 0.74 |

No run is falsely predicted to saturate.

**What the results say:**
- **Low and moderate load are right:** 3% below link load 0.25, 10% at
  0.25-0.45, for all four kinds of traffic.
- **Burstiness at high load is handled** as well as random traffic at the
  same load (8.9% vs. 14.0%). This was untested before.
- **Near saturation the model is too low,** by 30% at link load
  0.45-0.65, and it misses the saturation point: Garnet saturates at
  about 70% of the utilization at which the model does. With unicast-only
  traffic (section 7.3), Garnet saturated close to where the model did
  (link load up to 0.97).
- **The gap is at the source NI.** The time until the last copy gets a VC
  (measured from `end_cyc`, so before the network) is under-predicted by
  as much as completion. So the NI queue fills up faster in Garnet than
  in the model.

**Why: three resources the model treats as unlimited.**
1. **NI virtual channels between messages.** Every packet holds an NI VC
   from when it gets one until its tail's credit returns, R = 5 cycles
   later. With V = 4 VCs per vnet, an NI can start at most V / R = 0.8
   packets per cycle, not 1. The copy schedule applies the VC limit within
   one multicast, but the queue model assumes each new message finds all
   4 VCs free. At high load they are still held (the VC carry-over of
   section 7.5, now as a throughput limit).
2. **The injection link shared by a node's vnets.** In configuration D a
   node sends on three vnets, which share one injection link (one flit per
   cycle in total). The model gives each vnet its own queue and link.
3. **Finite router buffers.** The queueing formulas assume unlimited
   waiting room (assumption A7). Garnet's buffers hold 1 or 4 flits per
   VC; when they fill, wormhole backpressure stalls packets in place and
   blocks the packets behind them. This grows quickly near saturation.

**Possible solutions.**

*Fix 1: charge each message for the VC time it holds.* Use an effective
busy time in the NI queue (eqs. (4)-(10)) instead of B:

```
B_eff = max( B ,  H / V )
H     = Σ over the message's copies of (τ_j + R − a_j)
        (VC-cycles held: from getting a VC, a_j, until the tail's credit
         returns, τ_j + R)
```

| Message | B (link) | H / V (VCs) | B_eff |
|---|---|---|---|
| 1-flit unicast | 1 | 5/4 = 1.25 | 1.25: NI capacity 0.8 |
| 1-flit broadcast, K = 15 | 18 | 15 × 5/4 = 18.75 | 18.75 |
| 5-flit data unicast, 4-flit buffers | 6 | 10/4 = 2.5 | 6 (unchanged) |

It matters only for short packets and only at high load. First tested
alone on these runs: mean completion error on the 17 unsaturated runs
fell from 12.7% to 8.3%, and the model predicted 7 of the 14 saturations
instead of 2, with no false alarms. It did not help configuration D,
where fix 2 is needed.

*Fix 2: model the injection link as a shared channel.* Treat each node's
injection link like a mesh link in (14)-(17): its load is the sum over
the node's vnets of rate × flits, its "input ports" are the node's vnet
queues, and its wait (1 - h)·ρL̄(1 + C²_S)/(2(1 - ρ)) is added to every
packet from that node. A node is saturated when its total injection load
reaches 1.

*Fix 3 (larger): finite buffers.* Model backpressure, for example by
inflating a link's service time by the probability that its downstream
buffer is full (a standard finite-buffer decomposition).

**Implemented: the high-load correction (section 2.6.2).** Fixes 1 and 2
are now in `bmq.py` (`bmq.VC_HOLD`, on by default), together with a
simple form of fix 3: an M/D/V wait for a free downstream VC on every
channel (eq. 17a). On these runs (section 7.9, `synth_mc`, 4×4):

| | Completion error, 17 unsaturated runs | Garnet saturations predicted (of 14) | False saturations |
|---|---|---|---|
| Correction off | 12.5% | 2 | 0 |
| **Correction on** | **6.3%** (median 2.1%) | **9** | 0 |

The coherence results (sections 7.1-7.2) barely move (6.4% either way on
the standard benchmarks), since the correction only acts when NIs are
busy. Data: `results/garnet_synth_mc.csv`; runs in
`results/garnet_synth_mc/`.

### 7.5 Where BMQ breaks

1. **VC carry-over** (MESI lock, -18 to -28%): back-to-back multicasts
   find VCs still busy from the previous one. The schedule assumes each
   multicast starts with all 4 VCs free.
2. **Closed-loop arrivals** (hammer, 4-byte flits, lock, spin 0: +62%):
   lock handoffs are self-paced, so arrivals depend on the network
   (against A8).
3. **Shared injection link** (hammer, 4-byte prod-cons, -6 to -11%): other
   vnets' long packets share the NI's one injection link, which the
   per-vnet queues don't see. (With the earlier fitted T0 these runs were
   about -22%; most of that was the missing credit stalls.)
4. **High NI load** (synthetic multicast traffic, section 7.4): the NI's
   VCs are held R cycles per packet, which caps an NI at 0.8 packets per
   cycle, and router buffers are finite. Without the correction the model
   was 30% low at link load 0.45-0.65 and caught 2 of 14 saturations;
   with it (section 2.6.2), 6.3% and 9 of 14 on 4×4. On 8×8 the gap is
   still 11%: gem5's copies hold VCs about 1.4 cycles past R, the model
   predicts 0-0.2, probably because 63-copy broadcast trains are not
   average-rate traffic.
5. **Heavy broadcast traffic on larger meshes** (MOESI_hammer, 6×6 and
   8×8, section 7.9: -20 to -77% on radix, water-nsquared, fft,
   ocean-cont). Every hammer miss broadcasts to all 35 or 63 other
   caches, and Garnet's completion grows to 100-400 cycles while the
   model stays near 60-100. The correction doesn't fire on these runs
   (same error with it off), so the model sees the average load as
   light. Diagnosed in section 7.10: broadcast trains at home nodes, which
   p_b fitted at a one-cycle timescale cannot see, and short overload
   phases, which a whole-window average hides.

### 7.6 Worked example

Directory node 21 broadcasts a 1-flit `GETS` to the 15 other caches from
an idle NI (section 2.9): the last copy arrives at **29 cycles**. Garnet
measures 29.2 (hammer swaptions), BMQ predicts 27.6 on the whole trace,
Moadeli 14.1 (all copies leave together) and Mandal-style 21.3 (every
copy waits the average).

---

### 7.7 How the baselines are implemented

Both baselines run on **BMQ's own inputs**: the same traffic parameters
fitted to each trace (section 5, expanded the same way), the same mesh
and XY paths (any size), and the same derived zero-load timing (a lone
packet's tail leaves the NI, then the head latency of section 2.6.1).
Only the queueing model differs. They are in `scripts/baselines.py`
(`mandal()`, `moadeli()`), and `scripts/compare_models.py --mesh N`
runs all four models (BMQ with and without the correction, Mandal-style,
Moadeli) on every run of a mesh (section 7.9).

The Phase 1 versions (`scripts/bmq_model.py`, `scripts/moadeli_baseline.py`:
4×4 only, each trace's exact message table, the earlier fitted T0 = 4 +
2H + 1.25(L - 1)) are kept for reference; sections 7.1-7.2 quote them.

**Mandal-style GGeo** (Mandal et al. 2020/2021, applied to multicast):

| Piece | Implementation |
|---|---|
| Arrivals | One GGeo stream per (source, vnet) NI queue; **every copy is its own packet** arrival (Mandal has no multicast) |
| p_b | Mandal's virtual-queue fit, on the copy arrival times (so a multicast's K same-cycle copies look like a burst; `Params.pb_pkt`) |
| Burstiness | C²_A = 2/(1 - p_b) - λ - 1 [M20] |
| NI wait | GGeo/G/1: W = (T/2)[(C²_A - 1) + ρ(C²_A + C²_S)/(1 - ρ)] [Kouvatsos 1994; M21 eqs. 4-5], T and C²_S from packet lengths. The **same wait for every copy**: no copy order |
| Network | Mandal's GGeo/G/1 decomposition: departure SCV [M21 eq. 18] and split/merge [M21 eq. 19], a link delivering at most one flit per cycle (Phase 1 used BMQ's link model (17) here) |
| Completion | NI wait + max over copies of (link waits + T0) |
| Left out | Priority / weighted round-robin arbitration (Garnet is round-robin) |

**Moadeli & Vanderbauwhede** (IPDPS 2009, as published, adapted to the
mesh):

| Piece | Implementation |
|---|---|
| Channels | Injection channel, XY mesh links, ejection channel; each an M/G/1 queue with Poisson arrivals |
| Multicast | Each copy is an independent stream (the paper's assumption); all copies of a node share its injection channel |
| Service time | Backward recursion from the ejection channel (x = message length): x_i = Σ_j P_ij[(1 - λ_ij/λ_j) W_j + x_j + hop], hop = 2 cycles (Garnet) |
| Wait | Pollaczek-Khinchine, W = λx²(1 + σ²/x²)/(2(1 - λx)), with σ = x - message length (Draper & Ghosh); iterated to a fixed point, saturated if any λx ≥ 1 |
| Completion | E[max] over the copies of independent exponential waits (numerical integral of 1 - Π(1 - e^(-t/w_k))), plus T0 of the farthest copy |
| Adapted | Mesh with XY routing instead of Quarc; measured traffic instead of uniform; two packet lengths averaged per channel; Garnet's T0 and hop time |

**A bug we fixed (2026-11-06).** The Moadeli wait was first coded as
λ²x(1 + σ²/x²)/(2(1 - λx)), not λx²(…): dimensionally wrong, and too
small by a factor x/λ. Earlier documents reported Moadeli at 40.6%
(standard benchmarks) and 51.8% (load sweep) with that bug. Corrected,
Moadeli is less wrong under load at the standard benchmarks (34.2%) but
much more erratic on the load sweep (median 32.7%, 14 runs saturated).
The Mandal-style code was checked against its formulas and is unchanged;
its sweep number moved from 25.9% to 27.5% only because it now uses the
current link model.

**Why a reviewer might still push back, and the answers:**
- *"Mandal-style isn't Mandal's model."* Correct: Mandal et al. model
  unicast only. We apply their arrival model and NI queue in the most
  direct way to multicast (copies as packets). Their arbitration models
  don't apply to Garnet's round-robin routers.
- *"Moadeli was built for Quarc, not a mesh."* Correct: its parallel
  injection assumption is exactly what a source-replicating mesh breaks,
  which is the point of the comparison. Everything else follows the paper.
- *"The baselines were given worse inputs."* No: all four models get
  the same fitted parameters, mesh and zero-load timing. (In Phase 1 the
  baselines got each trace's exact traffic, which favoured them; the
  numbers barely changed.)

### 7.8 Design-space exploration (model only)

What the model is for: answering design questions in seconds that would
take hours of simulation per point. Nine experiments, all run with the
model alone (`scripts/dse.py`, 1.5 s for all of them; numbers in
`results/dse/*.csv`, figures `docs/figures/fig8a`-`fig8d_dse_*.png`
from `scripts/fig_dse.py`).

**Method.**
- **Idle network:** one multicast from one sender, everything else
  empty. Isolates the copy schedule and the trip. Multicasts with K < 15
  are averaged over 200 random destination sets.
- **Loaded network:** the synthetic setup of section 7.4 (16 senders,
  uniform destinations, 15-way multicasts, buffers 1/4 flits). Loaded
  points are kept mostly below link load 0.45, where section 7.4 found
  the model within about 10%; points beyond are flagged.
- **Router parameters** are changed through the model's inputs (V,
  t_router, t_link); R and T_hop follow from them (section 2.6.1).
- Router area, power and cost are not modeled: these are latency
  results only.

**A1. Completion vs fan-out K** (idle; figure 8a, left).

| K | 2 | 4 | 5 | 8 | 9 | 12 | 13 | 15 |
|---|---|---|---|---|---|---|---|---|
| 1-flit, center sender | 10.2 | 13.1 | 15.0 | 18.9 | 21.0 | 24.6 | 26.7 | 29.0 |
| 1-flit, corner sender | 13.0 | 16.6 | 18.6 | 22.8 | 24.9 | 28.5 | 30.7 | 33.0 |
| 5-flit, center sender | 18.2 | 29.1 | 36.0 | 50.9 | 58.0 | 72.6 | 79.7 | 89.0 |

Completion grows with K and jumps after every 4th copy (K = 5, 9, 13),
when the copies run out of VCs. Long packets are 3× slower, because
sending the flits dominates.

**A2. Number of NI VCs V** (15-way broadcast from the center; figure 8a,
right).

| V | 1 | 2 | 4 (Garnet) | 8 | 16 |
|---|---|---|---|---|---|
| 1-flit, idle | 82 | 47 | 29 | 26 | 26 |
| 5-flit, idle | 157 | 108 | 89 | 86 | 86 |
| 1-flit, loaded (rate 0.1, α = 0.2) | saturated | 108.6* | 33.6 | 28.7 | 28.7 |

4 VCs is close to the knee: 8 saves only 3 cycles (the link's one flit
per cycle then becomes the limit), while 2 VCs cost 60%. (*NI load 0.8,
beyond the validated range.)

**A3. Router pipeline depth** (1-4 stages; t_link = 1; figure 8d, left).

| Stages | 1 (Garnet) | 2 | 3 | 4 |
|---|---|---|---|---|
| R, cycles per hop | 5, 2 | 6, 3 | 7, 4 | 8, 5 |
| 1-flit broadcast, idle | 29 | 37 | 45 | 53 |
| 5-flit broadcast, idle | 89 | 97 | 105 | 113 |
| 1-flit broadcast, loaded | 33.6 | 44.2 | 56.0 | 69.5 |

Each stage adds a cycle per hop and a cycle to the VC return time R, so
it hurts twice: the trip gets longer and the copies leave in slower
waves. Under load the NI also fills up faster (NI load 0.44 → 0.62).

**A4. Copy order policy** (all 16 senders averaged; figure 8b, left).
Garnet sends copies in ascending destination id. Alternatives: farthest
destination first, nearest first.

| Case | Ascending (Garnet) | Farthest first | Nearest first | Gain |
|---|---|---|---|---|
| Idle, 1-flit, K = 15 | 27.6 | 23.0 | 31.0 | **-17%** |
| Idle, 5-flit, K = 15 | 87.6 | 83.0 | 91.0 | -5% |
| Idle, 1-flit, K = 8 | 18.7 | 14.9 | 20.9 | **-20%** |
| Loaded, 1-flit, K = 15 | 33.6 | 28.8 | 37.2 | **-14%** |
| Loaded, 5-flit, K = 15 | 88.2 | 83.3 | 91.7 | -5% |
| Loaded, 1-flit, K = 8 | 24.7 | 20.8 | 27.1 | **-16%** |

Why: completion is the max over copies of (leave time + trip). Sending
the longest trips first pairs the latest leave times with the shortest
trips. It's the classic "longest job first" rule for minimizing the
latest finish. It helps short packets most; for 5-flit data, sending the
flits dominates and the trips matter less.

**A5. Placement of the sending node** (idle 1-flit broadcast, by source
router; figure 8b, right). With ascending order, completion ranges from
23 to 33 cycles: the corner router 0 is worst, because its far
destinations have the highest ids and leave last. **With farthest-first,
every router completes in 23 cycles**: the copy order absorbs the
difference in distances, so placement stops mattering.

**A6. Load × burstiness** (1-flit, 5% broadcasts; figure 8c, left).

| Rate (per node) | 0.02 | 0.05 | 0.1 | 0.15 | 0.2 | 0.25 |
|---|---|---|---|---|---|---|
| p_b = 0 | 27.8 | 28.1 | 28.7 | 29.5 | 30.5 | 31.8 |
| p_b = 0.3 | 28.7 | 29.3 | 30.5 | 32.2 | 34.7* | 39.3* |
| p_b = 0.6 | 31.2 | 32.7 | 37.0 | 47.5* | 120* | saturated |

Bursts cost little at light load but move the knee: at p_b = 0.6 the
model saturates near rate 0.25 (link and NI load reach 1), against
beyond 0.3 without bursts. (*link load above 0.45: the model is
optimistic there, section 7.4.)

**A7. Multicast width at equal message rate** (20% of messages are
multicasts, rate 0.1 per node; figure 8c, right).

| K | 2 | 4 | 8 | 15 |
|---|---|---|---|---|
| Packets per message | 1.2 | 1.6 | 2.4 | 3.8 |
| Multicast completion | 11.5 | 14.1 | 20.0 | 33.6 |
| Unicast latency | 9.1 | 9.3 | 10.2 | 14.9 |
| Busiest link load | 0.13 | 0.17 | 0.25 | 0.40 |

Wider multicasts cost twice: each multicast takes longer (more waves of
copies), and the extra copies load the network, slowing every unicast
too. This is the trade-off between directory protocols (K = number of
sharers) and broadcast protocols (always K = 15), seen from the network
side only: it leaves out the directory lookup and the coherence
behavior.

**A8. Link width** (control packets 8 B, data 72 B; figure 8d, right).

| Flit width | 32 B | 16 B (Garnet) | 8 B | 4 B |
|---|---|---|---|---|
| Control / data packet, flits | 1 / 3 | 1 / 5 | 1 / 9 | 2 / 18 |
| Data broadcast, idle | 59 | 89 | 149 | 284 |
| Control broadcast, idle | 29 | 29 | 29 | 49 |
| Data unicast, 3 hops, idle | 12 | 15 | 20 | 31 |
| Mixed traffic, loaded (rate 0.05) | 38.4 | 49.6 | 74.8 | 174.5* |

Narrow links hurt data broadcasts most: each copy takes longer to send,
and on 4-flit buffers the copies also stall for credits. Control
broadcasts don't change until control packets need 2 flits (4 B).
(*link load 0.64, beyond the validated range.)

**A9. Speed.** One model evaluation (16 senders, a mix of 2- to 15-way
multicasts) takes 0.18 s. A gem5 coherence run takes about 17 min
(median), a synthetic run about 25 s. The model is about 5,000 times
faster than a coherence simulation, and a whole design sweep takes
seconds.

**What to take from it.**
- **The NI is where multicast latency is decided:** the number of VCs,
  the VC return time (router pipeline) and the copy order each change
  broadcast completion by 15-60%, more than placement or moderate load.
- **Farthest-first copy ordering** is a one-line NI policy change that
  the model predicts cuts multicast completion by 14-20% and makes it
  independent of where the sender sits. It is the strongest design
  result, and it is **not yet validated in gem5** (next step: patch
  Garnet's copy order and re-measure).
- **4 VCs per vnet is near the knee;** more VCs help little because the
  injection link then limits.

### 7.9 Larger meshes: all four models on 4×4, 6×6 and 8×8

The same comparison on three mesh sizes, with all four models on the same
inputs (section 7.7): BMQ with the high-load correction (the model as it
now stands), BMQ without it, Mandal-style and Moadeli.

**Setup.**
- **Coherence runs:** the 10 standard benchmarks (section 4.4) under both
  protocols, 20 runs per mesh. 6×6 has 36 cores and 32 L2/directories on
  routers 0-31 (`Mesh_XY_Partial`: Ruby needs a power-of-two count);
  8×8 has 64 of each. SPLASH-4 uses 32 threads on 6×6, swaptions
  `-ns 64`. The 6×6 and 8×8 runs measure a **20M-cycle ROI window**
  (4×4: 50M), to fit the run time. The coherence load sweeps were run on
  4×4 only.
- **One 8×8 run is shorter:** MOESI_hammer `s4-ocean-cont` hit the 6 h
  limit 75% of the way through its window. It is measured over the 14.9M
  cycles it completed (window cut at cycle 21,000,000, 92k cycles before
  the trace ends so every message in it was delivered; the run's
  `PARTIAL.txt` gives the details). A full rerun would take about 8 h.
- **Synthetic runs:** the high-injection multicast experiment (section
  7.4) and unicast traffic (section 7.3), with multicasts as broadcasts
  (K = 15, 31-35 or 63) and rates scaled so the busiest queue has the
  same utilization on every mesh.
- **Metric:** mean |error| of multicast completion (unicast latency for
  the unicast runs) over the runs Garnet does not saturate. A run a model
  calls saturated is counted under "false saturations", not in the mean.
- **Run it:** `python3 scripts/compare_models.py --mesh N` →
  `results/[meshN/]model_compare.csv`; `python3 scripts/summarize_compare.py 4 6 8`
  → `results/model_compare_summary.csv`.

**Standard benchmarks, multicast completion** (mean abs. error; median in
brackets):

| Mesh | Runs | BMQ | BMQ, no correction | Mandal-style | Moadeli | Moadeli false saturations |
|---|---|---|---|---|---|---|
| 4×4 | 20 | **6.4%** (5.5) | 6.4% | 24.5% | 34.3% | 1 |
| 6×6 | 20 | **13.4%** (7.9) | 13.3% | 34.5% | 48.2% | 4 |
| 8×8 | 20 | **17.6%** (10.3) | 17.6% | 37.3% | 37.1% | 10 |

By protocol:

| Mesh | Protocol | BMQ | Mandal-style | Moadeli |
|---|---|---|---|---|
| 4×4 | MESI_Two_Level | **5.9%** | 22.7% | 27.5% |
| 4×4 | MOESI_hammer | **6.8%** | 26.3% | 41.9% |
| 6×6 | MESI_Two_Level | **2.9%** | 25.0% | 45.2% |
| 6×6 | MOESI_hammer | **23.9%** | 44.1% | 53.4% |
| 8×8 | MESI_Two_Level | **7.0%** | 19.3% | 33.0% |
| 8×8 | MOESI_hammer | **28.3%** | 55.2% | 53.2% |

**Synthetic traffic:**

| Mesh | Workload | Runs (Garnet saturated) | BMQ | BMQ, no correction | Mandal-style | Moadeli | Saturations predicted, BMQ / no corr. / Mandal / Moadeli |
|---|---|---|---|---|---|---|---|
| 4×4 | multicast, high injection | 17 (+14) | **6.3%** | 12.5% | 60.4% | 266% (14 false) | 9 / 2 / 2 / 14 |
| 6×6 | multicast, high injection | 17 (+14) | **11.4%** | 13.0% | 70.9% | 57% (15 false) | 7 / 2 / 2 / 14 |
| 8×8 | multicast, high injection | 12 (+19) | **11.2%** | 11.9% | 78.0% | all 12 false | 7 / 2 / 2 / 19 |
| 4×4 | unicast | 25 (+5) | **1.6%** (1 false) | 2.5% | 12.7% | 108% (20 false) | 5 / 5 / 5 / 5 |
| 6×6 | unicast | 15 | **1.1%** | 1.1% | 3.4% | 30% (13 false) | — |
| 8×8 | unicast | 24 (+5) | **1.0%** | 1.2% | 11.5% | 65% (22 false) | 5 / 5 / 5 / 5 |

**What the results say:**
- **BMQ is the most accurate model on every mesh and workload,** by 2-4×
  over Mandal-style and more over Moadeli, and it never predicts a false
  saturation on coherence traffic.
- **MESI is accurate at every size** (3-7%). Its multicasts are
  invalidations to the current sharers, a few copies each.
- **The growth with mesh size is all MOESI_hammer** (6.8%, 23.9%,
  28.3%). Every hammer miss broadcasts to all other caches (35 or 63
  copies). On most hammer runs BMQ is still within 10-25%, but a few
  benchmarks are far off, all low: on 6×6 radix -64% (Garnet 159 cycles,
  BMQ 58), fft -36%, water-nsquared -28%, ocean-cont -27%; on 8×8
  water-nsquared -77% (414 vs. 97), radix -63%, ocean-cont -33%,
  fft -27%. This is failure
  mode 5 (section 7.5) and the main open problem.
- **The correction changes nothing on coherence traffic** (same error
  with it off) and helps where it was designed to: high NI load. On the
  synthetic multicast runs it catches 7-9 saturations where the model
  without it catches 2, with no false alarms, on every mesh.
- **Mandal-style is always low** (it has no copy order: every copy waits
  the average), and the gap grows with K.
- **Moadeli fails in both directions and gets worse with size:** it is
  too low on light traffic (all copies leave together) and its blocking
  recursion over-charges each channel under load, so it calls more and
  more unsaturated runs saturated (1, 4, 10 standard runs; every 8×8
  high-injection run).
- **Unicast latency** on the standard benchmarks follows the same pattern
  as completion: BMQ 7.1%, 12.4% and 33.3% on 4×4, 6×6 and 8×8, all low,
  again driven by hammer.

### 7.10 Diagnosis: why MOESI_hammer is low on larger meshes

**Short answer.** BMQ's mechanics (NI service, copy schedule, links) are
right; its *inputs* are averages over the whole 20M-cycle window, and
hammer traffic on large meshes is uneven in time in two ways the averages
hide. The cost of unevenness grows with mesh size because a broadcast's
NI service time grows with its number of copies: about 18 cycles on 4×4
(K = 15), 45 on 6×6 (K = 35), 80 on 8×8 (K = 63).

**Method** (`scripts/diag_hammer/`, data in `results/diag_hammer/`):
1. Split Garnet's latency per message (`decomp.py`): NI queueing
   (`start_cyc − ready`), getting VCs for all copies (`end_cyc −
   start_cyc`) and network (last delivery − `end_cyc`).
2. Cut each run into 1M-cycle windows and measure broadcast completion
   per window (`windows.py`), then compare each window with BMQ's
   prediction (`classify.py`).
3. Replay real arrivals through the model's own service times
   (`pbfix.py`), to separate arrival effects from service effects.

**Mechanism 1: broadcast trains at a home node.** On 6×6 radix, late in
the run, one directory receives about 16 GETX in 180 cycles and
broadcasts each to 35 caches. Each needs about 45 cycles of NI time, so
the train builds a queue of several hundred cycles. The NI queueing grows
from 1 to about 195 cycles across the run while the VC time stays at 43
and the network part at 16. The per-node rate is the same throughout
(2.1×10⁻⁴ per cycle); only the spacing changes: the median gap between a
node's broadcasts falls from 4,071 cycles to 12.
- **Why the model misses it:** p_b is fitted with a unit-service virtual
  queue (section 5.3), so it counts only same-cycle arrivals as bursts.
  Gaps of 3-20 cycles are invisible at that timescale: p_b = 0, and the
  model's NI wait for these queues is 0.27 cycles.
- **Proof:** the real arrivals, replayed through BMQ's own service time
  (45.25 cycles per broadcast), give a mean wait of 54 cycles (Garnet:
  65).

**Mechanism 2: overload phases.** On 8×8 water-nsquared, the first 1M
cycles carry 93k broadcasts (33% of the run's) and about 6M ACKs back to
the requesters. The network is overloaded: ACKs wait up to 32k cycles at
their NI, and broadcast completion is about 1,030 cycles. In the other 19
windows, Garnet measures 96-115 cycles, and **BMQ predicts 96.5**. The
mean over the run (414) is dominated by that one phase.

**All 20 hammer runs on 6×6 and 8×8** (completion in cycles; "best
window" is the window with the lowest completion; "excess from hot
windows" is the share of the above-median latency that comes from windows
more than 1.5× the median):

| Mesh | Benchmark | Garnet | BMQ | Error | Best window | BMQ vs. best | Excess from hot windows | Mean NI queueing | Class |
|---|---|---|---|---|---|---|---|---|---|
| 6×6 | blackscholes | 60.1 | 57.8 | -4% | 58.3 | -1% | 0% | 0.4 | fine |
| 6×6 | barnes | 56.2 | 61.2 | +9% | 55.6 | +10% | 0% | 1.6 | fine |
| 6×6 | swaptions | 66.7 | 57.6 | -14% | 54.0 | +7% | 0% | 2.9 | mild variation |
| 6×6 | fluidanimate | 70.9 | 58.4 | -18% | 58.6 | 0% | 0% | 3.9 | mild variation |
| 6×6 | lu-cont | 69.7 | 57.3 | -18% | 62.6 | -9% | 0% | 7.3 | steady trains |
| 6×6 | streamcluster | 75.5 | 58.3 | -23% | 57.7 | +1% | 69% | 11.3 | phases |
| 6×6 | ocean-cont | 81.6 | 59.5 | -27% | 66.4 | -10% | 0% | 8.7 | steady trains |
| 6×6 | water-nsquared | 79.8 | 57.7 | -28% | 57.5 | 0% | 92% | 15.4 | phases |
| 6×6 | fft | 91.1 | 58.7 | -36% | 85.8 | -32% | 0% | 13.9 | steady trains |
| 6×6 | radix | 159.4 | 57.8 | -64% | 60.1 | -4% | 81% | 95.2 | trains, growing |
| 8×8 | blackscholes | 101.1 | 95.7 | -5% | 95.1 | +1% | 0% | 1.3 | fine |
| 8×8 | barnes | 96.2 | 101.7 | +6% | 94.7 | +7% | 0% | 1.8 | fine |
| 8×8 | fluidanimate | 117.7 | 97.1 | -18% | 97.5 | 0% | 0% | 5.8 | mild variation |
| 8×8 | streamcluster | 120.6 | 99.5 | -18% | 96.3 | +3% | 64% | 7.9 | phases |
| 8×8 | lu-cont | 115.6 | 95.0 | -18% | 94.9 | 0% | 0% | 11.2 | mild variation |
| 8×8 | swaptions | 118.4 | 95.7 | -19% | 90.0 | +6% | 0% | 5.9 | mild variation |
| 8×8 | fft | 134.8 | 98.0 | -27% | 119.9 | -18% | 0% | 13.3 | steady trains |
| 8×8 | ocean-cont | 149.7 | 100.4 | -33% | 131.5 | -24% | 51% | 19.1 | steady trains + phases |
| 8×8 | radix | 267.0 | 99.5 | -63% | 111.5 | -11% | 89% | 119.7 | trains, growing |
| 8×8 | water-nsquared | 414.2 | 96.5 | -77% | 96.3 | 0% | 97% | 280.4 | phases (overload) |

**What the table says:**
- **In its quietest window, BMQ is within 11% on 16 of 20 runs.** The
  model is right for the traffic it is given.
- **Phases** (streamcluster, water-nsquared, radix, 8×8 ocean-cont): most
  of the excess comes from a few hot windows.
- **Steady trains** (fft, ocean-cont, 6×6 lu-cont): completion is high in
  every window, with 9-19 cycles of NI queueing that the model doesn't
  predict. Even the best window is 10-32% above BMQ.
- **Mild variation** (fluidanimate, swaptions, 8×8 lu-cont): the best
  window matches BMQ, but the windows vary moderately and the mean is
  17-19% above it.

**Fix A: fit burstiness at the service timescale** (`pbfix.py`, tested on
two runs, not adopted). For each NI queue, replay its real arrivals
through BMQ's own service times, take the mean wait, and choose the p_b
at which BMQ's NI-wait formula (5)-(10) gives that wait. It uses only
arrival times and the model's service times, not Garnet's measured
waits.

| Run | Garnet | BMQ | BMQ with fix A |
|---|---|---|---|
| 6×6 radix | 159.4 | 57.8 (-64%) | 111.8 (-30%) |
| 8×8 water-nsquared | 414.2 | 96.5 (-77%) | 108.2 (-74%) |

It halves the error from trains (the fitted p_b of the home queues goes
from 0 to 0.64) and, as expected, does little for an overload phase. The
remaining radix gap is probably the VC time during trains, which the
replay doesn't include.

*Full evaluation (2026-11-25; not adopted, final model is BMQ without it).*
Fix A was run on all standard runs (`scripts/fit_pbsvc.py`, model
`bmq_A` in `model_compare.csv`). Mean |error| of completion, BMQ -> with
fix A: standard 4x4 6.4 -> 4.8%, 6x6 13.4 -> 9.1%, 8x8 17.6 -> 16.3%
(MOESI_hammer 6.8 -> 3.8, 23.9 -> 15.4, 28.3 -> 25.8%; MESI unchanged).
But the 4 B sweep on 4x4 worsens from 11.4 to 23.9% (over-prediction),
and the worst 8x8 hammer run stays at -74%. We therefore report plain
BMQ; the results in section 7.9 are the final numbers.

**Fix B: phase-aware prediction** (proposed, not built). Fit the
parameters per window (for example 1M cycles), predict each window, and
weight the predictions by each window's broadcasts. A window the model
calls saturated is reported as such. Since BMQ matches the quiet windows,
this should remove most of the phase error; a phase that truly overloads
the network (water-nsquared's first window) would be flagged as
saturated, not predicted.

## 8. Limitations and next steps

**Limitations:**
- The failure modes of section 7.5: VC carry-over, closed-loop traffic,
  the shared injection link, high NI load (reduced by the correction),
  and heavy broadcast traffic on larger meshes.
- **Accuracy drops with mesh size for broadcast protocols** (section
  7.9): 6.4%, 13.4% and 17.6% on 4×4, 6×6 and 8×8, all from
  MOESI_hammer (MESI stays at 3-7%).
- Completion is a maximum of mean latencies. The expected maximum of
  random latencies is larger, so BMQ is slightly low when waits vary a
  lot.
- The link model ignores router-internal contention and wormhole
  backpressure; it is about 8% low at link load 0.5.
- At high NI load (synthetic multicast traffic, section 7.4) the
  correction of section 2.6.2 brings the error from 12.5% to 6.3% on 4×4,
  but it still predicts about half the saturations, and 11% remains on
  6×6 and 8×8. Router buffers are modelled only through the downstream-VC
  wait. Bursty traffic itself is handled as well as random traffic.
- Fitted to coherence traffic, the parameters describe destinations as
  uniform. Real coherence traffic is structured (requests to home nodes,
  replies to the requester, hotspot banks), which the model sees only
  through per-node rates. This costs about 1 point of completion error
  and is the main source of the unicast error.

**Next steps:**
0. Validate the design predictions of section 7.8 in gem5: patch
   Garnet's NI to send copies farthest-first (A4), and change
   `--vcs-per-vnet` (A2), then compare with the model without refitting.
1. Reduce the MOESI_hammer error on 6×6 and 8×8 (failure mode 5,
   diagnosed in section 7.10): predict per time window (fix B). Fix A
   was evaluated and not adopted (it worsens the 4 B sweep).
2. (Done: the VC-time charge and the shared injection link are in the
   model as the high-load correction, section 2.6.2.)
3. Carry VC state from one multicast to the next in the schedule, so a
   multicast's copies start later when VCs are busy (failure mode 1).
4. Fuller finite-buffer modelling near saturation (section 7.4, fix 3;
   the correction has only the downstream-VC wait).
5. The expected maximum over copies for the random part of the waits
   (Moadeli's E[max] idea, on top of our deterministic schedule).
6. Router-internal contention at high load, e.g. Mandal-style effective
   service per input port.
7. Role-based coherence parameters (miss rate, home mapping, sharing
   degree), generating requests, replies and invalidations instead of
   uniform destinations.

---

## Appendix

### A.1 Glossary

| Term | Meaning |
|---|---|
| **Flit** | The unit a link moves per cycle; a packet is L flits |
| **NI** | Network interface: a node's on-ramp to the network |
| **VC** | Virtual channel: one of several queues sharing a physical link |
| **Vnet** | Virtual network: a class of traffic with its own VCs (requests, responses, …) |
| **Utilization ρ** | Fraction of time a resource is busy; waits explode as ρ → 1 |
| **C², squared coefficient of variation** | Variance / mean²; for arrival gaps, 1 ≈ random, > 1 = bursty |
| **GGeo** | A discrete-time bursty arrival model with rate λ and burst probability p_b |
| **Zero-load latency** | Trip time through an empty network: T_hop(H) = 4 + 2H for a flit, plus the packet's own NI time τ |
| **Merge factor 1 - h** | Fraction of packet pairs on a link that come through different input ports and so can collide; 0 for a link with one input |
| **Completion latency** | Time until the last copy of a multicast arrives |

### A.2 Where each idea comes from

| Idea | Source |
|---|---|
| GGeo arrivals, C²_A = 2/(1 - p_b) - λ - 1, virtual-queue fit of p_b | Mandal et al. 2020 |
| Fitting p_b on messages, not copies; one queue per (source, vnet) | Ours |
| Multicast as a fixed-size batch in a bursty NI queue; B(K, L) from the schedule | Ours |
| Slotted batch-queue backlog E[U] = E[A(A-1)]/(2(1 - ρ)); length bias Δ | Standard (re-derived) |
| VC- and credit-limited copy schedule (V = 4, R = 5 from the router pipeline) | Ours (derived from Garnet's design, checked against measurement) |
| Channel decomposition; copies as per-destination streams; completion as a maximum | Moadeli & Vanderbauwhede 2009 |
| Exact single-flit link wait; "one input never queues" | Karol, Hluchyj & Morgan 1987 |
| Merge-factor link model (17) | Ours (generalizes Karol et al. with the P-K wait) |
| Pollaczek-Khinchine queue wait | Standard |
| Two-moment link alternative: GGeo/G/1 wait, departure rule, merge rule | Kouvatsos 1994; Mandal et al. 2021 (eqs. 4-5, 18, 19); Whitt's QNA |
| Pre-replication NI trace hook; NI-arrival timestamp correction | Ours |

### A.3 References

- **[MV09]** M. Moadeli, W. Vanderbauwhede, "A Performance Model of
  Multicast Communication in Wormhole-Routed Networks on-Chip," IEEE
  IPDPS 2009.
- **[M20]** S. K. Mandal, R. Ayoub, M. Kishinevsky, M. M. Islam,
  U. Y. Ogras, "Analytical Performance Modeling of NoCs under Priority
  Arbitration and Bursty Traffic," IEEE Embedded Systems Letters 2020;
  arXiv:2007.13951.
- **[M21]** S. K. Mandal, J. Tong, R. Ayoub, M. Kishinevsky,
  A. Abousamra, U. Y. Ogras, "Theoretical Analysis and Evaluation of NoCs
  with Weighted Round-Robin Arbitration," arXiv:2108.09534.
- **[K94]** D. D. Kouvatsos, "Entropy Maximisation and Queueing Network
  Models," Annals of Operations Research 48(1):63-126, 1994.
- **[KHM87]** M. J. Karol, M. G. Hluchyj, S. P. Morgan, "Input Versus
  Output Queueing on a Space-Division Packet Switch," IEEE Trans.
  Communications 35(12), 1987.
- **[W83]** W. Whitt, "The Queueing Network Analyzer," Bell System
  Technical Journal 62(9), 1983.
- **[HR90]** J. Y. Hui, T. Renner, "Queueing Analysis for Multicast
  Packet Switching," 1990 (fan-out as batch service in ATM switches).
- **gem5:** N. Binkert et al., "The gem5 Simulator," SIGARCH CAN 2011;
  Garnet 3.0 in gem5 v26.
- **SPLASH-4:** E. J. Gómez-Hernández et al., "Splash-4: A Modern
  Benchmark Suite with Lock-Free Constructs," IISWC 2022.
- **PARSEC:** C. Bienia, S. Kumar, J. P. Singh, K. Li, "The PARSEC
  Benchmark Suite," PACT 2008.

### A.4 Project files

| Path | What |
|---|---|
| `gem5/` (uncommitted diff) | Trace hook and ROI cap (section 4.3); multicast and bursty synthetic injection (section 4.6.1) |
| `gem5/build/X86_MESI_hammer/gem5.opt` | Coherence-workload binary |
| `gem5/build/Garnet_standalone/gem5.opt` | Synthetic-traffic binary |
| `benchmarks/micro/cohmicro.c` | Our microbenchmarks |
| `benchmarks/parsec-hooks/hooks.c`, `scripts/build_parsec.sh` | PARSEC ROI hooks and build |
| `results/<protocol>/<bench>/` | Traces, stats, config of each run |
| `results/garnet_synth/` | Synthetic runs (unicast) |
| `results/dse/` | Design-space exploration results, one CSV per experiment (section 7.8) |
| `results/garnet_synth_mc/`, `results/garnet_synth_mc.csv` | High-injection runs with multicast and bursts, and model vs. Garnet (section 7.4) |
| `results/bmq_validation.csv` | **The model's validation:** parameters fitted per run (global and per node), predicted vs. measured latencies (`fit_params.py RUN_DIR... --csv`) |
| `results/bmq_results.csv`, `results/bmq_results_full.csv` | Phase 1 / trace-driven predictions and the baselines (`bmq_model.py`) |
| `results/model_compare.csv`, `results/mesh{6,8}/model_compare.csv` | All four models vs. Garnet, per run, per mesh (section 7.9) |
| `results/model_compare_summary.csv` | The section 7.9 error tables |
| `results/mesh6/`, `results/mesh8/` | 6×6 and 8×8 runs (same layout as `results/`), `runs.log` |
| `results/garnet_synth_linkmodels.csv` | Synthetic sweep, all three link models |
| `results/summary*.csv` | Motivation metrics |
| `UNIFIED_MODEL.md` | The model alone (section 2 of this guide) |
| `PHASE1_REPORT.md`, `DATA_COLLECTION.md`, `RUNNING.md`, `STATUS.md` | Phase 1 report, data collection, how to run, status |
