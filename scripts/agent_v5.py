#!/usr/bin/env python3
"""Placement agent v5: estimates how well each operator would run on each machine.

WHY A NEW FORMULATION (2026-10-11). Versions 1-4 learned a TABLE indexed by labels we chose —
load HIGH/LOW, disk Y/N against a fixed threshold, free-slot buckets — with the machines as bare
action numbers. What it learned was real (with the disk cap moved to another machine it learned
the opposite policy) but it could only ever come out as a set of rules, and three measured failures
trace back to that form:

  * Q3 at 30 000 ev/s (old source): "medium has two free slots" read the same with two joins on it
    as with two sinks — the state did not say what a machine already carried.
  * The machine-load feature added to fix that did not transfer from Q8 to Q3: each combination is
    its own row, and Q3's combinations never occurred in Q8.
  * Q5 with CAPSys' window (60 s / 1 s) at 4500 ev/s: the counts rewrite ~3 MB/s to disk, which
    FITS in the capped machine's 5 MB/s, and that machine has twice the cores. The right plan puts a
    count there (p50 latency 2.8 s against 4.7 s). Any fixed "writes to disk" threshold — v3's
    logical bytes or v4's 1 MB/s of compaction — sends it away.

Here the agent describes every (slice, candidate machine) pair by quantities, not labels: how much
CPU the slice needs against the cores the machine has left, how much it rewrites to disk against the
disk the machine has left, both counting what this plan has already put there. Whether 3 MB/s
"is disk-bound" is no longer our call: it is 0.6 of fast's disk, and 7 MB/s is 1.4 of it. The value
of a pair is a LINEAR function of those features with learned weights, so what is learned about
"a machine pushed past its disk" holds for any operator, machine or query, seen or not.

LEARNING. Each placement decision is a context (the features of the chosen pair) and gets a reward
(the episode's, plus the slice's own local terms, computed by the caller). That is a contextual
bandit, solved with LinUCB (Li et al., WWW 2010): ridge regression of reward on features, plus an
exploration bonus that shrinks where the agent has evidence. Frozen, it is greedy on the regression.

ORDER IS DECIDED BY THE ESTIMATES TOO. Instead of "saturated first, then by busy time" — which in Q5
gave the sources the fast machine before the counts could ask for it — at every step the agent
places the remaining slice whose best machine is most clearly better than its worst: the one with
most to lose. "Most constrained first", read off the learned values.

WHAT THE AGENT IS TOLD ABOUT MACHINES. Cores and, where it is limited, disk bandwidth — the spec of
the instance, which a scheduler on a real cluster knows. Whether a given quantity hurts is learned.
(Estimating the disk bandwidth itself from observed RocksDB stalls is the natural next step.)

Usage: scripts/agent_v5.py --selftest     # offline check on a simulated cluster
"""
import argparse
import itertools
import json
import math
import random
from pathlib import Path

import numpy as np

FEATURES = [
    "bias",
    "cpu_util",        # (CPU already planned on the machine + this slice) / machine cores
    "cpu_over",        # max(0, cpu_util - 1): demand the machine cannot serve
    "disk_util",       # (disk rewrite already planned + this slice) / machine disk bandwidth
    "disk_over",       # max(0, disk_util - 1)
    "sat_per_core",    # 1/cores if the slice is saturated: its true demand is unknown and above 1
    "sat_util",        # saturated slices on the machine / its cores: a saturated slice measures
                       # at most one core while needing more, so its measured CPU under-counts
    "slot_util",       # slots used on the machine / its slots
]
UNLIMITED_DISK_MBPS = 200.0     # a machine without a cap: its disk is never the constraint here


def pair_features(slice_, machine, others):
    """Features of `slice_` on `machine`, where `others` is what ELSE the plan puts there
    ({'cpu', 'disk', 'slots', 'sat'}). Whole-plan totals, so the order of decision is irrelevant."""
    cores = machine["cores"]
    disk = machine.get("disk_mbps") or UNLIMITED_DISK_MBPS
    cpu_util = (others["cpu"] + slice_["cpu"]) / cores
    disk_util = (others["disk"] + slice_["disk"]) / disk
    sat = others["sat"] + (1 if slice_["saturated"] else 0)
    return np.array([
        1.0,
        cpu_util,
        max(0.0, cpu_util - 1.0),
        disk_util,
        max(0.0, disk_util - 1.0),
        (1.0 / cores) if slice_["saturated"] else 0.0,
        sat / cores,
        (others["slots"] + 1) / machine["slots"],
    ])


