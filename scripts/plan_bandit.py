#!/usr/bin/env python3
"""Part B monitor, v2: an online bandit over placements of the HEAVY operators, with SARSA (v4)
proposing and the bandit verifying by measurement.

WHY v2 (2026-10-13, after the first night on Q5-60). v1 learned the right ORDER of placements by
measurement alone (counts split > on fast > v4's choice on medium > anything on slow), the same
order the controlled comparison found — but never converged: its arms were whole placements, so
moving a sink made a "new" arm with an optimism bonus, there was always something unmeasured, and
it kept returning to counts on the one-core machine (74% delivered on average at 6000 ev/s). And it
compared rewards per rate band, which a gradually changing rate (a sine, Vicente's preferred,
realistic profile) confounds with the placement. v2 changes both:

  ARMS      how many subtasks of each HEAVY operator sit on each machine CLASS — e.g. Q5 counts
            {fast: 1, medium: 1}. Light slices are placed by a fixed rule in the room left, so they
            never create arms; same-kind subtasks are interchangeable. Heavy kinds are those whose
            peak busy time seen so far is high (they never stop being heavy at a low rate).
  CANDIDATES each decision considers only a NEIGHBOURHOOD, never every arm:
              - stay;
              - move ONE heavy subtask to another machine class (one candidate per kind × class);
              - SARSA's (v4's) proposal for what it sees now;
              - the best arm measured so far.
            So the cost per decision grows linearly with the heavy subtasks, not combinatorially.
  WHAT IS LEARNED, per arm that has RUN (candidates never get rewards, they are looked up):
              - a capacity lower bound: the highest offered rate it delivered in full;
              - a capacity estimate: what it delivered when it could NOT keep up;
              - latency points (offered rate, p50).
            None of these depends on the rate it was measured at, so a measurement at 5100 ev/s
            informs the decision at 5900: the arm's expected delivery at rate r is min(1, cap/r).
  DECISION  predicted reward at the CURRENT rate = delivery − w·ln(latency / 1 s); an arm never
            run gets the running arm's value plus an optimism bonus (more if SARSA proposes it);
            an arm run but never saturated above the current rate gets a smaller bonus (its limit
            is unknown); every move pays a small fixed cost. Old evidence fades (forgetting), so a
            machine that starts to underperform is noticed.
  PENALTY   (2026-10-15, the professor's suggestion) each move is judged at the next decision
            against not having moved: the arm left behind, predicted at the rate measured now. A
            move that did worse leaves its loss on that KIND of move (operator, from, to), charged
            to every later candidate that repeats it, with forgetting. And an arm never run is
            capped by a measured arm with the same subtasks on strictly better machines
            (--dominance, slow<medium here), so optimism cannot override what is known.
  SCALING   if even the best known capacity is below the offered rate, no placement suffices: the
            log says so ("replanificar no basta — habría que escalar"), for the coordinated
            autoscaler that comes next.

Modes, so a comparison measures every arm with the same loop and the same windows:
  --mode stock   measure only (run with the fork's STOCK arm published: Flink's own placement)
  --mode v4      apply v4's plan once at the start, then measure only (no monitor)
  --mode bandit  v4's plan at the start, then the bandit

Learning is per run (the bandit is online); --model only saves it for inspection, or warm-starts a
rerun of the same query on the same cluster with --warm.
"""
import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import characterizer_agent as ca                               # noqa: E402
import replan_monitor as rm                                    # noqa: E402

REPLAN_FILE = "/var/thesis/replan"
FULL = 0.97                     # delivered/offered at or above which an arm "kept up"


def machine_class(tm):
    """tm-3-fast -> fast. The cluster's TaskManager ids end in their speed class."""
    return tm.rsplit("-", 1)[-1]


# ---------------------------------------------------------------------------
# arms
# ---------------------------------------------------------------------------
def arm_of(placement, kinds, heavy):
    """{heavy kind: {class: n}} as a canonical JSON string."""
    arm = {}
    for kind, tm in zip(kinds, placement):
        if kind in heavy:
            cls = arm.setdefault(kind, {})
            cls[machine_class(tm)] = cls.get(machine_class(tm), 0) + 1
    return json.dumps({k: dict(sorted(v.items())) for k, v in sorted(arm.items())}, sort_keys=True)


def realise(arm, running, kinds, heavy, capacity, speeds):
    """A per-slice placement that has `arm`'s heavy layout, moving as little as possible.

    Heavy subtasks keep their machine when the arm still wants that class there; the rest go to a
    TaskManager of the wanted class with room. Light slices stay where they are unless that machine
    is now over its slots; then they move to the SLOWEST machine with room (the fixed rule for
    light work: keep the fast cores for what needs them). None if the arm does not fit."""
    want = json.loads(arm)
    out = list(running)
    tms_of = {}
    for tm in capacity:
        tms_of.setdefault(machine_class(tm), []).append(tm)
    for kind in want:
        idx = [i for i, k in enumerate(kinds) if k == kind]
        need = dict(want[kind])
        keep, move = [], []
        for i in idx:
            cls = machine_class(running[i]) if running[i] else None
            if cls is not None and need.get(cls, 0) > 0:
                need[cls] -= 1
                keep.append(i)
            else:
                move.append(i)
        targets = [cls for cls, n in need.items() for _ in range(n)]
        if len(targets) != len(move):
            return None
        for i, cls in zip(move, targets):
            if cls not in tms_of:
                return None
            out[i] = ("class", cls)
    used = {tm: 0 for tm in capacity}
    for i, tm in enumerate(out):
        if out[i] is None:
            # A subtask that does not exist yet (the plan for a parallelism about to be set): a
            # light one goes where there is room, after the heavy ones below.
            continue
        if isinstance(tm, str) and kinds[i] in heavy:
            used[tm] += 1
    # Heavy subtasks that move: the TaskManager of their class with most room.
    for i, tm in enumerate(out):
        if isinstance(tm, tuple):
            cands = [t for t in tms_of[tm[1]] if used[t] < capacity[t]]
            if not cands:
                return None
            best = max(cands, key=lambda t: capacity[t] - used[t])
            out[i] = best
            used[best] += 1
    # Light slices: stay if their machine still has room, else go to the slowest with room.
    light = [i for i in range(len(out)) if kinds[i] not in heavy]
    for i in light:
        if out[i] is not None and used[out[i]] < capacity[out[i]]:
            used[out[i]] += 1
        else:
            out[i] = None
    for i in light:
        if out[i] is None:
            cands = sorted((t for t in capacity if used[t] < capacity[t]),
                           key=lambda t: (speeds.get(t, 1.0), t))
            if not cands:
                return None
            out[i] = cands[0]
            used[cands[0]] += 1
    return out


