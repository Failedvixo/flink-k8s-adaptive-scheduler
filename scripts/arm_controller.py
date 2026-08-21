#!/usr/bin/env python3
"""
Phase 3 — the external meta-scheduler that learns which slot-placement arm to
publish to the forked Flink JobManager.

Where this sits
---------------
Phase 2 proved the forked SlotAssigner really changes placement, but only at a
rescale and only with the arm baked into the pod's environment, so a run could
never use more than one arm. This controller closes the loop:

    rescale event  ->  measure the placement that arm produced (Flink metrics)
                   ->  reward  ->  SARSA update  ->  publish the next arm
                   ->  the JobManager applies it at the following rescale

Nothing here runs inside the JobManager. The assigner stays a pure function of
(slices, slots, published arm); every bit of state and learning lives in this
process, which is what makes the fork safe against the adaptive scheduler's
speculative re-invocations of the assigner.

Why the reward comes from Flink's own metrics
---------------------------------------------
Two rejected alternatives, both of which the project used earlier:

  * K8s node CPU/memory (the Metrics Server, as the pod-placement strategies
    use) is the wrong granularity: several TaskManagers share a node, so two
    placements that differ exactly in how they split work between two TMs on
    one node are indistinguishable at node level — while the assigner decides
    at TM level.
  * The structural `balanced` counter used to validate Phase 2 counts slices
    per TM, not load. A 1/1/1 spread scores perfect even when one slice carries
    the source operator on top of the window and genuinely does more work.

So load is read from Flink itself: `busyTimeMsPerSecond` per subtask, joined to
the TaskManager hosting that subtask via the `taskmanager-id` field of
`/jobs/{jid}/vertices/{vid}`. That join is the whole trick — it converts
per-subtask metrics into the per-TM granularity the assigner decides at, and it
is engine-level, so the signal does not depend on the machine, the cloud or the
node exporter.

Reward
------
Default reward is placement dispersion:

    reward = 1 / (1 + cv_busy)      cv_busy = coefficient of variation of the
                                    per-TM busy time, over all registered TMs

Bounded in (0, 1], 1.0 meaning every TaskManager carries the same load. Taken
over *all registered* TaskManagers rather than only the ones hosting a slice,
because an arm that packs every slice onto one TM would otherwise score a
perfect zero dispersion — the packing is precisely the pathology under study.

Dispersion is preferred over throughput/core as the learning signal because a
rescale changes parallelism and throughput with it, so throughput cannot tell a
good placement from a bigger job (the reward confound of Phase 2). Throughput is
still recorded on every row, so any other reward can be recomputed offline from
the CSV without re-running the cluster: `--reward throughput` and
`--reward blend` do exactly that online.

The two meta-schedulers
-----------------------
`--meta sarsa` (default) conditions the arm on the discretised Flink-metric state; `--meta bandit`
is UCB1 and ignores the state. They see identical rewards, so a SARSA win over the bandit is
evidence that the best placement arm actually depends on the cluster's condition, rather than one
arm simply being better everywhere.

Usage
-----
  # learn online, publishing a new arm after every rescale
  scripts/arm_controller.py --meta sarsa
  scripts/arm_controller.py --meta bandit

  # measure one arm without learning (per-arm baselines)
  scripts/arm_controller.py --fixed-arm ROUND_ROBIN

  # watch and record only; never touch the arm file
  scripts/arm_controller.py --observe

Assumes the Flink REST API is reachable (kubectl port-forward -n flink
svc/flink-jobmanager 8081:8081) and that the JobManager runs the fork
(scripts/deploy-thesis-fork.sh).
"""
import argparse
import csv
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REST = os.environ.get("FLINK_REST_URL", "http://localhost:8081")
DEFAULT_ARMS = ["FCFS", "ROUND_ROBIN", "LEAST_LOADED", "LPT", "ACO", "GA"]

# Rates, not accumulators: Flink also exposes accumulated busy time, which grows
# monotonically and would make every later window look busier than the last.
SUBTASK_METRICS = [
    "busyTimeMsPerSecond",
    "backPressuredTimeMsPerSecond",
    "idleTimeMsPerSecond",
    "numRecordsInPerSecond",
    "numRecordsOutPerSecond",
    # End-to-end delay in EVENT time: how far the sink's notion of event time
    # trails the wall clock. Unlike busy%, which says how hard the machines are
    # working, this says what the pipeline's user actually waits for, and it is
    # where an imbalance shows up worst — the watermark is a MINIMUM over the
    # subtasks, so one lagging subtask holds the whole job back.
    "currentInputWatermark",
]

# A subtask that has not seen a watermark yet reports Long.MIN_VALUE; treating it
# as a timestamp yields a delay of ~2.9e11 seconds and poisons every mean it
# reaches. Anything older than this is not a watermark, it is the sentinel.
WATERMARK_FLOOR_MS = 0

