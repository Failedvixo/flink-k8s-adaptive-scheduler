#!/usr/bin/env python3
"""Does placement stop being a greedy's problem once demand has TWO dimensions?

WHY THIS EXISTS (2026-09-02). scripts/placement_bench.py answered the
single-resource question and the answer closed a door: LPT sits 1-3% from the
exact optimum whatever the size of the instance, from 3 machines to 11 and from
2.2e3 placements to 6.7e20, because LPT is a 4/3-approximation whose guarantee
does not decay with n. Growing the search space alone gives a metaheuristic a
COMPUTATIONAL argument and no quality one.

The dimension the project never had is the one CETSA's model is built on. Li et
al. score a node by `0.8*U_cpu + 0.2*U_mem` over a cluster whose machines do not
share a cores-to-RAM ratio (4c/8GB, 8c/12GB, 12c/16GB — 2.0, 1.5 and 1.33 GB per
core), and their workloads are not CPU-shaped: Wordcount's counter holds keyed
state, Fixwindow's aggregate holds window buffers. Assigning a vector of demands
to machines with a vector of capacities is a different problem from minimising a
makespan — it is vector bin packing, and a greedy that sorts on one coordinate
has no reason to be near-optimal on the other.

So this asks: how much does a search beat a greedy as the two coordinates stop
agreeing? The sweep runs from rho=+1 (memory is a rescaled copy of CPU, which is
the degenerate case the thesis has measured so far) to rho=-1 (the memory-heavy
operator is the CPU-light one, which is what a counter beside a splitter looks
like).

THE CONTROL THAT MAKES IT A FINDING RATHER THAN A HOPE: the same sweep runs on a
cluster whose machines have identical RAM per core. If the gap is really about
the second dimension, it must collapse there — and if it does not, the gap is
about something else and the sweep proves nothing.

Reference is simulated annealing, not enumeration: 11^12 placements cannot be
enumerated. It is a lower bound on what a search finds, which makes every gap
reported here CONSERVATIVE.

Usage:
    python3 scripts/placement_bench_cetsa.py
    python3 scripts/placement_bench_cetsa.py --instances 40 --iters 40000
"""

import argparse
import math
import random
import statistics as st

# CETSA's cluster, Table 4: 12 VMs, one Small carrying the JobManager. Cores are
# the CPU capacity and the relative speed at once; RAM is the second capacity,
# and the 0.8 threshold is the paper's own ("the resource threshold of a node is
# 80% of the total resources").
CETSA = ([4.0] * 3 + [8.0] * 4 + [12.0] * 4,      # cores
         [8.0] * 3 + [12.0] * 4 + [16.0] * 4)     # GB
# The control: same cores, RAM scaled to a constant 1.33 GB per core, so the two
# capacities carry exactly the same information and the problem is 1-D again.
FLAT = (CETSA[0], [round(c * 4.0 / 3.0, 2) for c in CETSA[0]])

SLOTS_PER_TM = 4
MEM_THRESHOLD = 0.8
W_CPU, W_MEM = 0.8, 0.2


