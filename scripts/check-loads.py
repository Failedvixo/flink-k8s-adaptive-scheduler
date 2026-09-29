#!/usr/bin/env python3
"""Does the published load vector describe THIS job, and does it describe it honestly?

Three failures have cost campaigns in this project, and none shows up in
`publish-loads.sh --read`, which prints a file without ever looking at a job:

  * THE IDS DO NOT MATCH. The vector is keyed by JobVertexID. The operators with an
    explicit uid() keep their id across submissions, but Q8 also contains two Map
    vertices that CoGroupedStreams inserts to tag the join inputs, and those get a
    hash instead. If a published id names no vertex of the running job, the fork's
    sliceLoads() finds no match for it and that slice silently falls back to weight
    1.0 — which is how every campaign up to 2026-09-19 ran LPT on unit weights while
    a perfectly good file sat on the node. (Checked 2026-09-20: the ids DO survive a
    change of parallelism, 8 of 8.)

  * A ZERO IS NOT A COST. busyTimeMsPerSecond comes from a millisecond-resolution
    timer around the mailbox loop, so an operator whose per-record work falls below
    that resolution reports exactly 0.0 — "cheap" and "free" print the same. The fork
    sums a slice's vertices, so a 0.0 is a claim that the slice stacks anywhere for
    free. Idle time tells the two apart: an operator that really does nothing is idle
    ~1000 ms/s, one that is merely cheap is idle much less.

  * THE COST IS A PROPERTY OF THE PLACEMENT, NOT OF THE OPERATOR. Busy time is wall
    clock occupancy, so the same operator reads roughly 4x higher on `tm-1-slow` than
    on `tm-3-fast`, and it also absorbs contention from whatever shares the machine.
    Feeding that straight back into the placement is circular. This is why the report
    is PER SUBTASK with the machine it ran on, and why it also prints the cost
    normalised by the published speed — what the operator would have cost on the
    fastest machine, which is the figure that can be compared across jobs.

Usage (needs a RUNNING job; measure at a width with slack, not at full occupancy):
    scripts/check-loads.py
    scripts/check-loads.py --samples 9 --interval 5
"""
import argparse
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac  # noqa: E402  (REST transport through the JobManager pod)

LOADS_FILE = "/var/thesis/loads"
SPEEDS_FILE = "/var/thesis/speeds"
GENERATOR_SUFFIX = "generatorBusyMsPerSecond"
METRICS = ["busyTimeMsPerSecond", "idleTimeMsPerSecond", "backPressuredTimeMsPerSecond",
           "numRecordsInPerSecond", "numRecordsOutPerSecond"]


def node_file(node, path):
    """The node's copy of a /var/thesis file as {key: float}, or {}."""
    for cmd in (["docker", "exec", "-i", node, "cat", path],
                ["minikube", "ssh", "-n", node, "--", f"sudo cat {path}"]):
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            out = {}
            for line in done.stdout.replace("\r", "").splitlines():
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
            if out:
                return out
    return {}


def subtask_machines(base, jid, vid):
    """{subtask index: taskmanager id} for the current attempt."""
    detail = ac.rest(base, f"/jobs/{jid}/vertices/{vid}") or {}
    out = {}
    for sub in detail.get("subtasks", []):
        tm = sub.get("taskmanager-id") or sub.get("host") or "?"
        out[sub.get("subtask", len(out))] = tm
    return out


def generator_metric(base, jid, vid):
    """The generator's own busy gauge, for legacy sources whose mailbox reports NaN."""
    listing = ac.rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/0/metrics") or []
    match = [m["id"] for m in listing if m.get("id", "").endswith(GENERATOR_SUFFIX)]
    return match[0] if match else None


