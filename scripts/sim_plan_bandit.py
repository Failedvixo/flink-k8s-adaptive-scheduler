#!/usr/bin/env python3
"""Simulate plan_bandit.py on Q5-60 without a cluster: the bandit's own main() loop, with every
call into Flink, Kubernetes and the clock replaced by a small world model.

WHY (2026-10-15). Each change to the bandit's rules is cheap to break and an hour to test on the
cluster. The world here is crude on purpose — capacity per placement of the two counts, measured
on the cluster on 2026-10-15, a sine rate, a latency that jumps when the arm cannot keep up — so
it checks the decision logic end to end and compares variants, NOT the physics: it has no CPU
throttling, no replay after a restart and no clock skew unless asked, and a gain or loss here is
a hint to be confirmed on the cluster, never a result.

Usage:
  scripts/sim_plan_bandit.py SEED DURATION [plan_bandit args...]   -> "delivered/offered moves"
Environment:
  PERIOD   sine period, s (default 3600)          CKPT  share lost during checkpoints (0)
  LAG      s the delivered rate trails (0)         SHOW  1 to print the bandit's log
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
# Capacity (ev/s) by where the two counts sit; measured 20261015-170749 (fast×2 from night 1).
CAP = {("fast", "medium"): 4640, ("medium", "medium"): 3740, ("fast", "slow"): 3585,
       ("medium", "slow"): 3346, ("slow", "slow"): 2186, ("fast", "fast"): 4500}
TMS = [("tm-1-slow", 6), ("tm-2-medium", 4), ("tm-3-fast", 2)]
SLICES = [[("src", 0)], [("src", 1)], [("cnt", 0)], [("cnt", 1)], [("sink", 0)]]
NAMES = {"src": "Source: bid-source", "cnt": "hot-items-count", "sink": "Sink"}


class W:
    t = 0.0
    epoch = 0
    epoch_t = 0.0
    place = ["tm-1-slow", "tm-1-slow", "tm-2-medium", "tm-1-slow", "tm-1-slow"]
    pending = None
    offered = delivered = 0.0
    sat = 0
    last_sat = 0.0
    last_ratio = 1.0
    phase = rng.uniform(0, 3600)
    noise = {k: rng.gauss(1.0, 0.04) for k in CAP}         # per-run bias of each arm


def rate_at(t):
    return 4000 + 2000 * math.sin(2 * math.pi * (t + W.phase) / float(os.environ.get("PERIOD", 3600)))


def cap_now():
    key = tuple(sorted(tm.rsplit("-", 1)[-1] for tm in W.place[2:4]))
    return CAP[key] * W.noise[key]


def advance(dt):
    end = W.t + dt
    while W.t < end:
        r = rate_at(W.t)
        W.offered += r
        if W.t >= W.epoch_t + 2:                             # ~2 s with tasks down after a replan
            ck = float(os.environ.get("CKPT", 0))
            c = cap_now() * (1 - (ck if ck and (W.t % 180) < 40 else 0.0))
            W.sat += r > c
            W.delivered += min(rate_at(W.t - float(os.environ.get("LAG", 0))), c)
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
ac.rest = lambda rest, path: {"vertices": [{"id": v, "name": n} for v, n in NAMES.items()]}
ac.graph_roles = lambda d: ({"src"}, {"sink"})
ca.read_layout = lambda node: {"slices": SLICES, "tms": TMS}
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
    sat = ratio < 0.97
    return {"offered": (W.offered - off0) / (W.t - t0), "ratio": ratio, "rps": 0,
            "where": {m[0]: W.place[i] for i, m in enumerate(SLICES)},
            "per_slice": [(200, False), (200, False), (1000, sat), (900, sat), (100, False)],
            "per_slice_disk": None}


rm.observe = observe
ca.sink_record_latency_ms = lambda *a: (2500.0 * rng.uniform(0.95, 1.1) if W.last_sat < 0.3
                                        else 12000 + 8000 * (1 - W.last_ratio))
ca.decide = lambda agent, *a, **k: ([0, 0, 1, 1, 0], None)   # v4: both counts on medium


def publish(plans, node):
    W.pending = [plans[len(SLICES)][f"{v}:{s}"] for m in SLICES for v, s in m]
    return True, None


def command(node, cmd):
    W.place = W.pending
    W.epoch += 1
    W.epoch_t = W.t
    return "", None


ca.publish_plan, ca.node_command = publish, command
if hasattr(pb, "trigger_checkpoint"):
    pb.trigger_checkpoint = lambda jid, timeout=90.0: (3.0, None)
out_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "sim-plan-bandit"
sys.argv = ["plan_bandit.py", "--rate", "6000", "--duration", str(duration),
            "--out-dir", str(out_dir)] + sys.argv[3:]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pb.main()
if os.environ.get("SHOW"):
    print(buf.getvalue())
print(f"{W.delivered / W.offered:.4f} {buf.getvalue().count('-> MOVER')}")