# The assigner's own log line, which is ground truth for the arm that was really
# applied: the controller can publish an arm that a rescale never picks up.
RX_ASSIGN = re.compile(
    r"\[THESIS_ASSIGN\] strategy=(?P<strategy>[A-Z_]+)"
    r"(?:\((?P<delegate>\w+)\))? slices=(?P<slices>\d+) freeSlots=(?P<free>\d+) "
    r"tmsAvailable=(?P<tms>\d+) tmsUsed=(?P<used>\d+)"
)


# ---------------------------------------------------------------------------
# Flink REST
# ---------------------------------------------------------------------------
def rest(base, path, timeout=5.0):
    """GET a JSON document from the Flink REST API, or None if it is unreachable."""
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def running_job(base):
    """The id of the single RUNNING job, or None."""
    overview = rest(base, "/jobs/overview")
    if not overview:
        return None
    running = [j for j in overview.get("jobs", []) if j.get("state") == "RUNNING"]
    return running[0]["jid"] if running else None


def registered_taskmanagers(base):
    """Every TaskManager the cluster currently has, hosting a slice or not."""
    payload = rest(base, "/taskmanagers")
    if not payload:
        return []
    return [tm["id"] for tm in payload.get("taskmanagers", [])]


def slot_capacity(base):
    """(TaskManagers, total slots) as the cluster reports them."""
    payload = rest(base, "/taskmanagers")
    if not payload:
        return 0, 0
    taskmanagers = payload.get("taskmanagers", [])
    return len(taskmanagers), sum(int(tm.get("slotsNumber", 0)) for tm in taskmanagers)


def assignment_from_rest(base, measurement, arm):
    """
    The assignment record rebuilt from Flink's own REST API instead of the fork's log.

    UNMODIFIED Flink logs no `[THESIS_ASSIGN]` line, so measuring stock Flink as a baseline —
    which is the only honest comparison now that 2.x ships its own load balancing — would
    otherwise yield episodes with no `had_choice` and be discarded wholesale by the analysis.

    Everything here is derivable: the slice count and the TaskManagers hosting them come from
    the measurement, and the slot pool from /taskmanagers. `had_choice` is the one inference —
    a rescale restarts the whole job, so every slot is free at the moment of assignment, and the
    assigner therefore had a decision exactly when the pool is larger than the job is wide.
    """
    taskmanagers, total_slots = slot_capacity(base)
    slices = measurement["slices"]
    return {
        "arm": arm,
        "delegate": "stock",
        "slices": slices,
        "free_slots": total_slots,
        "tms_available": taskmanagers or measurement["tms_total"],
        "tms_used": measurement["tms_hosting"],
        "had_choice": total_slots > slices,
    }


def job_vertices(base, jid):
    """[(vertex_id, name, parallelism)] for the job, in topological order."""
    detail = rest(base, f"/jobs/{jid}")
    if not detail:
        return []
    return [
        (v["id"], v.get("name", ""), int(v.get("parallelism", 0)))
        for v in detail.get("vertices", [])
    ]


def epoch_started_at(key, fallback):
    """
    When the placement epoch really began, from the vertices' own start times.

    The controller polls, so it learns about a rescale up to one poll interval
    late, and it may attach to a job that has been running for a while. Timing
    the warm-up and the log lookback from the job's clock instead of the
    controller's keeps both anchored to the restart the assigner reacted to.
    """
    starts = [start for _vid, _par, start in key if start > 0]
    if not starts:
        return fallback
    started = max(starts) / 1000.0
    # A JobManager clock far from ours would silently skew every window; fall
    # back rather than measure a window that never happened.
    if not 0 < fallback - started < 24 * 3600:
        return fallback
    return started


def epoch_key(base, jid):
    """
    A value that changes exactly when the placement can have changed.

    A rescale restarts the execution graph, so both the parallelism vector and
    the per-vertex start times move. Using the start times as well catches a
    restart that keeps parallelism (a failure recovery), which also re-runs the
    assigner and therefore also opens a new placement epoch.
    """
    detail = rest(base, f"/jobs/{jid}")
    if not detail:
        return None
    return tuple(
        (v["id"], int(v.get("parallelism", 0)), int(v.get("start-time", -1)))
        for v in detail.get("vertices", [])
    )


def subtask_taskmanagers(base, jid, vid):
    """
    {subtask index -> taskmanager id} — THE join key.

    Without it there are per-subtask metrics and per-TM placement decisions with
    no way to connect the two.
    """
    detail = rest(base, f"/jobs/{jid}/vertices/{vid}")
    if not detail:
        return {}
    mapping = {}
    for subtask in detail.get("subtasks", []):
        tm = subtask.get("taskmanager-id")
        if tm:
            # "container_id:port" in some deployments; the resource id is the
            # part the assigner logs, so keep the whole string and let the
            # comparison be by identity rather than by parsing.
            mapping[int(subtask["subtask"])] = tm
    return mapping


