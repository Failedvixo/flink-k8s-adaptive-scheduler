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
import concurrent.futures
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
DEFAULT_ARMS = ["FCFS", "ROUND_ROBIN", "LEAST_LOADED", "LPT", "PACK", "ACO", "GA"]

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
# Set by main() when --jm-pod is given: every REST call then goes through
# `kubectl exec` into the JobManager instead of a host port-forward.
_JM_POD = None
_NAMESPACE = "flink"


def rest(base, path, timeout=5.0):
    """GET a JSON document from the Flink REST API, or None if it is unreachable.

    WHY THERE IS A POD TRANSPORT AT ALL (2026-09-03). This controller was the last
    component still reaching Flink over `kubectl port-forward`, and that is how a
    campaign came to record three episodes out of eight measurements. The driver
    performed every rescale and logged HTTP 200 for each; the controller, polling a
    tunnel that had quietly died, never saw a placement change after the first
    minute and simply sat there. Nothing errored — the run looked healthy and the
    data was not collected. A dead tunnel is indistinguishable from a cluster where
    nothing happens, which is the worst possible failure for an observer.

    NOT retried, unlike every other call into this cluster, and that is deliberate.
    Elsewhere a lost call costs a measurement, so retrying is worth the wait. Here
    the controller polls in a loop, so a failed call is retried a second later by
    construction — while a retry INSIDE the call blinds it. Measured 2026-09-07:
    with three attempts at forty seconds each, one snapshot could stall for minutes
    and the controller registered two placement changes where the driver performed
    eight. Fail fast and let the loop handle it.
    """
    for attempt in range(1):
        if _JM_POD:
            try:
                out = subprocess.run(
                    ["kubectl", "exec", "-n", _NAMESPACE, _JM_POD, "--",
                     "curl", "-s", "-m", str(int(max(timeout, 5))),
                     f"http://localhost:8081{path}"],
                    capture_output=True, text=True, timeout=max(timeout, 5) + 10)
                if out.returncode == 0 and out.stdout.strip():
                    return json.loads(out.stdout)
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                pass
        else:
            try:
                with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
                pass
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
        "had_choice": (total_slots > slices
                       or (CHOICE_RULE == "permutation" and len(all_taskmanagers) > 1)),
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


def graph_roles(detail):
    """({source vertex ids}, {sink vertex ids}) from the job plan.

    WHY NOT `vertices[0]` AND `vertices[-1]`, which is what this file used to do.
    That holds while a job has exactly one source and one sink, which was true of
    the project's own generator: it emitted one tagged stream that the query then
    filtered. The reference Nexmark implementation has a source per event type, so
    Q8 has TWO — persons at a quarter of the rate and auctions at three quarters —
    and position 0 is whichever one Flink happens to list first.

    Measured 2026-09-07: a campaign at 6500 rec/s recorded `source_out_rps` of
    about 1450, i.e. a quarter of the offered load, because it was reading the
    person branch alone. The auction branch — three quarters of the traffic, and
    the one that feeds the join where placement actually bites — was invisible, so
    both arms looked identical in throughput. The same mistake had already been
    found and fixed in calibrate-rate.sh and was not carried across.

    A source is a plan node with no inputs; a sink is one that is nobody's input.
    """
    nodes = (detail.get("plan") or {}).get("nodes") or []
    if not nodes:
        return set(), set()
    sources = {n["id"] for n in nodes if not n.get("inputs")}
    consumed = {i["id"] for n in nodes for i in (n.get("inputs") or [])}
    sinks = {n["id"] for n in nodes if n["id"] not in consumed}
    return sources, sinks


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


# HOW MANY REST CALLS RUN AT ONCE. One snapshot asks for the placement of every
# vertex plus the metrics of every subtask — around two dozen calls for a job of
# eight vertices at parallelism two. Over plain HTTP that was milliseconds and the
# sequential loop below was free. Through `kubectl exec` each call costs one to two
# seconds of process spawn, so the same snapshot took the better part of a minute
# and the controller's 60-second measurement window stretched to many minutes.
# Measured 2026-09-06: it detected two placement changes where the driver performed
# eight, and the campaign recorded one episode per arm. The calls are independent,
# so they are issued together.
REST_WORKERS = 8

