#!/usr/bin/env python3
"""Simulate plan_bandit.py without a cluster: the bandit's own main() loop, with every call into
Flink, Kubernetes and the clock replaced by a small world model — now with PARALLELISM.

WHY (2026-10-15, k-aware since 2026-10-21). Each change to the bandit's rules is cheap to break and
an hour to test on the cluster. The world is crude on purpose and checks the DECISION LOGIC end to
end, not the physics. A gain or loss here is a hint for the cluster, never a result.

World (Q5-like, CPU 2/1/0.5): the heavy operator has k subtasks, each handling 1/k of the stream
(key-partitioned, no skew). A subtask processes C0 ev/s per core; the subtasks on one machine share
its cores (fast ~1.8 effective — RocksDB's threads take the rest —, medium 1, slow 0.5), each
capped at one core (a subtask is one thread). The job keeps up to k x the slowest subtask's rate.
That reproduces what compare-plans measured on 2026-10-19: split ~4200, both on fast ~3800, both on
medium ~2100. A rescale or a replan stops the job for RESTART_S seconds.

Usage:
  scripts/sim_plan_bandit.py SEED DURATION [plan_bandit args...]   -> "delivered/offered moves scales"
Environment:
  PERIOD   sine period, s (default 3600)     LO/HI  sine range, ev/s (default 2000/6000)
  CKPT     share lost during checkpoints     SHOW   1 to print the bandit's log
  V4       where the fake v4 puts the heavy subtasks: medium (default, the stale table) or fast
  PB_FILE  another plan_bandit.py to load (e.g. an older commit, for comparison)
"""
import contextlib
import io
import math
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if os.environ.get("PB_FILE"):
    import importlib.util
    _spec = importlib.util.spec_from_file_location("plan_bandit", os.environ["PB_FILE"])
    pb = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(pb)
else:
    import plan_bandit as pb                                  # noqa: E402

seed, duration = int(sys.argv[1]), float(sys.argv[2])
rng = random.Random(seed)
C0 = 2100.0
CORES = {"fast": 1.8, "medium": 1.0, "slow": 0.5}
TMS = [("tm-1-slow", 6), ("tm-2-medium", 4), ("tm-3-fast", 2)]
NAMES = {"src": "Source: bid-source", "cnt": "hot-items-count", "sink": "Sink"}
RESTART_S = 20


class W:
    t = 0.0
    epoch = 0
    epoch_t = -1000.0
    k = 2
    # slice key (vid, sub) -> TaskManager; the first placement: counts on medium and slow (LPT-ish)
    place = {("src", 0): "tm-1-slow", ("src", 1): "tm-1-slow", ("cnt", 0): "tm-2-medium",
             ("cnt", 1): "tm-1-slow", ("sink", 0): "tm-1-slow"}
    plans = {}
    pending = None
    offered = delivered = 0.0
    m_off = m_del = 0.0
    sat = 0
    last_sat = 0.0
    last_ratio = 1.0
    phase = rng.uniform(0, 3600)
    noise = rng.gauss(1.0, 0.03)


def slices():
    return ([[("src", 0)], [("src", 1)]] + [[("cnt", i)] for i in range(W.k)] + [[("sink", 0)]])


def rate_at(t):
    lo, hi = float(os.environ.get("LO", 2000)), float(os.environ.get("HI", 6000))
    period = float(os.environ.get("PERIOD", 3600))
    if os.environ.get("WAVE") == "step":       # low for the first half-period of the run, then high
        return lo if t < period / 2 else hi
    return (lo + hi) / 2 + (hi - lo) / 2 * math.sin(2 * math.pi * (t + W.phase) / period)


def subtask_rate(i):
    tm = W.place[("cnt", i)]
    cls = tm.rsplit("-", 1)[-1]
    h = sum(1 for j in range(W.k) if W.place[("cnt", j)] == tm)
    return C0 * min(1.0, CORES[cls] / h)


def cap_now():
    return W.k * min(subtask_rate(i) for i in range(W.k)) * W.noise


def advance(dt):
    end = W.t + dt
    while W.t < end:
        r = rate_at(W.t)
        W.offered += r
        if W.t >= duration - float(os.environ.get("MEASURE_LAST", 1e9)):
            W.m_off += r
            W.m_del += 0.0 if W.t < W.epoch_t + RESTART_S else min(r, cap_now())
        if W.t >= W.epoch_t + RESTART_S:
            ck = float(os.environ.get("CKPT", 0))
            c = cap_now() * (1 - (ck if ck and (W.t % 180) < 40 else 0.0))
            W.sat += r > c
            W.delivered += min(r, c)
        W.t += 1