class MachineModel:
    """What each machine can take, LEARNED from what was observed on it — not declared.

    WHY (2026-10-11, Vicente: "sería mejor que el agente aprendiera las capacidades del clúster").
    The scheduler should not need to be told that fast has 4 cores and a 5 MB/s disk. Both show up
    in telemetry the harness already collects:

      * CPU — the TaskManager's CPU seconds per second (Status.JVM.CPU.Time). While its tasks
        contend for cores (more saturated tasks than the cores they get), that figure IS the
        machine's capacity; while they do not, it is only a lower bound.
      * Disk — the compaction bytes RocksDB rewrites per second on the machine, and whether its
        tasks stalled. Writing X MB/s WITH stalls bounds the disk from above by X; writing X MB/s
        without them bounds it from below.

    Each machine keeps an interval [lower, upper] per resource that closes as evidence arrives.
    Decisions use the optimistic end — an unknown upper bound is taken as plenty — so a machine is
    tried before it is written off, which is how the agent explores what it does not yet know.
    Keyed by TaskManager id, so a learned model is specific to a cluster, as capacities are.
    """

    OPTIMISTIC_CORES = 8.0      # assumed while no contention has been seen on a machine

    def __init__(self, stored=None):
        self.m = dict(stored or {})

    def _entry(self, mid):
        return self.m.setdefault(mid, {"cpu_lo": 0.0, "cpu_hi": None,
                                       "disk_lo": 0.0, "disk_hi": None})

    def observe(self, mid, cpu_used, cpu_contended, disk_written, disk_stalled):
        e = self._entry(mid)
        if cpu_contended:
            e["cpu_hi"] = cpu_used if e["cpu_hi"] is None else 0.7 * e["cpu_hi"] + 0.3 * cpu_used
        e["cpu_lo"] = max(e["cpu_lo"], cpu_used)
        if disk_stalled and disk_written > 0:
            e["disk_hi"] = disk_written if e["disk_hi"] is None else min(e["disk_hi"], disk_written)
        elif not disk_stalled:
            e["disk_lo"] = max(e["disk_lo"], disk_written)

    def view(self, mid, slots):
        """The machine as the features see it: estimated cores and disk, plus its slots."""
        e = self._entry(mid)
        cores = e["cpu_hi"] if e["cpu_hi"] is not None else max(self.OPTIMISTIC_CORES, e["cpu_lo"])
        disk = e["disk_hi"]                        # None = no stall seen: the disk never bound
        return {"id": mid, "cores": max(cores, 0.1), "disk_mbps": disk, "slots": slots}

    def to_json(self):
        return self.m


class LinUCBPlacer:
    """Ridge regression of decision reward on pair features, with an optimism bonus."""

    def __init__(self, path=None, alpha=0.5, lam=1.0):
        self.path = Path(path) if path else None
        self.alpha = alpha
        d = len(FEATURES)
        self.A = lam * np.eye(d)
        self.b = np.zeros(d)
        self.n = 0
        if self.path and self.path.exists():
            stored = json.loads(self.path.read_text())
            if stored.get("features") != FEATURES:
                raise ValueError(f"{self.path} was trained with other features: "
                                 f"{stored.get('features')}")
            self.A = np.array(stored["A"])
            self.b = np.array(stored["b"])
            self.n = stored.get("n", 0)

    def theta(self):
        return np.linalg.solve(self.A, self.b)

    def score(self, x, explore):
        value = float(self.theta() @ x)
        if explore and self.alpha > 0:
            value += self.alpha * math.sqrt(float(x @ np.linalg.solve(self.A, x)))
        return value

    def update(self, x, reward):
        self.A += np.outer(x, x)
        self.b += reward * x
        self.n += 1

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "features": FEATURES, "A": self.A.tolist(), "b": self.b.tolist(),
                "n": self.n, "weights": dict(zip(FEATURES, self.theta().round(4).tolist())),
            }, indent=2))


def plan_features(slices, machines, plan):
    """[(features, slice index)] for a complete plan {slice index: machine index}."""
    totals = [{"cpu": 0.0, "disk": 0.0, "slots": 0, "sat": 0} for _ in machines]
    for i, m in plan.items():
        totals[m]["cpu"] += slices[i]["cpu"]
        totals[m]["disk"] += slices[i]["disk"]
        totals[m]["slots"] += 1
        totals[m]["sat"] += 1 if slices[i]["saturated"] else 0
    out = []
    for i, m in plan.items():
        others = {"cpu": totals[m]["cpu"] - slices[i]["cpu"],
                  "disk": totals[m]["disk"] - slices[i]["disk"],
                  "slots": totals[m]["slots"] - 1,
                  "sat": totals[m]["sat"] - (1 if slices[i]["saturated"] else 0)}
        out.append((pair_features(slices[i], machines[m], others), i))
    return out


