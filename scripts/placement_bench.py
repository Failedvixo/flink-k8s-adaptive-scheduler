#!/usr/bin/env python3
"""How far from optimal are the placement heuristics, as the problem grows?

WHY THIS EXISTS (2026-08-18). Every in-cluster experiment so far ran with 4
slices on 3 TaskManagers, where the placement problem is so small that every
policy finds the same answer: measured across five separate runs, all six arms
produced identical dispersion, and stock Flink matched them. That is not
evidence that placement policy is irrelevant — it is evidence that the instance
was trivial. A metaheuristic cannot beat a greedy rule on a problem a greedy
rule solves exactly.

So this asks the question the cluster cannot: at what problem size do fast
heuristics start leaving throughput on the table? The instance is enumerated
exactly, so the answer is measured against the TRUE optimum rather than against
a competitor — which makes it a statement about the problem, not a contest, and
removes any suspicion that the scenario was picked to favour one policy.

Two things make an instance non-trivial, and BOTH are needed:
  * graded slice costs. With slot sharing a slice carries one subtask of every
    vertex whose parallelism reaches its index, so STAGGERED parallelisms give
    slices a spread of costs. (Adding vertices at the SAME width does not: the
    slice count is set by the widest vertex, so the placement space is unchanged
    — a correction worth keeping, since "more operators" sounds like it should
    enlarge the problem and does not.)
  * heterogeneous machines. Identical TaskManagers make placements equivalent
    under permutation, which is what collapsed the search space in every run so
    far.

Together they are a generalised assignment problem: minimise makespan over
unrelated machines, which is NP-hard — and the setting where a metaheuristic has
something to justify.

Usage:
    python3 scripts/placement_bench.py                # the default sweep
    python3 scripts/placement_bench.py --instances 500
"""

import argparse
import itertools
import random
import statistics as st


def slice_costs(vertex_parallelisms, vertex_costs):
    """Cost of each slice: the sum over the vertices whose parallelism reaches it.

    Slice i holds subtask i of every vertex with parallelism > i, so a vertex
    pinned narrower than the rest appears in only the first few slices. That gap
    is the whole source of slice heterogeneity.
    """
    width = max(vertex_parallelisms)
    return [
        sum(c for p, c in zip(vertex_parallelisms, vertex_costs) if p > i)
        for i in range(width)
    ]


def makespan(assignment, costs, speeds, slots_per_tm):
    """Load of the busiest machine; None if the assignment overflows a machine.

    Load is cost/speed, so a slower TaskManager takes proportionally longer for
    the same work — the "unrelated machines" part of the model.
    """
    load = [0.0] * len(speeds)
    used = [0] * len(speeds)
    for slice_index, tm in enumerate(assignment):
        used[tm] += 1
        if used[tm] > slots_per_tm:
            return None
        load[tm] += costs[slice_index] / speeds[tm]
    return max(load)


def optimal(costs, speeds, slots_per_tm):
    best = None
    for assignment in itertools.product(range(len(speeds)), repeat=len(costs)):
        m = makespan(assignment, costs, speeds, slots_per_tm)
        if m is not None and (best is None or m < best):
            best = m
    return best


# ---- the policies, as the fork implements them --------------------------------
def h_fcfs(costs, speeds, slots_per_tm):
    """Iteration order: slice i takes the next free slot, slots grouped by TM."""
    assignment, used = [], [0] * len(speeds)
    for _ in costs:
        for tm in range(len(speeds)):
            if used[tm] < slots_per_tm:
                assignment.append(tm)
                used[tm] += 1
                break
    return assignment


def h_round_robin(costs, speeds, slots_per_tm):
    assignment, used = [], [0] * len(speeds)
    tm = 0
    for _ in costs:
        for _ in range(len(speeds)):
            if used[tm] < slots_per_tm:
                break
            tm = (tm + 1) % len(speeds)
        assignment.append(tm)
        used[tm] += 1
        tm = (tm + 1) % len(speeds)
    return assignment