def slice_demands(rng, n_vertices, width, rho, cores, memory, mem_util):
    """One (cpu, mem) demand per slice, with a given CPU/memory correlation.

    Slices get graded costs the same way placement_bench does: staggered
    parallelisms mean a vertex pinned narrower than the rest appears in only the
    first few slices. One vertex dominates each coordinate — the CPU-heavy
    operator the thesis already has, and the state-heavy one it does not.
    """
    pars = [width] + [rng.randint(1, width) for _ in range(n_vertices - 1)]
    cpu = [rng.uniform(0.2, 1.0) for _ in range(n_vertices)]
    cpu[rng.randrange(n_vertices)] = rng.uniform(3.0, 8.0)

    # Memory correlated with CPU at the requested rho, then rescaled to GB.
    lo, hi = min(cpu), max(cpu)
    span = max(hi - lo, 1e-9)
    mem = []
    for c in cpu:
        z = (c - lo) / span
        noise = rng.random()
        mixed = rho * z + math.sqrt(max(0.0, 1.0 - rho * rho)) * noise
        mem.append(0.2 + 3.3 * min(1.0, max(0.0, (mixed + 1.0) / 2.0)))

    slices = [(sum(c for p, c in zip(pars, cpu) if p > i),
               sum(m for p, m in zip(pars, mem) if p > i))
              for i in range(width)]

    # THE SCALE HAS TO BE FIXED OR THE SWEEP MEASURES THE WRONG THING. Raw sums
    # put a slice's footprint anywhere from under a gigabyte to over fifteen, so
    # rho would change how OFTEN an instance is satisfiable at all and the
    # correlation's effect would be unreadable underneath. Memory is rescaled so
    # every instance asks the cluster for the same share of its usable RAM, and
    # capped so no single slice is unplaceable on the smallest node — what varies
    # across the sweep is then the SHAPE of the demand and nothing else.
    usable = MEM_THRESHOLD * sum(memory)
    total = sum(m for _, m in slices)
    scale = mem_util * usable / max(total, 1e-9)
    ceiling = MEM_THRESHOLD * min(memory)
    return [(c, min(m * scale, ceiling)) for c, m in slices]


def node_loads(assignment, demands, cores, memory):
    """Per-machine (cpu, mem) totals, or None when the placement is infeasible.

    Infeasible means what CETSA means: more slices than slots, or memory past the
    node's threshold. A placement that cannot run is not a placement, and a
    heuristic that produces them is reported for producing them rather than
    silently scored on the ones that happened to fit.
    """
    n = len(cores)
    cpu = [0.0] * n
    mem = [0.0] * n
    used = [0] * n
    for g, t in enumerate(assignment):
        used[t] += 1
        if used[t] > SLOTS_PER_TM:
            return None
        cpu[t] += demands[g][0]
        mem[t] += demands[g][1]
    for t in range(n):
        if mem[t] > MEM_THRESHOLD * memory[t]:
            return None
    return cpu, mem


def cost(assignment, demands, cores, memory):
    """CETSA's node load, worst machine: 0.8*U_cpu + 0.2*U_mem.

    Both utilisations are divided by what a perfectly divisible schedule would
    reach, so 1.0 is the ideal and the number is comparable across instances.
    """
    loads = node_loads(assignment, demands, cores, memory)
    if loads is None:
        return None
    cpu, mem = loads
    ideal_cpu = sum(d[0] for d in demands) / sum(cores)
    ideal_mem = sum(d[1] for d in demands) / sum(memory)
    worst = 0.0
    for t in range(len(cores)):
        u_cpu = (cpu[t] / cores[t]) / max(ideal_cpu, 1e-9)
        u_mem = (mem[t] / memory[t]) / max(ideal_mem, 1e-9)
        worst = max(worst, W_CPU * u_cpu + W_MEM * u_mem)
    return worst


# ---------------------------------------------------------------------------
# the heuristics
# ---------------------------------------------------------------------------
def h_lpt_cpu(demands, cores, memory):
    """The fork's LPT, exactly as it is today: sorts and places on CPU alone.

    It is not memory-blind by oversight — the published load vector has one
    number per vertex, so there is no second coordinate for it to read.
    """
    order = sorted(range(len(demands)), key=lambda g: (-demands[g][0], g))
    cpu = [0.0] * len(cores)
    used = [0] * len(cores)
    out = [None] * len(demands)
    for g in order:
        best, best_finish = None, None
        for t in range(len(cores)):
            if used[t] >= SLOTS_PER_TM:
                continue
            finish = (cpu[t] + demands[g][0]) / cores[t]
            if best_finish is None or finish < best_finish:
                best, best_finish = t, finish
        if best is None:
            return None
        out[g] = best
        cpu[best] += demands[g][0]
        used[best] += 1
    return out