def feasible_plans(slices, machines, limit=200_000):
    """Every assignment that fits the slots, or None if there are more than `limit`."""
    n, k = len(slices), len(machines)
    if k ** n > limit * 10:
        return None
    plans = []
    for combo in itertools.product(range(k), repeat=n):
        if all(combo.count(m) <= machines[m]["slots"] for m in range(k)):
            plans.append(dict(enumerate(combo)))
            if len(plans) > limit:
                return None
    return plans


def decide(agent, slices, machines, explore=False):
    """The plan with the highest summed estimate over its (slice, machine) pairs.

    WHOLE PLANS, NOT ONE SLICE AT A TIME (2026-10-11). Deciding slice by slice is myopic: in the
    simulated Q5 with CAPSys' window both bid sources fit comfortably on fast, so they took it, and
    the counts that needed its cores arrived to a full machine — the same thing the cluster did.
    No ordering rule fixes that, because at the first step nothing yet distinguishes the slices.
    Scoring every complete assignment (6561 at most for 8 slices on 3 machines, milliseconds)
    makes each slice's features describe its machine as the WHOLE plan leaves it.

    Returns (plan, trajectory [(features, slice index)]) or (None, []) if nothing fits.
    """
    plans = feasible_plans(slices, machines)
    if not plans:
        return None, []
    # The regression is solved ONCE per decision; every plan is then a few dot products.
    theta = agent.theta()
    a_inv = np.linalg.inv(agent.A) if (explore and agent.alpha > 0) else None
    best, best_value = None, -math.inf
    for plan in plans:
        X = np.array([x for x, _i in plan_features(slices, machines, plan)])
        value = float((X @ theta).sum())
        if a_inv is not None:
            value += agent.alpha * float(np.sqrt(np.einsum("ij,jk,ik->i", X, a_inv, X)).sum())
        if value > best_value:
            best, best_value = plan, value
    return best, plan_features(slices, machines, best)


# ---------------------------------------------------------------------------
# offline check: a simulated cluster with the bench's three machines
# ---------------------------------------------------------------------------
MACHINES = [
    {"id": "fast", "cores": 4.0, "disk_mbps": 5.0, "slots": 2},
    {"id": "medium", "cores": 2.0, "disk_mbps": None, "slots": 4},
    {"id": "slow", "cores": 1.0, "disk_mbps": None, "slots": 6},
]


def S(cpu, disk=0.0, sat=False):
    return {"cpu": cpu, "disk": disk, "saturated": sat}


# Shapes of the three queries at their campaign rates, from what was measured on the cluster
# (per slice: CPU in cores, disk rewrite in MB/s). Approximate on purpose: the point is whether
# the learning generalises across shapes, not to reproduce the cluster.
SCENARIOS = {
    "Q8": [S(1.0, sat=True), S(1.0, sat=True), S(0.8, 3.5), S(0.8, 3.5),
           S(0.05), S(0.05), S(0.0), S(0.0)],
    "Q3": [S(1.0, sat=True), S(1.0, sat=True), S(1.0, sat=True), S(1.0, sat=True),
           S(0.9, 3.5, True), S(0.9, 3.5, True), S(0.0), S(0.0)],
    "Q5-60": [S(0.9, sat=True), S(0.9, sat=True), S(1.0, 1.6, True), S(1.0, 1.6, True),
              S(0.0), S(0.0), S(0.0), S(0.0)],
}


def simulated_throughput(slices, plan):
    """Fraction of the requested rate the plan sustains: the most overloaded resource limits it."""
    worst = 1.0
    for m, machine in enumerate(MACHINES):
        mine = [slices[i] for i, mm in plan.items() if mm == m]
        cpu = sum(s["cpu"] * (1.25 if s["saturated"] else 1.0) for s in mine)
        disk = sum(s["disk"] for s in mine)
        if cpu > 0:
            worst = min(worst, machine["cores"] / cpu)
        if disk > 0 and machine["disk_mbps"]:
            worst = min(worst, machine["disk_mbps"] / disk)
    return min(1.0, worst)


def machine_factor(slices, plan, m):
    """How much of what the plan put on machine m that machine can serve (1 = no overload).

    The simulated counterpart of the per-machine signals the cluster does measure: each
    TaskManager's CPU utilisation (Status.JVM.CPU.Time) and each task's RocksDB stall time.
    """
    machine = MACHINES[m]
    mine = [slices[i] for i, mm in plan.items() if mm == m]
    cpu = sum(s["cpu"] * (1.25 if s["saturated"] else 1.0) for s in mine)
    disk = sum(s["disk"] for s in mine)
    f = 1.0
    if cpu > 0:
        f = min(f, machine["cores"] / cpu)
    if disk > 0 and machine["disk_mbps"]:
        f = min(f, machine["disk_mbps"] / disk)
    return min(1.0, f)