def neighbours(arm, total_slots):
    """Arms one move away: one subtask of one heavy kind to another class (with slots in it)."""
    want = json.loads(arm)
    out = []
    for kind, layout in want.items():
        for src, n in layout.items():
            if n <= 0:
                continue
            for dst in total_slots:
                if dst == src:
                    continue
                new = {k: dict(v) for k, v in want.items()}
                new[kind][src] -= 1
                if new[kind][src] == 0:
                    del new[kind][src]
                new[kind][dst] = new[kind].get(dst, 0) + 1
                heavy_on_dst = sum(v.get(dst, 0) for v in new.values())
                if heavy_on_dst <= total_slots[dst]:
                    out.append((json.dumps({k: dict(sorted(v.items())) for k, v in sorted(new.items())},
                                           sort_keys=True), f"mover un {kind[:24]} de {src} a {dst}"))
    return out


# ---------------------------------------------------------------------------
# what is known about an arm
# ---------------------------------------------------------------------------
class ArmStats:
    """Capacity bounds and latency points of one arm, with forgetting."""

    def __init__(self, d=None):
        d = d or {}
        self.lo = d.get("lo", 0.0)            # highest rate delivered in full
        self.cap = d.get("cap")               # estimated capacity when it could not keep up
        self.cap_w = d.get("cap_w", 0.0)      # weight of the capacity estimate
        self.lat = d.get("lat", [])           # [[rate, latency_ms, weight], ...]
        self.n = d.get("n", 0)
        self.tight = d.get("tight", False)    # its best full rate was held with a machine at its limit

    def to_json(self):
        return {"lo": self.lo, "cap": self.cap, "cap_w": self.cap_w, "lat": self.lat, "n": self.n,
                "tight": self.tight}

    def observe(self, offered, delivered_ratio, latency_ms, smooth, at_limit=False):
        self.n += 1
        if delivered_ratio >= FULL:
            if offered > self.lo:
                self.tight = at_limit
            elif offered == self.lo:
                self.tight = self.tight or at_limit
            self.lo = max(self.lo, offered)
            if self.cap is not None and self.cap < offered:
                self.cap = None                  # it kept up above the old estimate: stale
                self.cap_w = 0.0
        else:
            delivered = delivered_ratio * offered
            self.cap = delivered if self.cap is None else (1 - smooth) * self.cap + smooth * delivered
            self.cap_w = min(1.0, self.cap_w + smooth)
            self.lo = min(self.lo, delivered)    # it did not keep up there: the old bound is stale
        if latency_ms is not None and latency_ms >= 0:
            self.lat.append([offered, float(latency_ms), 1.0])
            self.lat = self.lat[-40:]

    def fade(self, factor):
        self.cap_w *= factor
        for p in self.lat:
            p[2] *= factor

    def delivery(self, rate, headroom):
        """(expected delivered/offered at `rate`, known?)

        Above the highest rate it has kept up with and with no capacity measured, the arm is
        assumed to hold `headroom` more than that bound, not anything at all (2026-10-15, one-hour
        test): arms measured only in the sine's valley all read "100% at the peak", and the bandit
        tried two counts on fast+slow at 5778 ev/s (62.5%) — known only up to 3440 — when
        fast+medium was known up to 4574. Exploration moves to where it is informative: an arm is
        tried at rates a little above what it has shown, on the way up."""
        if self.cap is not None:
            return min(1.0, self.cap / rate), True
        if rate <= self.lo:
            return 1.0, True
        if self.lo <= 0:
            return 1.0, False
        # No headroom if a machine was at its CPU limit when it held that rate (2026-10-17, second
        # campaign): fast+slow "held >= 3713" with slow throttled 92%, was credited 25% more, and
        # delivered 59% at 4910.
        return min(1.0, self.lo * (1.0 + (0.0 if self.tight else headroom)) / rate), False

    def latency(self, rate):
        """Latency expected at `rate`: weighted by closeness in rate and by recency."""
        pts = [(r, lat, w) for r, lat, w in self.lat if w > 0.05]
        if not pts:
            return None
        weights = [w / (1.0 + abs(r - rate) / max(rate, 1.0) * 10.0) for r, _l, w in pts]
        return sum(lat * w for (_r, lat, _w), w in zip(pts, weights)) / sum(weights)


def move_keys(src_arm, dst_arm):
    """The moves that turn one arm into another, as "kind|from|to" — one per subtask that changes
    class. A neighbour differs by one move; SARSA's proposal or the best measured may differ by
    several, paired in a fixed order (which subtask went where does not matter: same-kind subtasks
    are interchangeable)."""
    a, b = json.loads(src_arm), json.loads(dst_arm)
    keys = []
    for kind in sorted(set(a) | set(b)):
        la, lb = a.get(kind, {}), b.get(kind, {})
        out = [c for c in sorted(la) for _ in range(max(0, la[c] - lb.get(c, 0)))]
        into = [c for c in sorted(lb) for _ in range(max(0, lb[c] - la.get(c, 0)))]
        keys += [f"{kind}|{s}|{d}" for s, d in zip(out, into)]
    return keys


def dominated_by(arm, other, order):
    """True if `other` is `arm` with one or more heavy subtasks on a class that is better in EVERY
    resource: for each `worse<better` pair in `order`, `other` may have moved subtasks from `worse` to
    `better`, and nothing else differs. Only pairs that hold on every dimension belong in `order`:
    on this cluster slow<medium (fewer cores, same disk), NOT medium<fast (fast's disk is capped)."""
    a, b = json.loads(arm), json.loads(other)
    if a == b or set(a) != set(b):
        return False
    for kind in a:
        la, lb = dict(a[kind]), dict(b[kind])
        for worse, better in order:
            shift = la.get(worse, 0) - lb.get(worse, 0)
            if shift < 0:
                return False
            la[worse] = la.get(worse, 0) - shift
            la[better] = la.get(better, 0) + shift
        if {c: n for c, n in la.items() if n} != {c: n for c, n in lb.items() if n}:
            return False
    return True


