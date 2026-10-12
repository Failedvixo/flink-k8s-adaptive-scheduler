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
    "cpu_util",        # CPU the plan puts on the machine / its cores
    "cpu_over",        # max(0, cpu_util - 1): demand the machine cannot serve
    "disk_util",       # disk rewrite the plan puts on the machine / its disk bandwidth
    "disk_over",       # max(0, disk_util - 1)
    "sat_util",        # saturated slices on the machine / its cores: their measured CPU
                       # under-counts what they need
    "slot_util",       # slots used / slots
]
# ONE TERM PER MACHINE, NOT PER SLICE (2026-10-11). Summing a value per SLICE made an overloaded
# machine count once for every slice on it, light ones included: in the simulated Q5 the optimal
# plan put a sink beside an overloaded source on the one-core machine and paid twice, while the
# agent's plan overloaded medium with exactly two slices and paid twice too — and won. What limits
# the job is the MACHINE, so a plan's value is the sum of one estimate per machine it uses.
UNLIMITED_DISK_MBPS = 200.0     # a machine without a cap: its disk is never the constraint here


UTIL_CAP = 2.0


def _capped(util):
    """Utilisation capped at UTIL_CAP (twice the capacity). EVERY feature is bounded.

    WHY (2026-10-11/12, two trainings on the cluster). The reward cannot go below -1, but a raw
    utilisation can go anywhere: a join's 21 MB/s on fast's learned 0.56 MB/s disk is disk_util 38.
    A linear model fitted where the data are (utilisations around 0-2) extrapolates such a value
    into nonsense, in either direction:
      * first training: disk_over = 19 made LinUCB's bonus (which grows with the size of the
        feature vector) outweigh the penalty, so the agent explored the most absurd plan;
      * second, with only the overload terms capped: disk_util kept a small POSITIVE weight
        (+0.05; high disk use on medium came with good epochs), and x38 it turned the worst plan
        into the greedy favourite (+0.84). Both joins went to fast in 15 of 30 epochs.
    Past twice the capacity a machine is simply overwhelmed; the features say so and stop there.
    """
    return min(UTIL_CAP, util)