class FakeTime:
    def time(self):
        return W.t

    def sleep(self, s):
        advance(s)

    def strftime(self, f, *a):
        return "%05d" % int(W.t)


pb.time = FakeTime()
ac, ca, rm = pb.ac, pb.ca, pb.rm
ac.running_job = lambda rest: "job"
ac.epoch_key = lambda rest, jid: W.epoch
ac.epoch_started_at = lambda key, now: W.epoch_t
ac.rest = lambda rest, path: {"vertices": [
    {"id": "src", "name": NAMES["src"], "parallelism": 2},
    {"id": "cnt", "name": NAMES["cnt"], "parallelism": W.k},
    {"id": "sink", "name": NAMES["sink"], "parallelism": 1}]}
ac.graph_roles = lambda d: ({"src"}, {"sink"})
ca.read_layout = lambda node: {"slices": slices(), "tms": TMS}
ca.read_speeds = lambda node: {"tm-1-slow": 1.0, "tm-2-medium": 2.0, "tm-3-fast": 4.0}
ca.generator_metric_ids = lambda *a: {}
ca.SliceSarsa = lambda *a, **k: None


def observe(args, jid, detail, layout):
    t0, off0, del0 = W.t, W.offered, W.delivered
    W.sat = 0
    advance(args.window)
    ratio = min(1.0, (W.delivered - del0) / (W.offered - off0)
                * rng.gauss(1.0, 0.02 * math.sqrt(120 / args.window)))
    W.last_ratio, W.last_sat = ratio, W.sat / args.window
    r = (W.offered - off0) / (W.t - t0)
    per = []
    for m in layout["slices"]:
        vid, i = m[0]
        if vid == "cnt":
            busy = min(1000.0, 1000.0 * (r / W.k) / subtask_rate(i))
            per.append((busy, busy >= 900))
        else:
            per.append((200.0 if vid == "src" else 100.0, False))
    return {"offered": r, "ratio": ratio, "rps": 0,
            "where": {m[0]: W.place[m[0]] for m in layout["slices"]},
            "per_slice": per, "per_slice_disk": [0.0] * len(layout["slices"])}


rm.observe = observe
ca.sink_record_latency_ms = lambda *a: (2500.0 * rng.uniform(0.95, 1.1) if W.last_sat < 0.3
                                        else 12000 + 8000 * (1 - W.last_ratio))


def fake_decide(agent, per_slice, free, *a, **k):
    """A stand-in for v4: heavy subtasks to `V4` (medium = the stale table), the rest to slow."""
    want = 1 if os.environ.get("V4", "medium") == "medium" else 0      # index into (fast, medium, slow)
    room = list(free)
    plan = []
    for cost, sat in per_slice:
        idx = want if cost >= 300 or sat else 2
        while room[idx] <= 0:
            idx = (idx + 1) % 3
        room[idx] -= 1
        plan.append(idx)
    return plan, None


ca.decide = fake_decide


def publish(plans, node):
    W.plans.update(plans)
    return True, None


def apply_plan_or_fallback():
    plan = W.plans.get(len(slices()))
    keys = [m[0] for m in slices()]
    if plan and all(f"{v}:{s}" in plan for v, s in keys):
        W.place = {(v, s): plan[f"{v}:{s}"] for v, s in keys}
        return
    # No plan for this width: keep what exists, new subtasks go to the TM with the most free slots.
    used = {tm: 0 for tm, _ in TMS}
    new = {}
    for key in keys:
        if key in W.place:
            new[key] = W.place[key]
            used[W.place[key]] += 1
    for key in keys:
        if key not in new:
            tm = max(TMS, key=lambda t: t[1] - used[t[0]])[0]
            new[key] = tm
            used[tm] += 1
    W.place = new


def command(node, cmd):
    apply_plan_or_fallback()
    W.epoch += 1
    W.epoch_t = W.t
    return "", None


def put(jid, detail, group, k_new):
    W.k = k_new
    apply_plan_or_fallback()
    W.epoch += 1
    W.epoch_t = W.t + 40          # a rescale takes longer than a replan
    return True, None


ca.publish_plan, ca.node_command = publish, command
pb.rescale_put = put
pb.trigger_checkpoint = lambda jid, timeout=90.0: (3.0, None)
out_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "sim-plan-bandit"
sys.argv = ["plan_bandit.py", "--rate", "6000", "--duration", str(duration),
            "--out-dir", str(out_dir)] + sys.argv[3:]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pb.main()
if os.environ.get("SHOW"):
    print(buf.getvalue())
log = buf.getvalue()
ratio = W.m_del / W.m_off if W.m_off else W.delivered / W.offered
print(f"{ratio:.4f} {log.count('-> MOVER')} {log.count('-> ESCALAR')}")