def subtask_metrics(base, jid, vid, index):
    """The rate metrics of one subtask, as floats (missing metrics -> 0.0)."""
    query = ",".join(SUBTASK_METRICS)
    payload = rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/{index}/metrics?get={query}")
    values = {name: 0.0 for name in SUBTASK_METRICS}
    for entry in payload or []:
        try:
            value = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue
        # Flink reports NaN for a metric whose task has not produced a sample yet,
        # which is normal in the seconds after a rescale. Left in, it propagates
        # through every mean and dispersion downstream — and NaN does not merely
        # skew a number, it aborts the run.
        if math.isfinite(value):
            values[entry["id"]] = value
    return values


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def sample_once(base, jid, all_taskmanagers):
    """
    One snapshot of the job: load per TaskManager plus job-level rates.

    Per-TM load is the SUM of its subtasks' busy time, not the mean: a TM
    hosting two fully busy subtasks really is carrying twice the work of a TM
    hosting one, and averaging would hide exactly the packing this measures.
    """
    vertices = job_vertices(base, jid)
    if not vertices:
        return None

    busy_per_tm = {tm: 0.0 for tm in all_taskmanagers}
    subtask_busy = []
    subtask_backpressure = []
    subtask_idle = []
    hosting = set()
    source_out = 0.0
    sink_in = 0.0
    sink_watermarks = []

    for position, (vid, _name, parallelism) in enumerate(vertices):
        placement = subtask_taskmanagers(base, jid, vid)
        for index in range(parallelism):
            values = subtask_metrics(base, jid, vid, index)
            busy = values["busyTimeMsPerSecond"]
            subtask_busy.append(busy)
            subtask_backpressure.append(values["backPressuredTimeMsPerSecond"])
            subtask_idle.append(values["idleTimeMsPerSecond"])
            tm = placement.get(index)
            if tm is not None:
                busy_per_tm.setdefault(tm, 0.0)
                busy_per_tm[tm] += busy
                hosting.add(tm)
            if position == 0:
                source_out += values["numRecordsOutPerSecond"]
            if position == len(vertices) - 1:
                sink_in += values["numRecordsInPerSecond"]
                watermark = values["currentInputWatermark"]
                if watermark > WATERMARK_FLOOR_MS:
                    sink_watermarks.append(watermark)

    if not subtask_busy:
        return None

    # The job's event-time position is the SLOWEST sink subtask, because a
    # downstream consumer can only trust event time up to the minimum. Reporting
    # the mean would let a fast subtask mask the straggler an imbalance creates —
    # which is precisely the effect under study.
    now_ms = time.time() * 1000.0
    e2e_delay = (now_ms - min(sink_watermarks)) if sink_watermarks else None
    e2e_delay_spread = (
        (max(sink_watermarks) - min(sink_watermarks)) if len(sink_watermarks) > 1 else 0.0
    )

    return {
        "busy_per_tm": busy_per_tm,
        "hosting": hosting,
        "e2e_delay_ms": e2e_delay,
        "e2e_delay_spread_ms": e2e_delay_spread,
        "slices": max(p for _v, _n, p in vertices),
        "busy_mean": statistics.fmean(subtask_busy),
        "backpressure_mean": statistics.fmean(subtask_backpressure),
        "idle_mean": statistics.fmean(subtask_idle),
        "source_out_rps": source_out,
        "sink_in_rps": sink_in,
    }


def coefficient_of_variation(values):
    """
    Dispersion normalised by the mean, so it does not grow with the load itself.

    The standard deviation is computed by hand rather than with `statistics`: a
    single non-finite sample makes that module raise, and losing a six-hour
    campaign to one NaN from a task that had not reported yet is not a trade
    worth making.
    """
    values = [v for v in values if math.isfinite(v)]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance) / mean