# ---------------------------------------------------------------------------
# coordinated scaling (2026-10-21)
# ---------------------------------------------------------------------------
def future_slices(slices, group, k_new):
    """The slice list after the operators in `group` (vertex ids) go to parallelism k_new, the
    rest unchanged. Under PER_STAGE sharing a slice is one subtask index of one sharing group, so
    the future layout is predictable: the group's slices for indices >= k_new disappear, and new
    ones [(vid, i) for vid in group] appear for the indices it gains."""
    out = [m for m in slices if not (m[0][0] in group and m[0][1] >= k_new)]
    have = {m[0][1] for m in slices if m[0][0] in group}
    members = sorted(group)
    for i in range(k_new):
        if i not in have:
            out.append([(vid, i) for vid in members])
    return out


def rescale_put(jid, detail, group, k_new):
    """PUT resource-requirements: the group's vertices to k_new, every other vertex as it is (a
    partial payload is rejected outright). (ok, error)."""
    reqs = {}
    for v in detail.get("vertices", []):
        par = k_new if v["id"] in group else max(1, int(v.get("parallelism", 1)))
        reqs[v["id"]] = {"parallelism": {"lowerBound": par, "upperBound": par}}
    if not ac._JM_POD:
        return False, "sin --jm-pod"
    try:
        out = subprocess.run(["kubectl", "exec", "-n", ac._NAMESPACE, ac._JM_POD, "--", "curl", "-s",
                              "-o", "/dev/null", "-w", "%{http_code}", "-m", "20", "-X", "PUT",
                              f"http://localhost:8081/jobs/{jid}/resource-requirements",
                              "-H", "Content-Type: application/json", "-d", json.dumps(reqs)],
                             capture_output=True, text=True, timeout=40)
        code = out.stdout.strip()
        return code in ("200", "202"), (None if code in ("200", "202") else f"HTTP {code}")
    except subprocess.SubprocessError as e:
        return False, str(e)


def trigger_checkpoint(jid, timeout=90.0):
    """Ask Flink for a checkpoint NOW (REST, Flink >= 1.19) and wait for it. (seconds, error).

    WHY (2026-10-16, first monitoring campaign). A replan restarts from the latest completed
    checkpoint, and the periodic ones are 180 s apart: every move replayed up to three minutes of
    events. The replayed results came out late (latency spikes of up to 510 s, 26-53 ledger samples
    over 20 s per BANDIT run against 5-8 for V4: +250% p50 over the run, p=0.008), and the sources,
    more than MAX_EVENT_AGE behind, dropped events in the first window — which once wrote off the
    best arm (fast×2 read as 2553 ev/s, true ~4860). With the request armed for "the next
    checkpoint" and a checkpoint taken on the spot, the restart replays seconds, not minutes."""
    def call(*curl):
        out = subprocess.run(["kubectl", "exec", "-n", ac._NAMESPACE, ac._JM_POD, "--", "curl", "-s",
                              "-m", "20", *curl], capture_output=True, text=True, timeout=40)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    if not ac._JM_POD:
        return None, "sin --jm-pod"
    started = time.time()
    try:
        got = call("-X", "POST", "-H", "Content-Type: application/json", "-d", "{}",
                   f"http://localhost:8081/jobs/{jid}/checkpoints")
        rid = (got or {}).get("request-id")
        if not rid:
            return None, f"POST checkpoints: {got}"
        while time.time() - started < timeout:
            st = call(f"http://localhost:8081/jobs/{jid}/checkpoints/{rid}") or {}
            if st.get("status", {}).get("id") == "COMPLETED":
                failure = (st.get("operation") or {}).get("failure-cause")
                return time.time() - started, (str(failure)[:200] if failure else None)
            time.sleep(1.0)
    except (subprocess.SubprocessError, ValueError):
        pass
    return None, "el checkpoint no terminó a tiempo"


def source_gauge(rest, jid, detail, suffix, agg="sum"):
    """A gauge of the source subtasks, aggregated over them; None if no source exposes it."""
    total = None
    for vid, mid in (ca.generator_metric_ids(rest, jid, detail, suffix) or {}).items():
        for m in ac.rest(rest, f"/jobs/{jid}/vertices/{vid}/subtasks/metrics?get={mid}&agg={agg}") or []:
            try:
                v = float(m[agg])
            except (KeyError, TypeError, ValueError):
                continue
            total = v if total is None else (total + v if agg == "sum" else max(total, v))
    return total