def h_lpt_weighted(demands, cores, memory):
    """LPT on CETSA's weighted load, and refusing to overflow a node's memory.

    The smallest honest upgrade to the arm the fork ships: same greedy, same
    ordering rule, one coordinate more.
    """
    ideal_cpu = sum(d[0] for d in demands) / sum(cores)
    ideal_mem = sum(d[1] for d in demands) / sum(memory)
    order = sorted(range(len(demands)),
                   key=lambda g: -(W_CPU * demands[g][0] / max(ideal_cpu, 1e-9)
                                   + W_MEM * demands[g][1] / max(ideal_mem, 1e-9)))
    cpu = [0.0] * len(cores)
    mem = [0.0] * len(cores)
    used = [0] * len(cores)
    out = [None] * len(demands)
    for g in order:
        best, best_score = None, None
        for t in range(len(cores)):
            if used[t] >= SLOTS_PER_TM:
                continue
            if mem[t] + demands[g][1] > MEM_THRESHOLD * memory[t]:
                continue
            score = (W_CPU * ((cpu[t] + demands[g][0]) / cores[t]) / max(ideal_cpu, 1e-9)
                     + W_MEM * ((mem[t] + demands[g][1]) / memory[t]) / max(ideal_mem, 1e-9))
            if best_score is None or score < best_score:
                best, best_score = t, score
        if best is None:
            return None
        out[g] = best
        cpu[best] += demands[g][0]
        mem[best] += demands[g][1]
        used[best] += 1
    return out


def h_best_fit_decreasing(demands, cores, memory):
    """GS-BFD, the greedy CETSA compares itself against: heaviest first into the
    node that ends up TIGHTEST while still fitting."""
    order = sorted(range(len(demands)), key=lambda g: -demands[g][1])
    cpu = [0.0] * len(cores)
    mem = [0.0] * len(cores)
    used = [0] * len(cores)
    out = [None] * len(demands)
    for g in order:
        best, best_slack = None, None
        for t in range(len(cores)):
            if used[t] >= SLOTS_PER_TM:
                continue
            slack = MEM_THRESHOLD * memory[t] - mem[t] - demands[g][1]
            if slack < 0:
                continue
            if best_slack is None or slack < best_slack:
                best, best_slack = t, slack
        if best is None:
            return None
        out[g] = best
        cpu[best] += demands[g][0]
        mem[best] += demands[g][1]
        used[best] += 1
    return out


HEURISTICS = [("LPT (solo CPU)", h_lpt_cpu),
              ("LPT ponderado", h_lpt_weighted),
              ("GS-BFD", h_best_fit_decreasing)]


def search(demands, cores, memory, iters, seed):
    """Simulated annealing over the full two-dimensional objective."""
    rng = random.Random(seed)
    n_tm = len(cores)
    cur = h_lpt_weighted(demands, cores, memory)
    if cur is None:
        # Start from any feasible packing the annealer can repair.
        cur = h_best_fit_decreasing(demands, cores, memory)
        if cur is None:
            return None
    cur_cost = cost(cur, demands, cores, memory)
    best, best_cost = list(cur), cur_cost
    t0 = cur_cost * 0.25
    for i in range(iters):
        temp = t0 * (1 - i / iters) + 1e-9
        cand = list(cur)
        if rng.random() < 0.5:
            cand[rng.randrange(len(cand))] = rng.randrange(n_tm)
        else:
            a, b = rng.randrange(len(cand)), rng.randrange(len(cand))
            cand[a], cand[b] = cand[b], cand[a]
        c = cost(cand, demands, cores, memory)
        if c is None:
            continue
        if c < cur_cost or rng.random() < math.exp(-(c - cur_cost) / max(temp, 1e-9)):
            cur, cur_cost = cand, c
            if c < best_cost:
                best, best_cost = list(cand), c
    return best_cost