def measure(base, jid, all_taskmanagers, window, interval, verbose=False,
            trace=None, trace_interval=1.0):
    """
    Average several snapshots over the measurement window.

    A single snapshot of `busyTimeMsPerSecond` is a one-second average and is
    noisy enough to reorder two arms on its own, so the window is what the
    reward is actually computed from.
    """
    samples = []
    deadline = time.time() + window
    while time.time() < deadline:
        snapshot = sample_once(base, jid, all_taskmanagers)
        if snapshot:
            samples.append(snapshot)
            if verbose:
                print(
                    f"    sample busy_mean={snapshot['busy_mean']:7.1f} ms/s  "
                    f"tms_hosting={len(snapshot['hosting'])}"
                )
        # Sleeping straight through `interval` is what used to blind the controller
        # for the whole window: a rescale landing inside it was detected up to
        # `window` seconds late, and the transient it opened was already over. The
        # cheap counter keeps ticking here so the recovery curve survives that.
        deadline_for_this_nap = time.time() + interval
        while time.time() < deadline_for_this_nap:
            time.sleep(min(trace_interval, max(0.0, deadline_for_this_nap - time.time())))
            if trace is not None:
                produced = source_records(base, jid)
                if produced is not None:
                    trace.append((time.time(), produced))

    if not samples:
        return None

    taskmanagers = sorted({tm for s in samples for tm in s["busy_per_tm"]})
    busy_per_tm = {
        tm: statistics.fmean([s["busy_per_tm"].get(tm, 0.0) for s in samples])
        for tm in taskmanagers
    }
    hosting = {tm for s in samples for tm in s["hosting"]}

    def mean_of(field):
        return statistics.fmean([s[field] for s in samples])

    # Present only once watermarks have advanced; right after a rescale the sink
    # may have none, and averaging over the samples that DO have one is better
    # than reporting a delay of zero for a job that simply has not caught up.
    def mean_where_present(field):
        values = [s[field] for s in samples if s.get(field) is not None]
        return statistics.fmean(values) if values else None

    return {
        "samples": len(samples),
        "e2e_delay_ms": mean_where_present("e2e_delay_ms"),
        "e2e_delay_spread_ms": mean_where_present("e2e_delay_spread_ms"),
        "busy_per_tm": busy_per_tm,
        "tms_total": len(taskmanagers),
        "tms_hosting": len(hosting),
        "slices": max(s["slices"] for s in samples),
        "busy_mean": mean_of("busy_mean"),
        "backpressure_mean": mean_of("backpressure_mean"),
        "idle_mean": mean_of("idle_mean"),
        "source_out_rps": mean_of("source_out_rps"),
        "sink_in_rps": mean_of("sink_in_rps"),
        # Over every registered TM: an arm that packs all slices onto one TM
        # would score a perfect 0 if only the hosting TMs were counted.
        "cv_busy_all": coefficient_of_variation(busy_per_tm.values()),
        "cv_busy_hosting": coefficient_of_variation(
            [v for tm, v in busy_per_tm.items() if tm in hosting]
        ),
    }


def reward_of(measurement, kind, blend_weight, throughput_scale):
    """
    Map a measurement to a scalar in (0, 1].

    `dispersion` is the default because it is the only one of the three that is
    invariant to the parallelism change the rescale itself caused.
    """
    dispersion = 1.0 / (1.0 + measurement["cv_busy_all"])
    slots = max(measurement["slices"], 1)
    per_slot = measurement["source_out_rps"] / slots
    throughput = per_slot / throughput_scale if throughput_scale > 0 else 0.0
    throughput = min(throughput, 1.0)
    if kind == "dispersion":
        return dispersion
    if kind == "throughput":
        return throughput
    return (1.0 - blend_weight) * dispersion + blend_weight * throughput


# ---------------------------------------------------------------------------
# state and learner
# ---------------------------------------------------------------------------
def discretise(measurement, bins):
    """
    The SARSA state: three engine-level features, each cut into LOW/MED/HIGH.

    Backpressure is a state feature and deliberately not part of the reward: a
    backpressured subtask says the bottleneck is DOWNSTREAM of it, which is
    information about where the job is tight, not evidence that its placement
    is bad. Rewarding it would conflate the two.
    """
    saturation = measurement["busy_mean"] / 1000.0
    imbalance = measurement["cv_busy_all"]
    backpressure = measurement["backpressure_mean"] / 1000.0
    return "|".join(
        [
            f"sat={level(saturation, bins['saturation'])}",
            f"imb={level(imbalance, bins['imbalance'])}",
            f"bp={level(backpressure, bins['backpressure'])}",
        ]
    )


def level(value, edges):
    low, high = edges
    if value < low:
        return "LOW"
    if value < high:
        return "MED"
    return "HIGH"


class Ucb1:
    """
    UCB1 over the arms, ignoring the state entirely — the context-free meta-scheduler.

    It is the honest contrast to SARSA rather than a weaker version of it: if the best arm does not
    depend on the cluster's condition, a bandit finds it with far fewer episodes, and SARSA's extra
    parameters buy nothing. If SARSA wins, the win *is* the evidence that placement quality is
    state-dependent. Both are fed identical rewards so the comparison is about the policy alone.
    """

    def __init__(self, arms, path, exploration=1.0):
        self.arms = arms
        self.path = path
        self.exploration = exploration
        self.stats = {arm: {"n": 0, "mean": 0.0} for arm in arms}
        if path and path.exists():
            stored = json.loads(path.read_text())
            for arm in arms:
                if arm in stored:
                    self.stats[arm] = stored[arm]

    def value(self, _state, arm):
        return self.stats.get(arm, {}).get("mean", 0.0)

    def choose(self, _state):
        total = sum(s["n"] for s in self.stats.values())
        untried = [arm for arm in self.arms if self.stats[arm]["n"] == 0]
        if untried:
            # Every arm must be pulled once before a confidence bound means anything.
            return untried[0], True
        def score(arm):
            s = self.stats[arm]
            return s["mean"] + self.exploration * math.sqrt(2.0 * math.log(total) / s["n"])
        best = max(self.arms, key=lambda arm: (score(arm), arm))
        greedy = max(self.arms, key=lambda arm: (self.stats[arm]["mean"], arm))
        return best, best != greedy

    def update(self, _state, arm, reward, _next_state, _next_arm):
        stat = self.stats.setdefault(arm, {"n": 0, "mean": 0.0})
        before = stat["mean"]
        stat["n"] += 1
        stat["mean"] += (reward - stat["mean"]) / stat["n"]
        return before, stat["mean"]

    def save(self):
        if self.path:
            self.path.write_text(json.dumps(self.stats, indent=2, sort_keys=True))