def simulated_telemetry(slices, plan):
    """What the cluster would report per machine for a plan: CPU seconds/s used and whether tasks
    contended for cores, compaction MB/s written and whether they stalled."""
    out = []
    for m, machine in enumerate(MACHINES):
        mine = [slices[i] for i, mm in plan.items() if mm == m]
        cpu_demand = sum(s["cpu"] * (1.25 if s["saturated"] else 1.0) for s in mine)
        disk_demand = sum(s["disk"] for s in mine)
        cap = machine["disk_mbps"]
        out.append({"cpu_used": min(cpu_demand, machine["cores"]),
                    "cpu_contended": cpu_demand > machine["cores"],
                    "disk_written": min(disk_demand, cap) if cap else disk_demand,
                    "disk_stalled": bool(cap) and disk_demand > cap})
    return out


def best_plan(slices):
    best = (-1.0, None)
    for combo in itertools.product(range(len(MACHINES)), repeat=len(slices)):
        if any(combo.count(m) > MACHINES[m]["slots"] for m in range(len(MACHINES))):
            continue
        plan = dict(enumerate(combo))
        best = max(best, (simulated_throughput(slices, plan), plan), key=lambda t: t[0])
    return best


def selftest(episodes=300, seed=1, credit="local", load_range=(1.0, 1.0), learn_machines=False):
    """credit='global': every decision gets the episode's throughput; 'local': each also gets
    how the machine it chose coped (half and half), as the cluster can measure per TaskManager."""
    random.seed(seed)
    np.random.seed(seed)
    agent = LinUCBPlacer(alpha=0.5)
    train = SCENARIOS["Q8"]
    model = MachineModel() if learn_machines else None

    def machines_seen():
        if model is None:
            return MACHINES
        return [model.view(mm["id"], mm["slots"]) for mm in MACHINES]
    for _ in range(episodes):
        # The rate of the episode scales every demand together: training at several rates is
        # what lets the agent tell USING a resource from EXCEEDING it — at one rate the join
        # always writes the same, and "writes to disk" and "overloads the disk" never come apart.
        load = random.uniform(*load_range)
        noisy = [dict(s, cpu=s["cpu"] * load * random.uniform(0.9, 1.1),
                      disk=s["disk"] * load * random.uniform(0.9, 1.1)) for s in train]
        plan, traj = decide(agent, noisy, machines_seen(), explore=True)
        reward = simulated_throughput(noisy, plan) + random.gauss(0, 0.02)
        if model is not None:
            for mm, t in zip(MACHINES, simulated_telemetry(noisy, plan)):
                model.observe(mm["id"], t["cpu_used"], t["cpu_contended"],
                              t["disk_written"], t["disk_stalled"])
        for x, i in traj:
            r = reward if credit == "global" else 0.5 * reward + 0.5 * machine_factor(noisy, plan, plan[i])
            agent.update(x, r)
    if model is not None:
        print("capacidades APRENDIDAS (verdad: fast 4 núcleos / 5 MB/s, medium 2 / sin límite, slow 1 / sin límite):")
        for mm in MACHINES:
            v = model.view(mm["id"], mm["slots"])
            e = model.m[mm["id"]]
            print(f"   {mm['id']:7} núcleos {v['cores']:.2f} (visto hasta {e['cpu_lo']:.2f})   "
                  f"disco {('%.2f MB/s' % v['disk_mbps']) if v['disk_mbps'] else 'sin límite visto'}")
    print(f"pesos aprendidos (entrenado SOLO en Q8, crédito {credit}):")
    for name, w in zip(FEATURES, agent.theta()):
        print(f"   {name:13} {w:+.3f}")
    print()
    for name, slices in SCENARIOS.items():
        plan, _ = decide(agent, slices, machines_seen(), explore=False)
        got = simulated_throughput(slices, plan)
        opt, opt_plan = best_plan(slices)
        where = {}
        for i, m in plan.items():
            where.setdefault(MACHINES[m]["id"], []).append(i)
        print(f"{name:6} agente {got:5.0%}   óptimo {opt:5.0%}   plan del agente: {where}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--episodes", type=int, default=300)
    ap.add_argument("--credit", choices=["global", "local"], default="local")
    ap.add_argument("--learn-machines", action="store_true",
                    help="estimate machine capacities from simulated telemetry instead of declaring them")
    ap.add_argument("--load-range", default="1.0,1.0",
                    help="training rate as a multiple of the campaign rate, 'low,high'")
    args = ap.parse_args()
    if args.selftest:
        lo, hi = (float(v) for v in args.load_range.split(","))
        selftest(args.episodes, credit=args.credit, load_range=(lo, hi),
                 learn_machines=args.learn_machines)