def sweep(label, cores, memory, rows, instances, width, iters, seed, by):
    """One table. `rows` is either the correlations to sweep or the memory
    tightnesses, depending on `by` — the two questions the bench exists to ask.

    TIGHTNESS IS THE ONE THAT SHOULD MATTER. A second coordinate only changes the
    problem while it BINDS: at 60% of usable RAM every placement fits, memory
    never refuses anything, and 0.8*U_cpu + 0.2*U_mem is a CPU objective wearing
    a hat. What forces a CPU-suboptimal choice is a machine that has the cores
    but no longer has the gigabytes.
    """
    print(f"\n{label}  ({len(cores)} TMs, {width} slices, {SLOTS_PER_TM} slots c/u)")
    header = f"  {('rho(cpu,mem)' if by == 'rho' else 'RAM pedida'):>13}"
    for name, _ in HEURISTICS:
        header += f"{name:>18}"
    print(header)
    print("  " + "-" * (13 + 18 * len(HEURISTICS)))
    for row in rows:
        rho = row if by == "rho" else args_rho_fixed[0]
        mem_util = args_rho_fixed[1] if by == "rho" else row
        gaps = {name: [] for name, _ in HEURISTICS}
        infeasible = {name: 0 for name, _ in HEURISTICS}
        rng = random.Random(seed)
        for k in range(instances):
            demands = slice_demands(rng, 5, width, rho, cores, memory, mem_util)
            reference = search(demands, cores, memory, iters, seed=k)
            if reference is None:
                continue
            for name, fn in HEURISTICS:
                placement = fn(demands, cores, memory)
                c = cost(placement, demands, cores, memory) if placement else None
                if c is None:
                    infeasible[name] += 1
                else:
                    gaps[name].append(100.0 * (c - reference) / reference)
        line = f"  {row:>13.2f}"
        for name, _ in HEURISTICS:
            done = len(gaps[name]) + infeasible[name]
            if gaps[name]:
                mark = f"{st.mean(gaps[name]):5.1f}%  {100 * infeasible[name] / max(done, 1):3.0f}% inf"
            else:
                mark = f"   —    {100 * infeasible[name] / max(done, 1):3.0f}% inf"
            line += f"{mark:>18}"
        print(line)


# Set by main(): (rho, mem_util) for whichever coordinate the table holds fixed.
args_rho_fixed = (-1.0, 0.6)


def main():
    global args_rho_fixed
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=25)
    ap.add_argument("--iters", type=int, default=25000)
    ap.add_argument("--width", type=int, default=12)
    ap.add_argument("--mem-util", type=float, default=0.6,
                    help="share of the cluster's usable RAM every instance asks for")
    ap.add_argument("--sweep", choices=["rho", "tightness"], default="rho")
    ap.add_argument("--mem-utils", default="0.5,0.7,0.85,0.95",
                    help="tightness sweep: shares of usable RAM to ask for")
    ap.add_argument("--rho-fixed", type=float, default=-1.0,
                    help="tightness sweep: the correlation to hold fixed")
    ap.add_argument("--rhos", default="1.0,0.5,0.0,-0.5,-1.0",
                    help="CPU/memory correlations to sweep")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    print("=" * 78)
    print("  Brecha media contra una búsqueda sobre el objetivo completo (0.8*U_cpu + 0.2*U_mem)")
    print("  Menor es mejor. 'N% inf' = fracción de emplazamientos del brazo que no caben.")
    print("=" * 78)
    if args.sweep == "rho":
        rows = [float(x) for x in args.rhos.split(",")]
        args_rho_fixed = (None, args.mem_util)
        note = f"correlación variable, RAM pedida = {args.mem_util:.0%} de la utilizable"
    else:
        rows = [float(x) for x in args.mem_utils.split(",")]
        args_rho_fixed = (args.rho_fixed, None)
        note = f"RAM pedida variable, rho fijo en {args.rho_fixed:+.1f}"
    print(f"  {note}")
    sweep("CETSA (4c/8GB, 8c/12GB, 12c/16GB)", *CETSA, rows=rows,
          instances=args.instances, width=args.width, iters=args.iters,
          seed=args.seed, by=args.sweep)
    sweep("CONTROL: misma RAM por núcleo en todas", *FLAT, rows=rows,
          instances=args.instances, width=args.width, iters=args.iters,
          seed=args.seed, by=args.sweep)


if __name__ == "__main__":
    main()