def h_least_loaded(costs, speeds, slots_per_tm):
    """Greedy on current load, slices in index order — no lookahead."""
    assignment, used, load = [], [0] * len(speeds), [0.0] * len(speeds)
    for i, cost in enumerate(costs):
        options = [t for t in range(len(speeds)) if used[t] < slots_per_tm]
        tm = min(options, key=lambda t: load[t])
        assignment.append(tm)
        used[tm] += 1
        load[tm] += cost / speeds[tm]
    return assignment


def h_lpt(costs, speeds, slots_per_tm):
    """Longest-processing-time-first: the classic greedy with a known bound.

    Included as the strong baseline. If a metaheuristic cannot beat LPT there is
    little reason to run one, and the fork has no arm that does this today.
    """
    assignment = [0] * len(costs)
    used, load = [0] * len(speeds), [0.0] * len(speeds)
    for i in sorted(range(len(costs)), key=lambda i: -costs[i]):
        options = [t for t in range(len(speeds)) if used[t] < slots_per_tm]
        tm = min(options, key=lambda t: load[t] + costs[i] / speeds[t])
        assignment[i] = tm
        used[tm] += 1
        load[tm] += costs[i] / speeds[tm]
    return assignment


HEURISTICS = [("FCFS", h_fcfs), ("ROUND_ROBIN", h_round_robin),
              ("LEAST_LOADED", h_least_loaded), ("LPT", h_lpt)]


def random_instance(rng, n_vertices, width, n_tms, slots_per_tm, heterogeneous):
    # Staggered parallelisms: one vertex at full width, the rest narrower, which
    # is what an independently-scaled expensive operator looks like.
    pars = [width] + [rng.randint(1, width) for _ in range(n_vertices - 1)]
    vcosts = [round(rng.uniform(0.2, 1.0), 3) for _ in range(n_vertices)]
    vcosts[rng.randrange(n_vertices)] = round(rng.uniform(3.0, 8.0), 3)  # one dominant stage
    speeds = ([round(rng.choice([0.5, 0.75, 1.0]), 2) for _ in range(n_tms)]
              if heterogeneous else [1.0] * n_tms)
    return slice_costs(pars, vcosts), speeds


def sweep(instances, seed, heterogeneous):
    rng = random.Random(seed)
    print(f"  {'slices':>6} {'TMs':>4} {'slots':>6}   " +
          "".join(f"{name:>14}" for name, _ in HEURISTICS))
    print("  " + "-" * (20 + 14 * len(HEURISTICS)))
    for width, n_tms, slots_per_tm in ((4, 3, 2), (6, 3, 2), (6, 4, 2),
                                       (8, 4, 2), (8, 4, 3)):
        if width > n_tms * slots_per_tm:
            continue
        gaps = {name: [] for name, _ in HEURISTICS}
        for _ in range(instances):
            costs, speeds = random_instance(
                rng, n_vertices=5, width=width, n_tms=n_tms,
                slots_per_tm=slots_per_tm, heterogeneous=heterogeneous)
            best = optimal(costs, speeds, slots_per_tm)
            if not best:
                continue
            for name, fn in HEURISTICS:
                m = makespan(fn(costs, speeds, slots_per_tm), costs, speeds, slots_per_tm)
                gaps[name].append(100.0 * (m - best) / best)
        row = f"  {width:>6} {n_tms:>4} {n_tms * slots_per_tm:>6}   "
        for name, _ in HEURISTICS:
            row += f"{st.mean(gaps[name]):>13.1f}%"
        print(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    print("=" * 76)
    print("  Mean gap to the EXACT optimum (makespan), lower is better")
    print("=" * 76)
    print("\n  IDENTICAL TaskManagers — the cluster as it has been all along:")
    sweep(args.instances, args.seed, heterogeneous=False)
    print("\n  HETEROGENEOUS TaskManagers (speeds 0.5 / 0.75 / 1.0):")
    sweep(args.instances, args.seed, heterogeneous=True)
    print("\n  A gap of 0% means the heuristic is already optimal and no search")
    print("  can improve on it. Room for ACO/GA exists only where the gap is not 0.")


if __name__ == "__main__":
    main()