def reward(delivery, latency_ms, weight):
    lat = None if latency_ms is None else max(1.0, latency_ms)
    return delivery - (weight * math.log(lat / 1000.0) if lat else 0.0)


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["stock", "v4", "bandit"], default="bandit")
    ap.add_argument("--qtable", default="results/characterizer/q8-v4-20261008.json",
                    help="v4's table; 'none' = an EMPTY table, v4 with only its prior (the untrained control)")
    # THE BASE PLANNER (2026-10-21). v4 gives the first plan and a candidate every cycle; "stock"
    # gives neither — the run starts from Flink's own placement and the bandit explores from there,
    # the control showing the bandit works over any base planner (STOCK + bandit).
    ap.add_argument("--base", choices=["v4", "stock"], default="v4")
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--dist", default="CONSTANT")
    ap.add_argument("--duration", type=float, default=3600.0)
    # 90 s, not 60 (2026-10-15, third cluster test): the sources' rate meter averages the LAST
    # 60 s, so the first sample of a window opened 60 s after a restart still covers the restart
    # and the replay from the checkpoint — fast+medium "delivered" 98% at 5717 ev/s against a
    # capacity of ~4985. 90 s = the meter's 60 s + ~15-30 s for the sources to recover.
    ap.add_argument("--warmup", type=float, default=90.0, help="settle after a restart")
    ap.add_argument("--window", type=float, default=60.0)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--optimism", type=float, default=0.10, help="bonus of an arm never run")
    ap.add_argument("--sarsa-bonus", type=float, default=0.03,
                    help="extra for SARSA's proposal when never run: try it first")
    ap.add_argument("--unknown-limit", type=float, default=0.03,
                    help="bonus of an arm run but never seen at its limit above this rate")
    ap.add_argument("--move-cost", type=float, default=0.01)
    ap.add_argument("--headroom", type=float, default=0.25,
                    help="an arm never saturated is assumed to hold this much above its best full rate")
    ap.add_argument("--penalty-weight", type=float, default=1.0,
                    help="weight of what a move of the same kind lost before (0 disables)")
    ap.add_argument("--penalty-margin", type=float, default=0.03,
                    help="loss below which a move is not counted as having worked worse (noise)")
    ap.add_argument("--dominance", default="slow<medium",
                    help="class pairs worse<better on EVERY resource, comma-separated ('' disables)")
    ap.add_argument("--latency-weight", type=float, default=0.1)
    ap.add_argument("--replan-at", choices=["checkpoint", "now"], default="checkpoint",
                    help="restart from a checkpoint taken on the spot, or from the latest periodic one")
    ap.add_argument("--limit-throttle", type=float, default=0.85,
                    help="throttled share at which a machine counts as at its limit (no headroom)")
    ap.add_argument("--busy-machine", type=float, default=0.5,
                    help="share of throttled CPU periods above which no heavy subtask moves INTO a machine")
    # THE COMMON AUTOSCALER (2026-10-21, the "with rescaling" condition). The same rule for every
    # arm decides the parallelism of the scalable operator; what differs is the PLACEMENT that
    # comes with it. STOCK just rescales and Flink places. V4 and the bandit are COORDINATED: the
    # plan for the new parallelism is computed and published BEFORE the PUT — v4 from the loads it
    # sees divided over the new subtasks, the bandit from the best placement it has measured with
    # that many subtasks (else v4's proposal) — so the rescale lands on a planned placement instead
    # of falling back to LPT and being corrected afterwards.
    ap.add_argument("--autoscale", action="store_true")
    ap.add_argument("--scale-vertex", default="", help="name (substring) of the operator to scale")
    ap.add_argument("--kmin", type=int, default=1)
    ap.add_argument("--kmax", type=int, default=4)
    ap.add_argument("--up-streak", type=int, default=2, help="windows short in a row to scale up")
    ap.add_argument("--down-streak", type=int, default=2, help="windows idle in a row to scale down")
    ap.add_argument("--up-util", type=float, default=750.0,
                    help="busy ms/s per subtask the operator should stay under after scaling down")
    ap.add_argument("--scale-cooldown", type=float, default=240.0, help="s after a rescale before the next")
    ap.add_argument("--offered-lag", type=float, default=30.0,
                    help="seconds the delivered rate trails the schedule (see replan_monitor.observe)")
    ap.add_argument("--lag-floor", type=float, default=0.90,
                    help="delivered share below which a window is saturated whatever the latency")
    ap.add_argument("--latency-factor", type=float, default=2.0,
                    help="between --lag-floor and full, saturated only if latency >= this x the run's minimum")
    ap.add_argument("--smooth", type=float, default=0.4, help="weight of a new capacity reading")
    ap.add_argument("--fade", type=float, default=0.97, help="forgetting per decision")
    ap.add_argument("--heavy-share", type=float, default=0.5,
                    help="a kind is heavy if its peak busy reaches this share of the heaviest's")
    ap.add_argument("--heavy-floor", type=float, default=300.0,
                    help="ms/s of busy below which nothing is heavy")
    ap.add_argument("--model", default="", help="JSON to save what was learned")
    ap.add_argument("--warm", action="store_true", help="start from --model if it exists")
    ap.add_argument("--out-dir", default="results/plan-bandit")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--jm-pod", default="")
    # observe() and v4 settings
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--saturation-cut", type=float, default=900.0)
    ap.add_argument("--disk-suffix", default=".rocksdb_compact_write_bytes")
    ap.add_argument("--busy-cut", type=float, default=0.5)
    ap.add_argument("--disk-floor", type=float, default=1e6)
    args = ap.parse_args()

    ac._JM_POD = args.jm_pod or None
    ac._NAMESPACE = args.namespace
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_path = Path(args.model) if args.model else None
    stored = json.loads(model_path.read_text()) if (args.warm and model_path and model_path.exists()) else {}
    arms = {a: ArmStats(d) for a, d in stored.get("arms", {}).items()}
    peak_busy = dict(stored.get("peak_busy", {}))
    # What each KIND of move cost when it was made: "kind|from|to" -> loss. See "the penalty" below.
    penalties = dict(stored.get("penalties", {}))
    order = [tuple(p.split("<")) for p in args.dominance.split(",") if "<" in p]
    pending = None                  # (arm before the last move, the move's keys), until it is judged
    min_latency = None              # lowest p50 seen this run: the latency of a job that keeps up
    fresh = True                    # no window measured yet since the last restart
    # 'none': a path that never exists, so SliceSarsa starts from an empty table (its prior only).
    qtable_path = Path("/nonexistent/v4-sin-entrenar.json") if args.qtable in ("", "none") \
        else Path(args.qtable)
    agent = ca.SliceSarsa(0.0, 0.0, qtable_path, 3, ucb=0.0)
    arm_switched = args.base != "stock"   # STOCK + bandit: the fork's arm goes to RL on the first move
    short_streak = idle_streak = 0
    last_scale = 0.0
    scales = 0

    base_name = "Flink estándar" if args.base == "stock" else \
        ("v4 SIN ENTRENAR" if args.qtable in ("", "none") else "v4")
    names_mode = {"stock": "Flink estándar (solo mide)",
                  "v4": f"{base_name} sin monitoreo (plan inicial, solo mide)",
                  "bandit": f"{base_name} + bandit online"}
    print("=" * 70)
    print(f"  Monitor v2 — {names_mode[args.mode]}")
    print(f"  tasa {args.rate:.0f} ({args.dist}); ventana {args.window:.0f}s tras {args.warmup:.0f}s de asentamiento")
    print("=" * 70, flush=True)

    fields = ["time", "elapsed_s", "offered", "rps", "ratio", "latency_ms", "arm", "heavy",
              "decision", "why", "candidates", "best_capacity", "judged",
              "throttled_fast", "throttled_medium", "throttled_slow", "source_lag_epochs", "skipped_epochs",
              "clock_skew_ms"]
    csv_path = out / f"monitor2-{args.mode}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    started = time.time()
    last_key, settle_until, moves, initial_done = None, 0.0, 0, args.mode == "stock" or args.base == "stock"

    def save():
        if model_path:
            model_path.parent.mkdir(parents=True, exist_ok=True)
            model_path.write_text(json.dumps({"arms": {a: s.to_json() for a, s in arms.items()},
                                              "peak_busy": peak_busy, "penalties": penalties,
                                              "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                                             indent=1, sort_keys=True))

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while time.time() - started < args.duration:
            jid = ac.running_job(args.rest)
            key = ac.epoch_key(args.rest, jid) if jid else None
            if key is None:
                time.sleep(5)
                continue
            if key != last_key:
                last_key = key
                fresh = True                     # the next window is the first of this epoch
                settle_until = max(settle_until, ac.epoch_started_at(key, time.time()) + args.warmup)
            if time.time() < settle_until:
                time.sleep(5)
                continue
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            layout = ca.read_layout(args.node)
            if not detail or not layout:
                time.sleep(5)
                continue
            obs = rm.observe(args, jid, detail, layout)
            if obs is None:
                continue
            latency = ca.sink_record_latency_ms(args.rest, jid, detail)
            # DIAGNOSTICS, logged and not used (2026-10-15). In the one-hour test the shortfall on
            # a rising sine was asymmetric (+3% falling, -8% rising) with a normal latency. With
            # MAX_EVENT_AGE the bid sources DROP events more than 5 s late instead of queuing them,
            # so a source short of CPU loses events without raising the latency — and the rule for
            # light slices puts the sources on slow (1 core). The kernel's throttling per machine
            # and the sources' own lag and drop counters say whether that is it.
            throttled = {}
            for tm, m in (obs.get("machines") or {}).items():
                if m.get("throttled") is not None:
                    cls = machine_class(tm)
                    throttled[cls] = max(throttled.get(cls, 0.0), m["throttled"])
            src_lag = source_gauge(args.rest, jid, detail, "loopLagEpochs", "max")
            skipped = source_gauge(args.rest, jid, detail, "skippedEpochs", "sum")
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            kinds = [", ".join(sorted({names.get(v, v) for v, _ in m})) for m in layout["slices"]]
            running = [obs["where"].get(m[0]) for m in layout["slices"]]
            if None in running:
                continue
            capacity = dict(layout["tms"])
            speeds = ca.read_speeds(args.node)
            rate = obs["offered"]

            # Heavy kinds: by the PEAK load each kind has shown, so they stay heavy at low rates.
            # Sources by the CPU their thread burns, NOT the busy gauge v4 reads: that one is wall
            # time in the emit loop, which collect() blocks under backpressure — the first run on
            # the cluster (2026-10-13) took Q5's bid sources for heavy (~1000 ms/s while the counts
            # downstream were saturated) and spent explorations moving them. Same fix as v5's.
            cpu_ids = ca.generator_metric_ids(args.rest, jid, detail, "generatorCpuMsPerSecond")
            cpu_loads = ca.operator_profile(ca.subtask_loads(args.rest, jid, detail, cpu_ids)) \
                if cpu_ids else None
            cpu_slices = ca.slice_loads(layout, cpu_loads, args.saturation_cut) if cpu_loads else obs["per_slice"]
            sources, _sinks = ac.graph_roles(detail)
            for kind, members, (cost, sat), (cpu_cost, cpu_sat) in zip(kinds, layout["slices"],
                                                                       obs["per_slice"], cpu_slices):
                is_source = any(v in sources for v, _ in members)
                load = cpu_cost if (is_source and cpu_loads) else (1000.0 if sat else cost)
                peak_busy[kind] = max(peak_busy.get(kind, 0.0), load)
            top = max(peak_busy.values()) if peak_busy else 0.0
            heavy = {k for k, b in peak_busy.items()
                     if b >= args.heavy_floor and b >= args.heavy_share * top}
            here = arm_of(running, kinds, heavy)

            # THE PENALTY (2026-10-15, the professor's suggestion): judge the last move against
            # NOT having moved. The arm left behind is predicted at the rate measured NOW, from its
            # own capacity and latency, so a sine that fell or rose in between does not count as the
            # move's doing. If the move did worse by more than the noise, that loss is remembered
            # for the kind of move ("a hot-items count from medium to slow") and charged to every
            # later candidate that repeats it — including arms never run, which otherwise get the
            # full optimism bonus. A move that worked pulls its penalty down. Judged BEFORE
            # observe(), so the arm left behind is predicted from what was known when it was left.
            # A SHORTFALL IS NOT SATURATION BY ITSELF (2026-10-15, third cluster test). What the
            # sources deliver trails the offered schedule by ~15-25 s, so on a rising sine a job
            # that keeps up reads 94-95% (and over 100% on a falling one); with a 30-min period the
            # slope, and the error, are twice those of a 60-min one. The test read fast×2 at 2500-
            # 3000 ev/s as saturated with a normal 2.5 s latency and dragged its capacity from
            # ~5000 to ~3141. A job that cannot keep up builds a backlog, and the backlog shows in
            # the latency (12-16 s in every saturated window so far), so a small shortfall counts
            # only with the latency up; a large one (below --lag-floor) counts on its own. A small
            # shortfall with a normal latency is AMBIGUOUS — lag, or the first minute of a backlog
            # that has not reached the latency yet — so the window teaches nothing: no capacity, no
            # bound, no verdict on the last move (which waits for the next window).
            ratio = obs["ratio"]
            # Without a baseline yet, a small shortfall is ambiguous too: the sixth test read
            # fast×2 at 95.7% with 2.5 s of latency, before any window had kept up in full, as a
            # capacity of 2797 (true ~4860).
            ambiguous = (args.lag_floor <= ratio < FULL and latency is not None
                         and (min_latency is None or latency < args.latency_factor * min_latency))
            # Drops in the FIRST window after a restart are the sources catching up on the replay
            # (the skip counter restarts with the job, so anything in it is since the restart),
            # not the arm failing to keep up: pass 4 of the first campaign read fast×2 at 96%
            # with 525 drops and a normal latency as saturated and never went back to it.
            # Only with a normal latency: the seventh test saw real saturation right after a move
            # (65% delivered, 16 s) carrying drops too, and this rule hid it, so fast×2's capacity
            # was never learned.
            ambiguous = ambiguous or (fresh and ratio < FULL and bool(skipped) and latency is not None
                                      and min_latency is not None
                                      and latency < args.latency_factor * min_latency)
            fresh = False
            # The baseline is the latency of a job that KEPT UP: only windows delivered in full
            # update it. The fifth test started at the peak, saturated, so its "minimum" was 14 s
            # and a window at 90% with 10 s of latency passed for normal.
            if latency is not None and latency > 0 and obs["ratio"] >= FULL:
                min_latency = latency if min_latency is None else min(min_latency, latency)

            judged = ""
            if pending is not None and pending[0] in arms and not ambiguous:
                before, keys = pending
                d0, _ = arms[before].delivery(rate, args.headroom)
                lat0 = arms[before].latency(rate)
                expected = reward(d0, lat0 if lat0 is not None else latency, args.latency_weight)
                got = reward(ratio, latency, args.latency_weight)
                loss = max(0.0, expected - got - args.penalty_margin)
                # Only a move of ONE subtask leaves a penalty (2026-10-17, second campaign): a
                # two-subtask move (medium->fast plus medium->slow) lost because of its slow half
                # and charged the medium->fast direction too, which then kept the counts on medium
                # at 71% through the whole peak. Which half failed is not observable.
                for k in (keys if len(keys) == 1 else []):
                    penalties[k] = (1 - args.smooth) * penalties[k] + args.smooth * loss \
                        if k in penalties else loss
                judged = f"cambio anterior {expected - got:+.2f}"
                pending = None

            # Learn: the arm in force delivered this at this rate.
            stats = arms.setdefault(here, ArmStats())
            if not ambiguous:
                stats.observe(rate, ratio, latency, args.smooth,
                              at_limit=max(throttled.values(), default=0.0) >= args.limit_throttle)
            for a, s in arms.items():
                if a != here:
                    s.fade(args.fade)
            for k in penalties:
                penalties[k] *= args.fade

            # SARSA's proposal for what it sees now.
            ranked = sorted(layout["tms"], key=lambda t: (-speeds.get(t[0], 1.0), t[0]))
            plan = None
            if args.base == "v4":
                plan, _ = ca.decide(agent, obs["per_slice"], [f for _t, f in ranked], 0.0,
                                    args.busy_cut, disk=obs["per_slice_disk"], machine_load=True,
                                    disk_floor=args.disk_floor)
            sarsa_place = [ranked[plan[i]][0] for i in range(len(running))] if plan else None
            sarsa_arm = arm_of(sarsa_place, kinds, heavy) if sarsa_place else None

            class_speed = {}
            for tm, sp in speeds.items():
                class_speed[machine_class(tm)] = max(class_speed.get(machine_class(tm), 0.0), sp)
            top_speed = max(class_speed.values(), default=1.0) or 1.0

            def heavy_speed(arm):
                """Mean published speed of the machines that hold the arm's heavy subtasks, 0-1."""
                got = [class_speed.get(c, 0.0) / top_speed
                       for layout_ in json.loads(arm).values() for c, n in layout_.items() for _ in range(n)]
                return sum(got) / len(got) if got else 1.0

            def predicted(arm):
                s = arms.get(arm)
                here_s = arms[here]
                if s is None or s.n == 0:
                    lat = here_s.latency(rate)
                    base = reward(1.0, lat, args.latency_weight)
                    # Among arms never run, the published machine speeds break the tie: half the
                    # bonus is scaled by how fast the machines holding the heavy subtasks are. Twice
                    # the bandit tried a count on slow (1 core) first, at the peak, for 38% and 65%.
                    bonus = args.optimism * (0.5 + 0.5 * heavy_speed(arm)) \
                        + (args.sarsa_bonus if arm == sarsa_arm else 0.0)
                    # DOMINANCE (2026-10-15): an arm never run is no better than a measured arm
                    # that has the same heavy subtasks on strictly better machines. The test run
                    # tried two counts on slow at 4683 ev/s (57% delivered) with medium+slow
                    # already measured at ~3346: the optimism bonus overrode what was known.
                    caps = [predicted(b)[0] for b, sb in arms.items()
                            if sb.n and dominated_by(arm, b, order)]
                    if caps and min(caps) < base + bonus:
                        return min(caps), "nunca medido, acotado por uno mejor medido"
                    return base + bonus, "nunca medido"
                d, known = s.delivery(rate, args.headroom)
                lat = s.latency(rate)
                if lat is None:
                    lat = here_s.latency(rate)
                value = reward(d, lat, args.latency_weight)
                if not known:
                    value += args.unknown_limit
                return value, ("capacidad %.0f" % s.cap if s.cap else "aguanta >= %.0f" % s.lo)

            total_slots = {}
            for tm, slots in capacity.items():
                total_slots[machine_class(tm)] = total_slots.get(machine_class(tm), 0) + slots
            cands = {here: "quedarse"}
            for a, why in neighbours(here, total_slots):
                cands.setdefault(a, why)
            if sarsa_arm:
                cands.setdefault(sarsa_arm, "propuesta de SARSA")
            # Only arms with as many heavy subtasks as now: after a rescale the arms measured at the
            # old parallelism cannot be realised (the simulator's first scaled run kept proposing them).
            n_heavy = sum(sum(v.values()) for v in json.loads(here).values())
            measured = [a for a, s in arms.items() if s.n
                        and sum(sum(v.values()) for v in json.loads(a).values()) == n_heavy]
            if measured:
                best = max(measured, key=lambda a: predicted(a)[0])
                cands.setdefault(best, "el mejor medido")

            # The decision.
            decision, why, chosen = "quedarse", "", here
            if not initial_done:
                initial_done = True
                if sarsa_arm and sarsa_arm != here:
                    decision, why, chosen = "mover", "plan inicial de v4", sarsa_arm
                else:
                    why = "plan inicial de v4 ya aplicado"
            elif args.mode == "bandit":
                # EXPLORE ONLY WHEN THE ARM IN FORCE FALLS SHORT (2026-10-16, sixth test). Arms that
                # keep up all read the same, so trying one in the valley teaches nothing and costs
                # a restart, and an arm that delivers in full at a normal latency has no reason to
                # be left: when it does, only arms already MEASURED are candidates. And never move a
                # heavy subtask INTO a machine the kernel is throttling (--busy-machine): the test
                # moved the second count to slow with slow at 82% and delivered 38%.
                # "Short" = it did not deliver in full. A rising latency at full delivery (the
                # seventh test: fast×2 at 100% with 5.4 s) is a warning, not a shortfall: it may move
                # to an arm already measured, but tries a NEW one only once it actually falls short —
                # it tried slow there and delivered 65% at the peak.
                short = obs["ratio"] < FULL and not ambiguous
                for a in list(cands):
                    if a == here:
                        continue
                    if not short and not (arms.get(a) and arms[a].n):
                        del cands[a]
                    elif any(throttled.get(k.split("|")[2], 0.0) >= args.busy_machine
                             for k in move_keys(here, a)):
                        del cands[a]
                def charge(a):
                    return 0.0 if a == here else args.move_cost + args.penalty_weight * sum(
                        penalties.get(k, 0.0) for k in move_keys(here, a))
                values = {a: predicted(a)[0] - charge(a) for a in cands}
                chosen = max(values, key=values.get)
                if chosen != here:
                    pen = charge(chosen) - args.move_cost
                    decision, why = "mover", f"{cands[chosen]} ({predicted(chosen)[1]}" \
                        + (f", penalización {pen:.2f})" if pen > 0.005 else ")")
            else:
                why = "solo mide"

            # ---- THE COMMON AUTOSCALER (see --autoscale). Same rule for every arm; it overrides a
            # placement move in the cycle it fires. Up: the job fell short --up-streak windows in a
            # row. Down: --down-streak windows in a row in which the operator, spread over one
            # subtask less, would still stay under --up-util busy ms/s per subtask.
            if args.autoscale and args.scale_vertex and initial_done:
                group = {v["id"] for v in detail.get("vertices", []) if args.scale_vertex in v.get("name", "")}
                kcur = max((int(v.get("parallelism", 1)) for v in detail.get("vertices", [])
                            if v["id"] in group), default=0)
                gslices = [i for i, m in enumerate(layout["slices"]) if any(v in group for v, _ in m)]
                busy = [1000.0 if obs["per_slice"][i][1] else obs["per_slice"][i][0] for i in gslices]
                mean_busy = sum(busy) / len(busy) if busy else 0.0
                short_now = ratio < FULL and not ambiguous
                short_streak = short_streak + 1 if short_now else 0
                idle_now = (not short_now and kcur > 1
                            and mean_busy * kcur / (kcur - 1) < args.up_util)
                idle_streak = idle_streak + 1 if idle_now else 0
                k_new = None
                if group and gslices and time.time() - last_scale >= args.scale_cooldown:
                    if short_streak >= args.up_streak and kcur < args.kmax:
                        k_new = kcur + 1
                    elif idle_streak >= args.down_streak and kcur > args.kmin:
                        k_new = kcur - 1
                if k_new is not None:
                    fut = future_slices(layout["slices"], group, k_new)
                    fut_kinds = [", ".join(sorted({names.get(v, v) for v, _ in m})) for m in fut]
                    gkind = kinds[gslices[0]]
                    heavy_g = set(heavy) | {gkind}
                    where_now = {tuple(m[0]): running[i] for i, m in enumerate(layout["slices"])}
                    fut_running = [where_now.get(tuple(m[0])) for m in fut]
                    placement, how = None, "Flink coloca"
                    target = None
                    if args.mode in ("v4", "bandit"):
                        # The future slices' loads: the operator's total busy spread over k_new.
                        disk_now = obs.get("per_slice_disk") or [0.0] * len(layout["slices"])
                        tot = sum(busy)
                        tot_disk = sum(disk_now[i] for i in gslices)
                        idx_now = {tuple(m[0]): i for i, m in enumerate(layout["slices"])}
                        fps, fdisk = [], []
                        for m in fut:
                            if m[0][0] in group:
                                fps.append((tot / k_new, False))
                                fdisk.append(tot_disk / k_new)
                            else:
                                j = idx_now[tuple(m[0])]
                                fps.append(obs["per_slice"][j])
                                fdisk.append(disk_now[j])
                        fplan, _ = ca.decide(agent, fps, [f for _t, f in ranked], 0.0, args.busy_cut,
                                             disk=fdisk, machine_load=True, disk_floor=args.disk_floor)
                        fplace = [ranked[fplan[i]][0] for i in range(len(fut))] if fplan else None
                        target = None
                        if args.mode == "bandit":
                            known = [a_ for a_, s_ in arms.items() if s_.n and
                                     sum(json.loads(a_).get(gkind, {}).values()) == k_new]
                            if known:
                                target = max(known, key=lambda a_: predicted(a_)[0])
                                how = "el mejor medido con %d" % k_new
                        if target is None and args.mode == "bandit":
                            # Nothing measured with k_new: EXTEND the placement in force — the new
                            # subtask to the fastest class with room, or on the way down the one on
                            # the slowest class goes. The bandit's own neighbourhood, one step in k;
                            # a stale v4 put all three counts on medium in the simulator's first run.
                            cur = json.loads(here)
                            lay = dict(cur.get(gkind, {}))
                            classes = sorted(class_speed, key=lambda c: -class_speed[c])
                            tries = classes if k_new > kcur else [c for c in reversed(classes) if lay.get(c, 0)]
                            for c in tries:
                                l2 = dict(lay)
                                l2[c] = l2.get(c, 0) + (1 if k_new > kcur else -1)
                                l2 = {cc: n for cc, n in l2.items() if n > 0}
                                cand = dict(cur)
                                cand[gkind] = l2
                                cand_arm = json.dumps({k_: dict(sorted(v.items())) for k_, v in sorted(cand.items())},
                                                      sort_keys=True)
                                pl = realise(cand_arm, fut_running, fut_kinds, heavy_g, capacity, speeds)
                                if pl:
                                    placement = pl
                                    how = ("+1 en %s" if k_new > kcur else "-1 en %s") % c
                                    break
                        if target is None and placement is None and fplace:
                            target = arm_of(fplace, fut_kinds, heavy_g)
                            how = "plan de v4 para %d" % k_new
                        if target and placement is None:
                            placement = realise(target, fut_running, fut_kinds, heavy_g, capacity, speeds)
                        if not placement and fplace:
                            placement, how = fplace, "plan de v4 para %d" % k_new
                    error = None
                    if placement:
                        if not arm_switched:
                            ca.node_command(args.node, ["sh", "-c", "printf RL > /var/thesis/arm"])
                            arm_switched = True
                        ok, error = ca.publish_plan({len(fut): {f"{vid}:{sub}": placement[i]
                                                               for i, members in enumerate(fut)
                                                               for vid, sub in members}}, args.node)
                        if not ok:
                            error = error or "no se pudo publicar el plan"
                    if not error:
                        ok, error = rescale_put(jid, detail, group, k_new)
                    if error:
                        decision, why = "error", f"escalar {kcur}->{k_new}: {error}"
                    else:
                        decision, why = "escalar", f"{gkind[:24]} {kcur} -> {k_new} ({how})"
                        scales += 1
                        last_scale = time.time()
                        settle_until = time.time() + args.warmup
                        pending = None
                        short_streak = idle_streak = 0

            if decision == "mover":
                placement = realise(chosen, running, kinds, heavy, capacity, speeds)
                if not placement and chosen == sarsa_arm and sarsa_place:
                    placement = rm.fewest_moves(sarsa_place, running, kinds)
                if not placement:
                    decision, why = "quedarse", f"no se pudo realizar {cands.get(chosen, chosen)}"
                else:
                    plans = {len(running): {f"{vid}:{sub}": placement[i]
                                            for i, members in enumerate(layout["slices"])
                                            for vid, sub in members}}
                    if not arm_switched:
                        # The run started on the fork's STOCK arm, which ignores published plans:
                        # from the first move on the plan has to be applied, so the arm goes to RL.
                        ca.node_command(args.node, ["sh", "-c", "printf RL > /var/thesis/arm"])
                        arm_switched = True
                    ok, error = ca.publish_plan(plans, args.node)
                    token = str(int(time.time() * 1000))
                    if ok:
                        _, error = ca.node_command(args.node, ["sh", "-c",
                                                   f"printf '%s {args.replan_at}\\n' {token} > {REPLAN_FILE}"])
                    if ok and not error and args.replan_at == "checkpoint":
                        # The fork polls the request file every 2 s and only then arms it: a
                        # checkpoint completed before that would not trigger the restart.
                        time.sleep(5.0)
                        took, ck_error = trigger_checkpoint(jid)
                        why += f"; checkpoint {took:.0f} s" if took is not None else ""
                        if ck_error:
                            # The request stays armed: the restart comes with the next periodic
                            # checkpoint instead (<= 180 s), replaying more. Logged, not fatal.
                            why += f"; ! checkpoint manual falló ({ck_error}), queda para el periódico"
                    if not ok or error:
                        decision, why = "error", str(error)
                    else:
                        moves += 1
                        settle_until = time.time() + args.warmup
                        pending = (here, move_keys(here, chosen))

            caps = [s.cap if s.cap else s.lo for s in arms.values() if s.n]
            best_cap = max(caps) if caps else None
            # "No placement suffices" only once the neighbourhood is EXHAUSTED: every candidate has
            # been run and seen at its limit below the offered rate. The first cluster run said it
            # with one arm measured and four never tried, which is not evidence of anything.
            exhausted = all(arms.get(a) is not None and arms[a].n and
                            (arms[a].cap is not None or arms[a].lo >= rate) for a in cands)
            scale = (args.mode == "bandit" and exhausted and best_cap is not None
                     and rate > best_cap * 1.02)
            short = lambda a: "; ".join(f"{k.split(',')[0].split(':')[-1].strip()[:12]} "     # noqa
                                        + "+".join(f"{c}x{n}" for c, n in v.items())
                                        for k, v in json.loads(a).items()) or "(sin pesados)"
            print(f"  [{time.strftime('%H:%M:%S')}] pedido {rate:6.0f} entrega {obs['ratio']:5.1%} "
                  f"lat {latency} ms | {short(here)} | {len(cands)} candidatos -> {decision.upper()}"
                  + (f": {why}" if why else "")
                  + ("   (falta con latencia normal: ventana ambigua, no se aprende)" if ambiguous else "")
                  + ("   freno " + " ".join(f"{c} {v:.0%}" for c, v in sorted(throttled.items())) if throttled else "")
                  + (f"   descartadas {skipped:.0f}" if skipped else "")
                  + (f"   [{judged}]" if judged else "")
                  + ("   ! replanificar no basta: la mejor capacidad conocida es %.0f — habría que escalar"
                     % best_cap if scale else ""), flush=True)
            writer.writerow({"time": time.strftime("%H:%M:%S"), "elapsed_s": round(time.time() - started),
                             "offered": round(rate), "rps": round(obs["rps"]),
                             "ratio": round(obs["ratio"], 4), "latency_ms": latency, "arm": short(here),
                             "heavy": "|".join(sorted(heavy)), "decision": decision, "why": why,
                             "candidates": len(cands), "best_capacity": round(best_cap) if best_cap else "",
                             "judged": judged,
                             **{f"throttled_{c}": round(throttled[c], 3) for c in throttled
                                if c in ("fast", "medium", "slow")},
                             "source_lag_epochs": src_lag, "skipped_epochs": skipped,
                             "clock_skew_ms": obs.get("clock_skew_ms")})
            handle.flush()
            save()

    print(f"\nfin: {moves} cambios de emplazamiento, {scales} reescalados; {csv_path}")
    for a, s in sorted(arms.items(), key=lambda kv: -(kv[1].cap or kv[1].lo)):
        if s.n:
            print(f"  {short(a):40s} n={s.n:2d}  "
                  + (f"capacidad ~{s.cap:.0f}" if s.cap else f"aguanta >= {s.lo:.0f}"))
    for k, v in sorted(penalties.items(), key=lambda kv: -kv[1]):
        if v > 0.005:
            kind, src, dst = k.split("|")
            print(f"  penalización {v:.2f}: mover un {kind[:24]} de {src} a {dst}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