def sweep(base, jid, plan, samples, interval):
    """Sample EVERY subtask of every vertex once per round, `samples` rounds apart.

    ROUND-MAJOR, NOT VERTEX-MAJOR (fixed 2026-09-20). The first version finished one
    vertex before starting the next, so the wall clock cost samples x interval PER
    VERTEX: a 20 x 15 s request on Q8's eight vertices ran for forty minutes, outlived
    the job's own 30-minute duration, and reported the sink as NaN. Worse than slow, it
    was wrong — each vertex was profiled in a different stretch of the job, so the
    vector was never a picture of one moment.

    Returns {(vid, subtask): {metric: median}}.
    """
    series = {}
    for entry in plan:
        wanted = METRICS + ([entry["gen"]] if entry["gen"] else [])
        for i in range(entry["par"]):
            series[(entry["vid"], i)] = {m: [] for m in wanted}
    for round_index in range(samples):
        for entry in plan:
            wanted = METRICS + ([entry["gen"]] if entry["gen"] else [])
            for i in range(entry["par"]):
                payload = ac.rest(base, f"/jobs/{jid}/vertices/{entry['vid']}/subtasks/{i}"
                                        f"/metrics?get={','.join(wanted)}") or []
                for metric in payload:
                    try:
                        value = float(metric["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if math.isfinite(value) and metric["id"] in series[(entry["vid"], i)]:
                        series[(entry["vid"], i)][metric["id"]].append(value)
        if round_index < samples - 1:
            time.sleep(interval)

    out = {}
    for key, per_metric in series.items():
        stats = {m: (statistics.median(v) if v else float("nan"))
                 for m, v in per_metric.items()}
        gen = next((m for m in per_metric if m.endswith(GENERATOR_SUFFIX)), None)
        # THE GENERATOR'S GAUGE WINS OUTRIGHT where it exists (2026-09-20). Flink's own
        # figure for a legacy source is not merely absent, it is WRONG: one auction
        # subtask reported -2648 ms/s while its twin reported 29. Falling back only on
        # NaN let that through, and a negative cost does not just mislead LPT, it
        # inverts it — the busiest slice becomes the lightest. Anything outside [0,1000]
        # is not a measurement of a second of wall clock either way.
        busy = stats.get(gen, float("nan")) if gen else stats.get("busyTimeMsPerSecond")
        if not (isinstance(busy, float) and math.isfinite(busy) and 0.0 <= busy <= 1000.0):
            busy = float("nan")
        stats["busyTimeMsPerSecond"] = busy
        out[key] = stats
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rest", default="http://localhost:8081")
    parser.add_argument("--jm-pod", default=None, help="default: the running JobManager")
    parser.add_argument("--node", default="minikube")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    pod = args.jm_pod
    if not pod:
        found = subprocess.run(
            ["kubectl", "get", "pods", "-n", "flink", "-l", "component=jobmanager",
             "--field-selector=status.phase=Running",
             "-o", "jsonpath={.items[-1:].metadata.name}"],
            capture_output=True, text=True)
        pod = found.stdout.strip()
    if pod:
        ac._JM_POD = pod

    jid = ac.running_job(args.rest)
    if not jid:
        print("ERROR: no hay job RUNNING que medir", file=sys.stderr)
        return 2
    detail = ac.rest(args.rest, f"/jobs/{jid}")
    if not detail:
        print("ERROR: el JobManager no respondió", file=sys.stderr)
        return 2

    file_costs = node_file(args.node, LOADS_FILE)
    speeds = node_file(args.node, SPEEDS_FILE)
    fastest = max(speeds.values()) if speeds else 1.0

    plan = []
    for vertex in detail["vertices"]:
        vid = vertex["id"]
        plan.append({"vid": vid,
                     "par": vertex.get("parallelism", 1),
                     "name": vertex.get("name", ""),
                     "gen": generator_metric(args.rest, jid, vid),
                     "machines": subtask_machines(args.rest, jid, vid)})

    span = args.samples * args.interval
    print(f"job {jid[:8]}  vértices {len(plan)}  "
          f"vector publicado: {len(file_costs)} entradas  "
          f"velocidades: {len(speeds)} máquinas")
    print(f"  {args.samples} rondas cada {args.interval:.0f}s sobre TODOS los vértices "
          f"— al menos {span/60:.0f} min")
    started = time.time()
    measured = sweep(args.rest, jid, plan, args.samples, args.interval)
    if not ac.running_job(args.rest):
        print("  ! el job terminó durante el muestreo — la medición está incompleta")
    print(f"  muestreo terminado en {(time.time() - started)/60:.1f} min")

    seen, unmatched, suspect, starved = set(), [], [], []
    for entry in plan:
        vid, par, name = entry["vid"], entry["par"], entry["name"]
        machines = entry["machines"]
        stats = {i: measured[(vid, i)] for i in range(par) if (vid, i) in measured}
        cost = file_costs.get(vid)
        seen.add(vid)
        if cost is None:
            unmatched.append(name)

        busy, normalised = [], []
        print()
        print(f"{name[:40]:40} par={par}  publicado="
              f"{'-' if cost is None else f'{cost:.1f}'}"
              f"{'   <- SIN ENTRADA EN EL VECTOR' if cost is None else ''}")
        print(f"    {'sub':>3} {'máquina':22} {'vel':>4} {'busy':>7} {'norm':>7} "
              f"{'idle':>7} {'bp':>6} {'in/s':>8} {'out/s':>8}")
        for i in sorted(stats):
            s = stats[i]
            tm = machines.get(i, "?")
            speed = speeds.get(tm, float("nan"))
            b = s.get("busyTimeMsPerSecond", float("nan"))
            # What it would have cost on the fastest machine: busy x speed / fastest.
            n = b * speed / fastest if math.isfinite(b) and math.isfinite(speed) else float("nan")
            if math.isfinite(b):
                busy.append(b)
            if math.isfinite(n):
                normalised.append(n)
            print(f"    {i:3} {tm[:22]:22} {speed:4.1f} {b:7.1f} {n:7.1f} "
                  f"{s.get('idleTimeMsPerSecond', float('nan')):7.1f} "
                  f"{s.get('backPressuredTimeMsPerSecond', float('nan')):6.1f} "
                  f"{s.get('numRecordsInPerSecond', float('nan')):8.0f} "
                  f"{s.get('numRecordsOutPerSecond', float('nan')):8.0f}")
            if s.get("backPressuredTimeMsPerSecond", 0) > 50:
                starved.append(f"{name} sub{i}")
            if (cost is not None and cost < 1.0
                    and s.get("idleTimeMsPerSecond", 1000) < 900
                    and s.get("numRecordsInPerSecond", 0) > 100):
                suspect.append(f"{name} sub{i}")
        if busy:
            spread = (max(busy) / min(busy)) if min(busy) > 0 else float("inf")
            print(f"    medido {sum(busy)/len(busy):7.1f} ms/s   normalizado "
                  f"{sum(normalised)/len(normalised) if normalised else float('nan'):7.1f}"
                  f"   dispersión entre subtareas x{spread:.1f}")

    stale = [v for v in file_costs if v not in seen]
    print()
    if unmatched:
        print("! vértices del job SIN costo publicado — el fork los suma como 0:")
        for name in unmatched:
            print(f"    {name}")
    if stale:
        print(f"! {len(stale)} entradas publicadas no corresponden a ningún vértice de")
        print("  este job: se midieron sobre OTRO grafo y esas slices pesarán 1.0")
    if suspect:
        print("! costo publicado ~0 pero el operador no está ocioso — barato, no gratis:")
        for name in suspect:
            print(f"    {name}")
    if starved:
        print("! con contrapresión al medir; el vector queda sesgado a la baja:")
        for name in starved:
            print(f"    {name}")
    if not (unmatched or stale or suspect or starved):
        print("vector consistente con el job en ejecución")
    return 0


if __name__ == "__main__":
    sys.exit(main())