# "permutation": a placement across more than one machine is a decision even when
# the slot count is exactly the slice count. "slots": only surplus slots count.
# See the note on had_choice in applied_arm for why the default changed.
CHOICE_RULE = "permutation"


def fetch_parallel(jobs):
    """Run {key: (fn, args)} concurrently, returning {key: result}."""
    if not jobs:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=REST_WORKERS) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in jobs.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                out[key] = future.result()
            except Exception:
                out[key] = None
    return out


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


# Per-record latency (2026-10-08): gauges the reference sinks register through LatencyMeter.
# They live at OPERATOR scope, so their ids carry the operator's name as a prefix
# ("Sink__q8-sink.latencyP50Ms"); the ids are discovered once per subtask and cached.
LATENCY_GAUGES = ("latencyP50Ms", "latencyP99Ms", "latencyMeanMs", "latencySamples")
_latency_ids = {}


def sink_latency(base, jid, vid, index):
    """{gauge: value} for one sink subtask, or {} when the job has no LatencyMeter."""
    key = (jid, vid, index)
    if key not in _latency_ids:
        listing = rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/{index}/metrics") or []
        ids = [m["id"] for m in listing if m.get("id", "").split(".")[-1] in LATENCY_GAUGES]
        if not ids:
            return {}           # not cached: the gauges appear only once the sink has opened
        _latency_ids[key] = ids
    payload = rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/{index}/metrics"
                         f"?get={','.join(_latency_ids[key])}") or []
    out = {}
    for entry in payload:
        try:
            out[entry["id"].split(".")[-1]] = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
    _detail = rest(base, f"/jobs/{jid}") or {}
    source_ids, sink_ids = graph_roles(_detail)
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

    # Everything this snapshot needs, fetched at once rather than one call at a
    # time; the loop below then reads from memory and its logic is unchanged.
    placements = fetch_parallel({
        vid: (subtask_taskmanagers, (base, jid, vid))
        for vid, _name, _par in vertices})
    metrics = fetch_parallel({
        (vid, index): (subtask_metrics, (base, jid, vid, index))
        for vid, _name, parallelism in vertices
        for index in range(parallelism)})

    sink_vids = [vid for position, (vid, _n, _p) in enumerate(vertices)
                 if ((vid in sink_ids) if sink_ids else (position == len(vertices) - 1))]
    latencies = fetch_parallel({
        (vid, index): (sink_latency, (base, jid, vid, index))
        for vid, _name, parallelism in vertices if vid in sink_vids
        for index in range(parallelism)})
    # A subtask reports -1 when its last minute holds no result; only real readings count.
    lat_rows = [v for v in latencies.values()
                if v and v.get("latencySamples", 0) > 0 and v.get("latencyP50Ms", -1) >= 0]

    for position, (vid, _name, parallelism) in enumerate(vertices):
        placement = placements.get(vid) or {}
        for index in range(parallelism):
            values = metrics.get((vid, index))
            if values is None:
                values = {name: 0.0 for name in SUBTASK_METRICS}
            busy = values["busyTimeMsPerSecond"]
            subtask_busy.append(busy)
            subtask_backpressure.append(values["backPressuredTimeMsPerSecond"])
            subtask_idle.append(values["idleTimeMsPerSecond"])
            tm = placement.get(index)
            if tm is not None:
                busy_per_tm.setdefault(tm, 0.0)
                busy_per_tm[tm] += busy
                hosting.add(tm)
            if (vid in source_ids) if source_ids else (position == 0):
                source_out += values["numRecordsOutPerSecond"]
            if (vid in sink_ids) if sink_ids else (position == len(vertices) - 1):
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
        # Per-record latency across sink subtasks: p50 of the slowest subtask (the median
        # result of the worst sink), p99 of the worst, and the mean weighted by results.
        "latency_p50_ms": max(r["latencyP50Ms"] for r in lat_rows) if lat_rows else None,
        "latency_p99_ms": max(r["latencyP99Ms"] for r in lat_rows) if lat_rows else None,
        "latency_mean_ms": (sum(r["latencyMeanMs"] * r["latencySamples"] for r in lat_rows)
                            / sum(r["latencySamples"] for r in lat_rows)) if lat_rows else None,
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