class Sarsa:
    """
    Tabular on-policy SARSA over (engine state -> placement arm).

    On-policy is the honest choice here: the arm that gets evaluated is the arm
    that was actually published and applied, exploration included, and there is
    no way to evaluate a counterfactual arm on the same rescale.
    """

    def __init__(self, arms, alpha, gamma, epsilon, path):
        self.arms = arms
        self.alpha = alpha
        self.gamma = gamma
        self.epsilon = epsilon
        self.path = path
        self.q = {}
        if path and path.exists():
            self.q = json.loads(path.read_text())

    def value(self, state, arm):
        return self.q.get(state, {}).get(arm, 0.0)

    def choose(self, state):
        """Epsilon-greedy; unseen arms count as 0.0, so every arm gets tried early."""
        if random.random() < self.epsilon:
            return random.choice(self.arms), True
        best = max(self.arms, key=lambda arm: (self.value(state, arm), arm))
        return best, False

    def update(self, state, arm, reward, next_state, next_arm):
        target = reward + self.gamma * self.value(next_state, next_arm)
        old = self.value(state, arm)
        new = old + self.alpha * (target - old)
        self.q.setdefault(state, {})[arm] = new
        return old, new

    def save(self):
        if self.path:
            self.path.write_text(json.dumps(self.q, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# the arm channel
# ---------------------------------------------------------------------------
def publish(arm, script, dry_run=False):
    """Hand the arm to the JobManager through scripts/publish-arm.sh."""
    if dry_run:
        return True
    try:
        subprocess.run(
            [str(script), arm], check=True, capture_output=True, text=True, timeout=60
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"  ! could not publish {arm}: {exc}", file=sys.stderr)
        return False


def applied_arm(namespace, since_seconds, want_slices=None):
    """
    The arm the JobManager really used on the last assignment, read from its log.

    The published arm and the applied arm can differ: an arm published while no
    rescale happens is never applied, and the assigner only has a choice when
    the pool holds more slots than the job needs. Attributing a reward to the
    published arm instead of the applied one would silently poison the Q-table.

    `want_slices` is the parallelism the job was actually MEASURED at. The adaptive
    scheduler re-invokes the assigner speculatively, so the last line in the window
    is often a later what-if for a different width, whose freeSlots and had_choice
    describe a placement that never ran. Anchoring on the last line whose slice
    count matches what was measured keeps every field of the returned record —
    slices, freeSlots, had_choice — describing ONE assignment. Without it the
    caller mixes sources and can gate crediting on the wrong invocation.
    """
    try:
        pods = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", namespace,
                "-l", "component=jobmanager",
                "--field-selector=status.phase=Running",
                "--sort-by=.metadata.creationTimestamp",
                "-o", "jsonpath={.items[-1:].metadata.name}",
            ],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not pods:
            return None
        logs = subprocess.run(
            ["kubectl", "logs", "-n", namespace, pods, f"--since={int(since_seconds)}s"],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None

    last = None
    matching = None
    for line in logs.splitlines():
        match = RX_ASSIGN.search(line)
        if match:
            last = match.groupdict()
            if want_slices is not None and int(last["slices"]) == int(want_slices):
                matching = last
    # Fall back to the last line only when nothing matches what actually ran, so a
    # changed log format degrades to the old behaviour instead of dropping epochs.
    last = matching or last
    if not last:
        return None
    return {
        "arm": last["strategy"],
        "delegate": last["delegate"] or "",
        "slices": int(last["slices"]),
        "free_slots": int(last["free"]),
        "tms_available": int(last["tms"]),
        "tms_used": int(last["used"]),
        # The assigner only has a real decision when it is offered more slots
        # than it has slices to place; otherwise every arm produces the same
        # placement and the episode teaches nothing.
        "had_choice": int(last["free"]) > int(last["slices"]),
    }


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def source_records(base, jid):
    """Cumulative records emitted by the source, in ONE REST call.

    Deliberately not `sample_once`: that walks every vertex and every subtask —
    around thirty REST calls — and firing it repeatedly at a JobManager that is
    in the middle of restarting the job would perturb the very recovery it is
    supposed to observe. The job overview already carries the source vertex's
    `write-records` counter, which is both cheaper and a better instrument: a
    counter integrates exactly, where sampled rates have to be interpolated.
    """
    data = rest(base, f"/jobs/{jid}")
    if not data:
        return None
    vertices = data.get("vertices") or []
    if not vertices:
        return None
    value = (vertices[0].get("metrics") or {}).get("write-records")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recovery_of(trace, steady_rps):
    """What the rescale COST, read off the throughput curve during the warmup.

    Every campaign so far measured only the steady state after a rescale, because
    the warmup exists to keep rep 1 from being colder than rep N. That is correct
    for comparing placements, but it means the price of the rescale itself — the
    job stops, restores state from the checkpoint, and refills the pipeline — has
    never appeared in any number. A scheduler cannot decide WHETHER a rescale is
    worth doing without it: the benefit of a better placement is a rate, the cost
    of getting there is a one-off, and the two are only comparable once both are
    measured.

    Three quantities, in increasing usefulness:
      * restart_gap_s  — until the source produces anything at all again.
      * recovery_s     — until it reaches 90% of the steady rate it will settle at.
      * deficit        — events NOT emitted versus a job that had never stopped.
                         This is the one a cost/benefit gate needs, because dividing
                         it by the throughput a better placement gains gives the
                         seconds the rescale takes to pay for itself.

    `trace` holds (seconds since the rescale, cumulative source records). Rates come
    from differences between consecutive samples; a negative difference means the
    counter was reset by a restart, and that interval is dropped rather than
    counted as negative production.

    `recovery_s` stays empty when the job never reaches 90% within the warmup —
    which is itself a finding, not a missing value.
    """
    empty = {"restart_gap_s": "", "recovery_s": "",
             "rescale_deficit_events": "", "recovery_samples": 0}
    # Two samples are the minimum that define an interval. With fewer, the honest
    # answer is "not measured" — reporting a deficit of 0.0 would assert the rescale
    # was free, which is a claim, where missing data is not.
    if len(trace) < 2 or not steady_rps or steady_rps <= 0:
        return empty

    restart_gap, recovery, deficit = "", "", 0.0
    previous_t, previous_records = trace[0]
    for elapsed, records in trace[1:]:
        span = elapsed - previous_t
        produced = records - previous_records
        previous_t, previous_records = elapsed, records
        if span <= 0 or produced < 0:
            continue
        rate = produced / span
        if restart_gap == "" and rate > 0.05 * steady_rps:
            restart_gap = round(elapsed, 1)
        # The integral STOPS at recovery on purpose. The trace keeps running into the
        # steady state and, when an epoch is detected late, on into the NEXT rescale's
        # transient — which would otherwise be charged to this epoch. Once the rate is
        # back the deficit is closed by definition.
        if recovery != "":
            break
        deficit += max(0.0, steady_rps * span - produced)
        if rate >= 0.90 * steady_rps:
            recovery = round(elapsed, 1)

    return {"restart_gap_s": restart_gap,
            "recovery_s": recovery,
            "rescale_deficit_events": round(deficit, 1),
            "recovery_samples": len(trace)}


CSV_FIELDS = [
    "epoch", "timestamp", "job_id", "arm_applied", "had_choice", "delegate",
    "slices", "free_slots", "tms_available", "tms_used", "tms_hosting",
    "busy_mean_ms_s", "cv_busy_all", "cv_busy_hosting", "backpressure_mean_ms_s",
    "idle_mean_ms_s", "source_out_rps", "sink_in_rps", "throughput_per_slot",
    "e2e_delay_ms", "e2e_delay_spread_ms",
    "state", "reward", "creditable", "credit_note", "q_before", "q_after",
    "arm_next", "exploring", "samples",
    # What the rescale itself cost, sampled during the warmup instead of slept through.
    "restart_gap_s", "recovery_s", "rescale_deficit_events", "recovery_samples",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rest", default=DEFAULT_REST, help="Flink REST base URL")
    parser.add_argument("--namespace", default="flink")
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                        help="comma-separated arms the learner may publish")
    parser.add_argument("--meta", choices=["sarsa", "bandit"], default="sarsa",
                        help="sarsa = contextual (Flink-metric state x arm); "
                             "bandit = UCB1, state-blind")
    parser.add_argument("--fixed-arm", default=None,
                        help="publish this arm and never learn (per-arm baseline runs)")
    parser.add_argument("--observe", action="store_true",
                        help="measure and record, but never write the arm file")
    parser.add_argument("--warmup", type=float, default=60.0,
                        help="seconds to discard after a rescale, while the job restores state")
    parser.add_argument("--window", type=float, default=60.0,
                        help="seconds of steady state the reward is averaged over")
    parser.add_argument("--sample-interval", type=float, default=10.0)
    parser.add_argument("--poll-interval", type=float, default=5.0,
                        help="how often to check for a new placement epoch")
    parser.add_argument("--min-busy-ms-s", type=float, default=5.0,
                        help="below this mean busy time the epoch is too idle to "
                             "credit to an arm (dispersion of near-zero load is noise)")
    parser.add_argument("--reward", choices=["dispersion", "throughput", "blend"],
                        default="dispersion")
    parser.add_argument("--blend-weight", type=float, default=0.5,
                        help="weight of throughput when --reward blend")
    parser.add_argument("--throughput-scale", type=float, default=20000.0,
                        help="records/s per slot that count as reward 1.0")
    parser.add_argument("--alpha", type=float, default=0.3)
    parser.add_argument("--gamma", type=float, default=0.9)
    parser.add_argument("--epsilon", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out-dir", default="results/arm-controller")
    parser.add_argument("--qtable", default=None,
                        help="Q-table json (default: <out-dir>/qtable.json)")
    parser.add_argument("--max-epochs", type=int, default=0,
                        help="stop after this many measured epochs (0 = run forever)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    bins = {
        "saturation": (0.30, 0.70),
        "imbalance": (0.15, 0.40),
        "backpressure": (0.05, 0.25),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"episodes-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    qtable_path = (Path(args.qtable) if args.qtable
                   else out_dir / f"{args.meta}-table.json")
    publish_script = Path(__file__).resolve().parent / "publish-arm.sh"

    # A fixed-arm or observe run must not write a table: those runs exist to measure arms, and
    # letting them leak episodes into the learned policy would contaminate the comparison.
    table_path = None if args.fixed_arm or args.observe else qtable_path
    learner = (Ucb1(arms, table_path) if args.meta == "bandit"
               else Sarsa(arms, args.alpha, args.gamma, args.epsilon, table_path))

    if rest(args.rest, "/overview") is None:
        print(f"ERROR: no Flink REST API at {args.rest}", file=sys.stderr)
        print("       kubectl port-forward -n flink svc/flink-jobmanager 8081:8081",
              file=sys.stderr)
        return 1

    mode = ("observe" if args.observe else
            f"fixed arm {args.fixed_arm}" if args.fixed_arm else
            f"{args.meta.upper()} over {arms}")
    print("=" * 60)
    print(f"  Arm controller — {mode}")
    print(f"  reward={args.reward}  warmup={args.warmup:.0f}s  window={args.window:.0f}s")
    print(f"  episodes -> {csv_path}")
    print("=" * 60)

    if args.fixed_arm:
        publish(args.fixed_arm, publish_script, dry_run=args.observe)

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        handle.flush()

        epoch = 0
        current_key = None
        epoch_started = 0.0
        measured = True
        previous = None  # (state, arm, reward) of the last measured epoch
        # (absolute time, cumulative source records), NEVER reset on an epoch change.
        #
        # Resetting it was the bug: the controller only notices a rescale when it next
        # polls `epoch_key`, and it does not poll while `measure` holds the measurement
        # window, so a rescale landing inside that window is seen up to `window` seconds
        # late. Anchoring the trace to the moment of DETECTION therefore threw away
        # exactly the transient it exists to capture — and did so precisely on the
        # creditable epochs. Keeping one rolling history and slicing it by the epoch's
        # own start time makes late detection harmless.
        records_history = []

        while True:
            jid = running_job(args.rest)
            if jid is None:
                current_key = None
                measured = True
                time.sleep(args.poll_interval)
                continue

            key = epoch_key(args.rest, jid)
            if key is not None and key != current_key:
                current_key = key
                epoch_started = epoch_started_at(key, time.time())
                measured = False
                epoch += 1
                print(f"\n[epoch {epoch}] placement changed at "
                      f"{time.strftime('%H:%M:%S', time.localtime(epoch_started))} "
                      f"— warming up {args.warmup:.0f}s")

            if measured or time.time() - epoch_started < args.warmup:
                # The warmup is not dead time: it is the recovery curve. Sampling it
                # changes nothing about when the measurement window starts, so the
                # comparison between arms is untouched, but it turns the transient
                # from something deliberately excluded into something recorded.
                # Sampled unconditionally, including after the window has been
                # measured: that stretch is the run-up to the NEXT rescale, and
                # leaving it blind would lose the start of the next transient the
                # same way anchoring to detection did.
                produced = source_records(args.rest, jid)
                if produced is not None:
                    records_history.append((time.time(), produced))
                time.sleep(args.poll_interval)
                continue

            # Keep only what any future epoch could still need; a campaign runs for
            # hours and the history is appended to every second.
            horizon = time.time() - 10 * 60
            records_history[:] = [(t, r) for t, r in records_history if t >= horizon]

            taskmanagers = registered_taskmanagers(args.rest)
            measurement = measure(args.rest, jid, taskmanagers,
                                  args.window, args.sample_interval, args.verbose,
                                  trace=records_history,
                                  trace_interval=args.poll_interval)
            measured = True
            if measurement is None:
                print("  (no metrics in this window — skipping the epoch)")
                continue

            # Look back past the start of the epoch: the assignment is logged
            # just before the vertices start, so a lookback that ends exactly at
            # the epoch boundary misses the very line it is after.
            since = time.time() - epoch_started + 120
            assignment = applied_arm(args.namespace, since,
                                      want_slices=measurement["slices"])
            if not assignment:
                # No assigner log line: either the fork is not deployed (a stock-Flink baseline
                # run) or the line fell outside the lookback. Rebuilding from REST keeps the
                # episode creditable instead of silently dropping it.
                assignment = assignment_from_rest(
                    args.rest, measurement, args.fixed_arm or "STOCK")
            arm = assignment.get("arm", args.fixed_arm or "UNKNOWN")
            state = discretise(measurement, bins)
            reward = reward_of(measurement, args.reward, args.blend_weight,
                               args.throughput_scale)

            # Two ways an epoch teaches nothing about the arm, both of which
            # would otherwise feed confident-looking noise into the Q-table:
            #   * the assigner was offered exactly as many slots as it had
            #     slices, so every arm would have produced this placement;
            #   * the job is essentially idle, and the dispersion of near-zero
            #     busy times is dominated by which TaskManager happened to log
            #     a millisecond of work.
            credit_note = ""
            if not assignment.get("had_choice"):
                credit_note = "the assigner had no choice (freeSlots == slices)"
            elif measurement["busy_mean"] < args.min_busy_ms_s:
                credit_note = (f"job too idle ({measurement['busy_mean']:.1f} < "
                               f"{args.min_busy_ms_s:.1f} ms/s busy)")
            creditable = not credit_note

            if args.fixed_arm or args.observe:
                next_arm, exploring, q_before, q_after = args.fixed_arm or "", False, "", ""
            else:
                next_arm, exploring = learner.choose(state)
                q_before = q_after = ""
                # SARSA needs the NEXT (state, arm) before it can back up the
                # PREVIOUS transition, so every update runs one epoch behind.
                if previous is not None:
                    prev_state, prev_arm, prev_reward = previous
                    q_before, q_after = learner.update(
                        prev_state, prev_arm, prev_reward, state, next_arm)
                    learner.save()

            slots = max(measurement["slices"], 1)
            row = {
                "epoch": epoch,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "job_id": jid,
                "arm_applied": arm,
                "had_choice": assignment.get("had_choice", ""),
                "delegate": assignment.get("delegate", ""),
                # Both from the SAME assignment record: pairing a measured slice
                # count with another invocation's freeSlots produced rows like
                # slices=4/free=8/had_choice=False, and had_choice is the gate that
                # decides whether the episode is credited at all.
                "slices": assignment.get("slices", measurement["slices"]),
                "free_slots": assignment.get("free_slots", ""),
                "tms_available": assignment.get("tms_available", measurement["tms_total"]),
                "tms_used": assignment.get("tms_used", ""),
                "tms_hosting": measurement["tms_hosting"],
                "busy_mean_ms_s": round(measurement["busy_mean"], 2),
                "cv_busy_all": round(measurement["cv_busy_all"], 4),
                "cv_busy_hosting": round(measurement["cv_busy_hosting"], 4),
                "backpressure_mean_ms_s": round(measurement["backpressure_mean"], 2),
                "idle_mean_ms_s": round(measurement["idle_mean"], 2),
                "e2e_delay_ms": ("" if measurement["e2e_delay_ms"] is None
                                 else round(measurement["e2e_delay_ms"], 1)),
                "e2e_delay_spread_ms": ("" if measurement["e2e_delay_spread_ms"] is None
                                        else round(measurement["e2e_delay_spread_ms"], 1)),
                "source_out_rps": round(measurement["source_out_rps"], 2),
                "sink_in_rps": round(measurement["sink_in_rps"], 2),
                "throughput_per_slot": round(measurement["source_out_rps"] / slots, 2),
                "state": state,
                "reward": round(reward, 4),
                "creditable": int(creditable),
                "credit_note": credit_note,
                "q_before": round(q_before, 4) if q_before != "" else "",
                "q_after": round(q_after, 4) if q_after != "" else "",
                "arm_next": next_arm,
                "exploring": int(exploring),
                "samples": measurement["samples"],
                # Measured against the steady rate this very epoch settled at, so a
                # slow epoch is not scored against another epoch's baseline.
                **recovery_of(
                    [(t - epoch_started, r) for t, r in records_history
                     if t >= epoch_started],
                    measurement["source_out_rps"]),
            }
            writer.writerow(row)
            handle.flush()

            print(f"  applied={arm:<12} choice={row['had_choice']}  state={state}")
            print(f"  busy={row['busy_mean_ms_s']:.0f} ms/s  cv={row['cv_busy_all']:.3f}  "
                  f"bp={row['backpressure_mean_ms_s']:.0f} ms/s  "
                  f"tput/slot={row['throughput_per_slot']:.0f}  reward={reward:.3f}")

            if not (args.fixed_arm or args.observe):
                if creditable:
                    previous = (state, arm, reward)
                else:
                    previous = None
                    print(f"  (not credited to the arm: {credit_note})")
                print(f"  publishing {next_arm}{' (exploring)' if exploring else ''}")
                publish(next_arm, publish_script)

            if args.max_epochs and epoch >= args.max_epochs:
                print("\nreached --max-epochs, stopping")
                break

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