def machine_features(load, machine):
    """Features of a machine carrying `load` = {'cpu', 'disk', 'slots', 'sat'} from one plan."""
    cores = machine["cores"]
    disk = machine.get("disk_mbps") or UNLIMITED_DISK_MBPS
    cpu_util = _capped(load["cpu"] / cores)
    disk_util = _capped(load["disk"] / disk)
    return np.array([
        1.0,
        cpu_util,
        max(0.0, cpu_util - 1.0),
        disk_util,
        max(0.0, disk_util - 1.0),
        _capped(load["sat"] / cores),
        load["slots"] / machine["slots"],
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
        e["cpu_lo"] = max(e["cpu_lo"], cpu_used)
        if cpu_contended:
            e["cpu_hi"] = cpu_used if e["cpu_hi"] is None else 0.7 * e["cpu_hi"] + 0.3 * cpu_used
        if e["cpu_hi"] is not None:
            # A machine seen using X cores has at least X: a low "contended" reading (blocked
            # rather than computing) must not cap it below what it has already delivered.
            e["cpu_hi"] = max(e["cpu_hi"], e["cpu_lo"])
        if disk_stalled and disk_written > 0:
            # ONE STALL IS AN ANECDOTE (2026-10-12). Compaction comes in bursts, and the host's
            # disk is shared: a single stalled epoch had capped medium (no disk limit at all) at
            # 13 MB/s, after which two joins "did not fit" and the agent split them onto slow —
            # ~70% delivered against ~90% for both on medium. The cap is set from the second
            # stalled epoch on, at the lowest throughput a stall was seen at.
            e["disk_stalls"] = e.get("disk_stalls", 0) + 1
            e["disk_stall_at"] = min(e.get("disk_stall_at") or disk_written, disk_written)
            if e["disk_stalls"] >= 2:
                e["disk_hi"] = e["disk_stall_at"]
        elif not disk_stalled:
            e["disk_lo"] = max(e["disk_lo"], disk_written)
        if e["disk_hi"] is not None:
            # Same rule as the CPU: a disk seen absorbing X MB/s without stalling can take X. A
            # single stall at low throughput (a compaction burst, the host's disk busy with
            # another machine) had capped medium at 1.85 MB/s after it had written 13 cleanly.
            e["disk_hi"] = max(e["disk_hi"], e["disk_lo"])

    def view(self, mid, slots):
        """The machine as the features see it: estimated cores and disk, plus its slots."""
        e = self._entry(mid)
        cores = e["cpu_hi"] if e["cpu_hi"] is not None else max(self.OPTIMISTIC_CORES, e["cpu_lo"])
        disk = e["disk_hi"]                        # None = no stall seen: the disk never bound
        return {"id": mid, "cores": max(cores, 0.1), "disk_mbps": disk, "slots": slots}

    def to_json(self):
        return self.m


class OperatorProfile:
    """What each kind of slice DEMANDS per unit of offered rate, remembered across epochs.

    WHY (2026-10-12, replaying the two cluster trainings). A slice's demand is measured where it
    ran, and an operator starved there measures cheap: both joins on fast, stalled on its disk,
    read 0.4 MB/s and 0.7 cores, against 10-13 MB/s on medium at a similar rate. Deciding with that
    reading, fast looked big enough for a join again, so the policy alternated: medium (good),
    fast (20-50% delivered), medium, fast... the same feedback loop as project memory "el agente
    mandó el join a la lenta". The cure is to characterise the OPERATOR, not the last reading:
    keep, per slice kind, its demand per 1000 ev/s offered, averaged over the epochs in which its
    machine was neither throttled nor stalled (see observe).
    Both learning and deciding use this demand, so a plan that starved a join is recorded as what
    it was: a machine asked for 20 MB/s of disk, not one that wrote 0.4.
    """

    SMOOTH = 0.3                       # weight of a new clean reading

    def __init__(self, stored=None):
        self.p = dict(stored or {})

    def observe(self, key, cpu, disk, offered, starved=False):
        """Fold one epoch's reading in. A STARVED reading (its machine was throttled or stalled)
        can only raise the profile: it shows a floor, never the operator's real demand.

        Revised the same day: the first version kept a decaying MAXIMUM of every reading, which
        a starved reading cannot pull down — but compaction is bursty, so the maximum also
        carried every spike (13 MB/s per join at 40 000 ev/s, against ~6.5 measured with both
        joins on medium delivering 90%), and two joins then looked like too much for medium.
        Clean readings are averaged instead; starved ones are excluded from the average.
        """
        if offered <= 0:
            return
        c, d = cpu * 1000.0 / offered, disk * 1000.0 / offered
        e = self.p.get(key)
        if e is None:
            self.p[key] = {"cpu": c, "disk": d, "clean": not starved}
            return
        if starved:
            e["cpu"], e["disk"] = max(e["cpu"], c), max(e["disk"], d)
        elif not e.get("clean", True):
            # The first clean reading replaces whatever starved readings left.
            e.update(cpu=c, disk=d, clean=True)
        else:
            e["cpu"] += self.SMOOTH * (c - e["cpu"])
            e["disk"] += self.SMOOTH * (d - e["disk"])

    def demand(self, key, offered, measured):
        """{'cpu', 'disk'} expected at `offered`; the measurement itself if never profiled."""
        e = self.p.get(key)
        if not e:
            return {"cpu": measured["cpu"], "disk": measured["disk"]}
        return {"cpu": e["cpu"] * offered / 1000.0, "disk": e["disk"] * offered / 1000.0}

    def to_json(self):
        return self.p


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
        """Ridge solution with every LOAD weight constrained to be <= 0 (the bias is free).

        MONOTONE, BECAUSE MORE LOAD NEVER HELPS (2026-10-12, third cluster training). The free
        fit learned cpu_over = +0.12: the earlier data had no CPU capacities (every machine at the
        optimistic 8 cores, cpu_over always 0) and the few new samples, collinear with cpu_util
        and sat_util, tipped it positive — so two joins on slow (1 core, cpu_util 2) scored better
        than spreading them, and the agent kept doing it at 53-63% delivered. "A machine carrying
        more, relative to what it has, is not better off" is a property of the reward, not a rule
        about where operators go; with it the regression can only learn HOW MUCH each overload
        costs. Solved as least squares on the Cholesky factor of the normal equations.
        """
        free = np.linalg.solve(self.A, self.b)
        if (free[1:] <= 0).all():
            return free
        from scipy.optimize import lsq_linear
        L = np.linalg.cholesky(self.A)
        target = np.linalg.solve(L, self.b)
        lower = np.full(len(free), -np.inf)
        upper = np.zeros(len(free))
        upper[0] = np.inf
        return lsq_linear(L.T, target, bounds=(lower, upper)).x

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
    """[(features, machine index)] for every machine the plan {slice: machine} uses."""
    loads = [{"cpu": 0.0, "disk": 0.0, "slots": 0, "sat": 0} for _ in machines]
    for i, m in plan.items():
        loads[m]["cpu"] += slices[i]["cpu"]
        loads[m]["disk"] += slices[i]["disk"]
        loads[m]["slots"] += 1
        loads[m]["sat"] += 1 if slices[i]["saturated"] else 0
    return [(machine_features(loads[m], machines[m]), m)
            for m in range(len(machines)) if loads[m]["slots"] > 0]


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

    Returns (plan, trajectory [(features, machine index)]) or (None, []) if nothing fits.
    """
    plans = feasible_plans(slices, machines)
    if not plans:
        return None, []
    # The regression is solved ONCE per decision; every plan is then a few dot products.
    theta = agent.theta()
    a_inv = np.linalg.inv(agent.A) if (explore and agent.alpha > 0) else None
    best, best_value = None, -math.inf
    for plan in plans:
        X = np.array([x for x, _m in plan_features(slices, machines, plan)])
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


def observed_slices(slices, plan, estimate_saturated):
    """What the agent would MEASURE for each slice after running `plan`: a saturated slice shows
    at most what its machine gave it. With estimate_saturated, its demand is reconstructed as
    measured x (offered / delivered), as agent_v5_loop.py does on the cluster."""
    ratio = simulated_throughput(slices, plan)
    out = []
    for s in slices:
        seen = dict(s)
        if s["saturated"]:
            # Its true demand is 1.25x nominal (as in simulated_throughput); it shows only the
            # share the bottleneck let through.
            seen["cpu"] = s["cpu"] * 1.25 * ratio
            if estimate_saturated and ratio > 0:
                seen["cpu"] /= ratio
        out.append(seen)
    return out


def selftest(episodes=300, seed=1, credit="local", load_range=(1.0, 1.0), learn_machines=False,
             estimate_saturated=False, train_on="Q8"):
    """credit='global': every decision gets the episode's throughput; 'local': each also gets
    how the machine it chose coped (half and half), as the cluster can measure per TaskManager."""
    random.seed(seed)
    np.random.seed(seed)
    agent = LinUCBPlacer(alpha=0.5)
    train = SCENARIOS[train_on]
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
        for x, m in traj:
            r = reward if credit == "global" else 0.5 * reward + 0.5 * machine_factor(noisy, plan, m)
            agent.update(x, r)
    if model is not None:
        print("capacidades APRENDIDAS (verdad: fast 4 núcleos / 5 MB/s, medium 2 / sin límite, slow 1 / sin límite):")
        for mm in MACHINES:
            v = model.view(mm["id"], mm["slots"])
            e = model.m[mm["id"]]
            print(f"   {mm['id']:7} núcleos {v['cores']:.2f} (visto hasta {e['cpu_lo']:.2f})   "
                  f"disco {('%.2f MB/s' % v['disk_mbps']) if v['disk_mbps'] else 'sin límite visto'}")
    print(f"pesos aprendidos (entrenado SOLO en {train_on}, crédito {credit}):")
    for name, w in zip(FEATURES, agent.theta()):
        print(f"   {name:13} {w:+.3f}")
    print()
    for name, slices in SCENARIOS.items():
        # Decide from what would be MEASURED in the query, as on the cluster: run a first plan
        # (the fallback), measure, then decide.
        first, _ = decide(agent, slices, machines_seen(), explore=False)
        measured = observed_slices(slices, first, estimate_saturated)
        plan, _ = decide(agent, measured, machines_seen(), explore=False)
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
    ap.add_argument("--train-on", default="Q8", choices=list(SCENARIOS))
    ap.add_argument("--estimate-saturated", action="store_true",
                    help="reconstruct a saturated slice's demand as measured x offered/delivered")
    ap.add_argument("--learn-machines", action="store_true",
                    help="estimate machine capacities from simulated telemetry instead of declaring them")
    ap.add_argument("--load-range", default="1.0,1.0",
                    help="training rate as a multiple of the campaign rate, 'low,high'")
    args = ap.parse_args()
    if args.selftest:
        lo, hi = (float(v) for v in args.load_range.split(","))
        selftest(args.episodes, credit=args.credit, load_range=(lo, hi),
                 learn_machines=args.learn_machines, estimate_saturated=args.estimate_saturated,
                 train_on=args.train_on)