_SPEEDS_CACHE = None


def taskmanager_speeds(node=None):
    """{taskmanager id: relative capacity} from /var/thesis/speeds, read once.

    FOR UTILISATION, NOT RAW BUSY TIME (2026-09-25). `cv_busy_hosting` is the dispersion of
    summed busy time across TaskManagers, and on a 4/2/1-core cluster that mostly measures the
    machines rather than the placement: its mean over 367 credited episodes is 0.81, it
    correlates +0.07 with throughput, and — decisively — repeating the SAME placement three
    times moved it by 92% while throughput moved 2.8%. Dividing by the machine's declared
    capacity is what turns it into utilisation, which is the quantity a balance penalty was
    always supposed to mean. Recorded so the question can be settled on data instead of
    argued: nothing consumes it yet.
    """
    global _SPEEDS_CACHE
    if _SPEEDS_CACHE is not None:
        return _SPEEDS_CACHE
    _SPEEDS_CACHE = {}
    node = node or os.environ.get("THESIS_NODE", "minikube")
    for cmd in (["docker", "exec", "-i", node, "cat", "/var/thesis/speeds"],
                ["minikube", "ssh", "-n", node, "--", "sudo cat /var/thesis/speeds"]):
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            for line in done.stdout.replace("\r", "").splitlines():
                parts = line.split()
                if len(parts) == 2:
                    try:
                        _SPEEDS_CACHE[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
            if _SPEEDS_CACHE:
                break
    return _SPEEDS_CACHE


def taskmanager_cpu_seconds(base, taskmanagers):
    """{taskmanager id: cumulative JVM CPU seconds}, from Status.JVM.CPU.Time.

    THE ONLY LOAD SIGNAL HERE THAT DOES NOT INHERIT THE busyTime PROBLEM (2026-09-27). Three
    attempts at a balance metric failed for the same root cause, each worse than the last:
    the slice weight is bounded at 1000 ms/s so a saturated operator reads cheap; the
    dispersion of summed busy time moved 92% between repetitions of one placement; and the
    capacity-normalised version reported `tm-1-slow` at 0.0 utilisation while that machine was
    hosting six of the eight slices, because the legacy sources it hosted report NaN and NaN
    sums to zero.

    This is measured by the JVM, not by the task mailbox, so it covers EVERY thread — the
    generator thread of a legacy source included — cannot be NaN for a badly instrumented
    operator, and being cumulative it has no ceiling: the difference across a window is
    honest CPU seconds. Divided by the machine's cores it is a utilisation in [0,1], which is
    what a load-balance penalty needed from the start.
    """
    out = {}
    for tm in taskmanagers:
        payload = rest(base, f"/taskmanagers/{tm}/metrics?get=Status.JVM.CPU.Time")
        for entry in payload or []:
            try:
                out[tm] = float(entry["value"]) / 1e9
            except (KeyError, TypeError, ValueError):
                pass
    return out


def measure(base, jid, all_taskmanagers, window, interval, verbose=False,
            trace=None, trace_interval=1.0):
    """
    Average several snapshots over the measurement window.

    A single snapshot of `busyTimeMsPerSecond` is a one-second average and is
    noisy enough to reorder two arms on its own, so the window is what the
    reward is actually computed from.
    """
    samples = []
    window_start = time.time()
    cpu_at_start = taskmanager_cpu_seconds(base, all_taskmanagers)
    deadline = window_start + window
    while time.time() < deadline:
        snapshot = sample_once(base, jid, all_taskmanagers)
        if snapshot:
            snapshot["t"] = time.time()
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
    speeds = taskmanager_speeds()
    # Real utilisation: CPU seconds actually burnt, over the wall clock of the window, over the
    # machine's cores. The speeds file is declared proportional to cores and on this cluster it
    # is literally 1/2/4, so it doubles as the core count.
    cpu_at_end = taskmanager_cpu_seconds(base, all_taskmanagers)
    elapsed = max(1e-6, time.time() - window_start)
    cpu_util = {}
    for tm, cores in speeds.items():
        if tm in cpu_at_start and tm in cpu_at_end and cores:
            cpu_util[tm] = max(0.0, (cpu_at_end[tm] - cpu_at_start[tm]) / (elapsed * cores))
    utilisation = {tm: busy / speeds[tm]
                   for tm, busy in busy_per_tm.items() if speeds.get(tm)}

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
        "window_start": window_start,
        "window_end": time.time(),
        "series": [(s["t"], s["source_out_rps"], s["backpressure_mean"], s["busy_mean"])
                   for s in samples],
        "e2e_delay_ms": mean_where_present("e2e_delay_ms"),
        "latency_p50_ms": mean_where_present("latency_p50_ms"),
        "latency_p99_ms": mean_where_present("latency_p99_ms"),
        "latency_mean_ms": mean_where_present("latency_mean_ms"),
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
        # The capacity-normalised version of the same idea, plus the raw per-machine figures
        # so a later analysis can define balance differently without re-running anything.
        "cv_util_hosting": coefficient_of_variation(
            [u for tm, u in utilisation.items() if tm in hosting]
        ) if utilisation else 0.0,
        "util_per_tm": "|".join(f"{tm}:{u:.1f}" for tm, u in sorted(utilisation.items())),
        "cv_cpu_util": coefficient_of_variation(
            [u for tm, u in cpu_util.items() if tm in hosting]) if cpu_util else 0.0,
        "cpu_util_per_tm": "|".join(f"{tm}:{u:.3f}" for tm, u in sorted(cpu_util.items())),
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
        # WHAT COUNTS AS A DECISION, and this rule was too strict until 2026-09-07.
        #
        # Surplus slots are one kind of freedom: with more slots than slices the
        # assigner chooses how many go on each machine. But it is not the only kind.
        # When the slots exactly match the slices the per-machine COUNTS are forced
        # — and WHICH SLICE lands on WHICH MACHINE is not. Under PER_STAGE the
        # slices are not interchangeable: one carries the stateful join, another the
        # sink that does almost nothing, and putting the join on the 4-core machine
        # instead of the 1-core one is the whole question this thesis asks.
        #
        # Two submissions of the same arm on 2026-09-07 produced the same spread
        # {slow=6, medium=4, fast=2} and different mappings — slices #2 and #6 on
        # `fast` in one, #3 and #7 in the other. The old rule discarded both as
        # "no choice", which is how a campaign can measure real decisions all day
        # and credit none of them.
        #
        # The permutation only stops mattering when the slices ARE interchangeable,
        # which is the SHARED case: there every slice holds the whole pipeline and
        # any two are the same. Pass --choice-rule slots to recover the old,
        # stricter behaviour for those runs.
        "had_choice": (int(last["free"]) > int(last["slices"])
                       or (CHOICE_RULE == "permutation" and int(last["tms"]) > 1)),
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
    sources, _sinks = graph_roles(data)
    total = 0.0
    seen = False
    for v in vertices:
        if sources and v["id"] not in sources:
            continue
        try:
            total += float((v.get("metrics") or {}).get("write-records"))
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def window_stability(trace, start, end, bin_s=10.0):
    """Throughput INSIDE the window, from the cumulative source counter, in bins.

    WHY (2026-09-18). Two consecutive ten-minute windows of the same job, with the same
    placement, differed by a median of 7% and up to 16%; with one-minute windows the
    median was ~20% and some fell to half their neighbours. The episode kept only the
    window's mean, so there was no way to tell a steady 30000 from a window that ran at
    35000 and collapsed to 15000 for two minutes. The counter is already sampled every
    second for the recovery curve; binning it costs nothing and shows the shape.

    A counter that goes BACKWARDS is a task restart inside the window and ends the series:
    after it the counts belong to a different run of the job.
    """
    points = [(t, r) for t, r in trace if start <= t <= end]
    bins = []
    edge = start
    while edge + bin_s <= end + 1e-6:
        inside = [(t, r) for t, r in points if edge <= t <= edge + bin_s]
        if len(inside) >= 2 and inside[-1][1] >= inside[0][1]:
            span = inside[-1][0] - inside[0][0]
            if span > 0:
                bins.append((round(edge - start, 1), (inside[-1][1] - inside[0][1]) / span))
        edge += bin_s
    rates = [r for _, r in bins]
    total = None
    if len(points) >= 2 and points[-1][1] >= points[0][1] and points[-1][0] > points[0][0]:
        total = (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])
    return {
        "bins": bins,
        "rps_counter": round(total, 1) if total is not None else "",
        "rps_median": round(statistics.median(rates), 1) if rates else "",
        "rps_cv": round(coefficient_of_variation(rates), 4) if len(rates) > 1 else "",
        "rps_min_bin": round(min(rates), 1) if rates else "",
    }


def checkpoints_in(base, jid, start, end):
    """The checkpoints TRIGGERED inside the window, from the REST history.

    At the throughput ceiling every checkpoint upload competes with the pipeline for the
    same CPU and disk, so a slow or failed one is a candidate explanation for a window that
    dips. Recorded per window so that can be checked against the rate series rather than
    assumed. Flink keeps only a short history (web.checkpoints.history, 10 by default), so
    in a long window the earliest checkpoints can be missing — the count is a lower bound.
    """
    data = rest(base, f"/jobs/{jid}/checkpoints") or {}
    rows = []
    for c in data.get("history") or []:
        trigger = (c.get("trigger_timestamp") or 0) / 1000.0
        if start <= trigger <= end:
            rows.append({
                "id": c.get("id"),
                "t": round(trigger - start, 1),
                "status": c.get("status", ""),
                "duration_ms": c.get("end_to_end_duration", ""),
                "size_bytes": c.get("checkpointed_size", c.get("state_size", "")),
            })
    durations = [r["duration_ms"] for r in rows if isinstance(r["duration_ms"], (int, float))]
    return rows, {
        "ckpt_count": len(rows),
        "ckpt_failed": sum(1 for r in rows if r["status"] == "FAILED"),
        "ckpt_duration_mean_ms": round(statistics.fmean(durations), 1) if durations else "",
        "ckpt_duration_max_ms": max(durations) if durations else "",
    }


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
    "busy_mean_ms_s", "cv_busy_all", "cv_busy_hosting", "cv_util_hosting",
    "util_per_tm", "cv_cpu_util", "cpu_util_per_tm", "backpressure_mean_ms_s",
    "idle_mean_ms_s", "source_out_rps", "sink_in_rps", "throughput_per_slot",
    "e2e_delay_ms", "e2e_delay_spread_ms",
    "latency_p50_ms", "latency_p99_ms", "latency_mean_ms",
    "state", "reward", "creditable", "credit_note", "q_before", "q_after",
    "arm_next", "exploring", "samples",
    # What the rescale itself cost, sampled during the warmup instead of slept through.
    "restart_gap_s", "recovery_s", "rescale_deficit_events", "recovery_samples",
    # Inside the window (2026-09-18): the shape of the throughput, not only its mean, and
    # the checkpoints that ran while it was measured.
    "rps_counter", "rps_median", "rps_cv", "rps_min_bin",
    "ckpt_count", "ckpt_failed", "ckpt_duration_mean_ms", "ckpt_duration_max_ms",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rest", default=DEFAULT_REST, help="Flink REST base URL")
    parser.add_argument("--choice-rule", choices=["permutation", "slots"],
                        default=os.environ.get("CHOICE_RULE", "permutation"),
                        help="what counts as the assigner having had a decision")
    parser.add_argument("--jm-pod", default=os.environ.get("FLINK_JM_POD", ""),
                        help="JobManager pod: REST goes through `kubectl exec` "
                             "instead of a port-forward, which cannot die silently")
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
    # SKIP THE EPOCHS THAT ARE NOT THE MEASURED STEP (2026-09-23, after it cost two runs of
    # three hours each). The controller measures epochs SEQUENTIALLY, spending a full
    # warmup+window on each. With SCHEDULE="3 2*" the submission's placement at parallelism 3
    # is an epoch like any other, so the controller burned 960 s on a width nobody analyses
    # and only THEN began warming up the measured one — which therefore needed the cell to
    # last 2 x (warmup+window), while the driver holds warmup+window+margin. Measured
    # 2026-09-23: epoch 1 at 17:02:17, the rescale at 17:10:50, the controller free at
    # 17:18:20, the cell over at 17:33:52 — twenty-eight seconds short, six cells in a row.
    #
    # The 2026-09-07 attempt to skip epochs failed because it used `slots-available`, which is
    # zero by design. The width is not: it is in the epoch key the controller already computes.
    parser.add_argument("--only-parallelism", default=None,
                        help="comma-separated list of parallelisms to measure; epochs at any "
                             "other width are skipped without spending a window")
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

    # Pick the transport before anything reads metrics. With a pod, REST goes
    # through `kubectl exec`; without one, the old port-forward path stays, so
    # nothing that used to work stops working.
    global _JM_POD, _NAMESPACE, CHOICE_RULE
    CHOICE_RULE = args.choice_rule
    _NAMESPACE = args.namespace
    _JM_POD = args.jm_pod or None
    if _JM_POD:
        print(f"  REST via kubectl exec -> {_JM_POD}")

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

        _measured_widths = {int(x) for x in str(args.only_parallelism).replace(",", " ").split()
                            } if args.only_parallelism else set()
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
                epoch += 1
                width = max((p for _, p, _ in key), default=0)
                # `measured` doubles as "nothing left to do in this epoch", so setting it
                # here makes the sampling branch below skip the epoch at no cost.
                measured = bool(_measured_widths and width not in _measured_widths)
                if measured:
                    print(f"\n[epoch {epoch}] paralelismo {width}, no esta entre los pasos "
                          f"medidos ({sorted(_measured_widths)}) — se omite sin gastar ventana",
                          flush=True)

                # The 2026-09-07 version of this skip asked `slots-available` whether the
                # assigner had a choice, and that figure is zero by design —
                # slot.idle.timeout holds released slots inside the job so the pool
                # geometry stays still — so it skipped every epoch and credited nothing.
                # The width above is the signal that works.
                else:
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
            # The controller does not poll the epoch while it measures, so a restart inside
            # the window is only visible afterwards: either the vertices' start times moved,
            # or the cumulative source counter stepped backwards.
            in_window = [r for t, r in records_history
                         if measurement["window_start"] <= t <= measurement["window_end"]]
            restarted_in_window = (
                epoch_key(args.rest, jid) != current_key
                or any(b < a for a, b in zip(in_window, in_window[1:])))

            # Look back past the start of the epoch: the assignment is logged
            # just before the vertices start, so a lookback that ends exactly at
            # the epoch boundary misses the very line it is after.
            since = time.time() - epoch_started + 120
            assignment = applied_arm(args.namespace, since,
                                      want_slices=measurement["slices"])
            reconstructed = False
            if not assignment:
                # No assigner log line for this epoch. That has an innocent reading —
                # the fork is not deployed, or the line fell outside the lookback —
                # and a fatal one, found 2026-09-07: THE ASSIGNER NEVER RAN. Flink's
                # adaptive scheduler reduces parallelism by dropping subtasks from
                # the slots it already holds, without asking for a new placement, so
                # a scale-down step produces no decision at all. The campaign's
                # "measured" step was then the SUBMISSION's placement running
                # narrower, and this fallback recorded it as though the arm had
                # chosen it.
                #
                # Rebuilding from REST still happens, because the measurement itself
                # is real and worth keeping, but the episode is marked: an epoch in
                # which no placement was decided cannot say anything about the arm
                # that would have decided it.
                assignment = assignment_from_rest(
                    args.rest, measurement, args.fixed_arm or "STOCK")
                reconstructed = True
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
            if reconstructed:
                # No assigner line for this epoch, so no placement was decided in
                # it. Whatever the job is running, the arm under test did not choose
                # it here — most likely it is the previous decision still in force.
                credit_note = ("no assignment was logged for this epoch — the "
                               "placement was inherited, not decided")
            elif not assignment.get("had_choice"):
                credit_note = "the assigner had no choice (freeSlots == slices)"
            elif measurement["busy_mean"] < args.min_busy_ms_s:
                credit_note = (f"job too idle ({measurement['busy_mean']:.1f} < "
                               f"{args.min_busy_ms_s:.1f} ms/s busy)")
            elif restarted_in_window:
                # A RESTART INSIDE THE WINDOW (2026-09-18). A failed checkpoint fails the
                # job while the default tolerates none, and the window then averages a
                # stretch at zero and a recovery ramp into the placement's throughput:
                # measured, one window went 38000 -> 0 -> 8392 -> ... -> 34912 and was
                # recorded as an ordinary episode. That is the checkpoint's result, not
                # the arm's.
                credit_note = "the job restarted inside the measurement window"
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
                "cv_util_hosting": round(measurement.get("cv_util_hosting", 0.0), 4),
                "util_per_tm": measurement.get("util_per_tm", ""),
                "cv_cpu_util": round(measurement.get("cv_cpu_util", 0.0), 4),
                "cpu_util_per_tm": measurement.get("cpu_util_per_tm", ""),
                "backpressure_mean_ms_s": round(measurement["backpressure_mean"], 2),
                "idle_mean_ms_s": round(measurement["idle_mean"], 2),
                "e2e_delay_ms": ("" if measurement["e2e_delay_ms"] is None
                                 else round(measurement["e2e_delay_ms"], 1)),
                "e2e_delay_spread_ms": ("" if measurement["e2e_delay_spread_ms"] is None
                                        else round(measurement["e2e_delay_spread_ms"], 1)),
                **{k: ("" if measurement.get(k) is None else round(measurement[k], 1))
                   for k in ("latency_p50_ms", "latency_p99_ms", "latency_mean_ms")},
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
            stability = window_stability(records_history, measurement["window_start"],
                                         measurement["window_end"])
            checkpoints, ckpt_summary = checkpoints_in(args.rest, jid,
                                                       measurement["window_start"],
                                                       measurement["window_end"])
            row.update({k: v for k, v in stability.items() if k != "bins"})
            row.update(ckpt_summary)
            # Side files, one per window, so the shape can be plotted against the checkpoints.
            with (out_dir / f"window-epoch{epoch:03d}.csv").open("w", newline="") as side:
                w = csv.writer(side)
                w.writerow(["t_s", "rps_counter_bin", "rps_gauge", "backpressure_ms_s",
                            "busy_ms_s"])
                gauge = {round(t - measurement["window_start"], 1): (r, bp, b)
                         for t, r, bp, b in measurement["series"]}
                for t, rate in stability["bins"]:
                    nearest = min(gauge, key=lambda g: abs(g - t)) if gauge else None
                    r, bp, b = gauge[nearest] if nearest is not None else ("", "", "")
                    w.writerow([t, round(rate, 1), round(r, 1) if r != "" else "",
                                round(bp, 1) if bp != "" else "", round(b, 1) if b != "" else ""])
            if checkpoints:
                with (out_dir / f"checkpoints-epoch{epoch:03d}.csv").open("w", newline="") as side:
                    w = csv.DictWriter(side, fieldnames=list(checkpoints[0]))
                    w.writeheader()
                    w.writerows(checkpoints)
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
