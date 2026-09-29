#!/usr/bin/env python3
"""What is actually limiting this job? Ask before assuming it is the operators.

WHY (2026-09-02). Profiling q8 at 60000 rec/s — 80% of the calibrated ceiling —
found the busiest vertex at 116 ms/s out of 1000, i.e. 12% utilisation, while the
source reported 220 ms/s of backpressure just above the ceiling. Something stalls
the pipeline about a fifth of the time and it is not any operator's CPU. Until that
is identified, a placement campaign on this configuration would be measuring
whatever it is, not the placement.

Reports, for a running job:
  * per-vertex busy / backpressure / idle, which together account for a subtask's
    second: busy is work, backpressure is downstream refusing, idle is nothing to
    do. A pipeline where everything is idle and the source is backpressured has its
    bottleneck OUTSIDE the operators;
  * checkpoint statistics, the first suspect here — RocksDB incremental checkpoints
    to MinIO on a laptop can stall the pipeline without any operator looking busy;
  * the source's own output rate against what was asked, to separate "the job is
    slow" from "the generator cannot produce".

Usage:
    python3 scripts/diagnose-bottleneck.py --pod <jm-pod> [--job <jid>]
"""

import argparse
import json
import subprocess
import sys

METRICS = ["busyTimeMsPerSecond", "backPressuredTimeMsPerSecond",
           "idleTimeMsPerSecond", "numRecordsInPerSecond", "numRecordsOutPerSecond"]


def curl(pod, namespace, path):
    try:
        out = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod, "--", "curl", "-s", "-m", "15",
             f"http://localhost:8081{path}"],
            capture_output=True, text=True, timeout=40).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pod", required=True)
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--job")
    args = ap.parse_args()

    jid = args.job
    if not jid:
        jobs = curl(args.pod, args.namespace, "/jobs") or {}
        running = [j["id"] for j in jobs.get("jobs", []) if j.get("status") == "RUNNING"]
        if not running:
            sys.exit("no RUNNING job")
        jid = running[0]

    detail = curl(args.pod, args.namespace, f"/jobs/{jid}")
    if not detail:
        sys.exit("could not read the job")

    print(f"job {jid}  state={detail.get('state')}")
    print()
    print(f"{'vertex':34} {'par':>3} {'busy':>6} {'bp':>6} {'idle':>6} "
          f"{'reg/s in':>10} {'reg/s out':>10}")
    print("-" * 84)
    for v in detail.get("vertices", []):
        m = curl(args.pod, args.namespace,
                 f"/jobs/{jid}/vertices/{v['id']}/subtasks/metrics"
                 f"?get={','.join(METRICS)}")
        got = {x["id"]: x for x in m} if m else {}

        def agg(key, field="avg"):
            try:
                return float(got[key][field])
            except (KeyError, TypeError, ValueError):
                return float("nan")

        fmt = lambda x, w, p=0: (f"{x:{w}.{p}f}" if x == x else f"{'—':>{w}}")
        print(f"{v.get('name', '')[:34]:34} {v.get('parallelism', 0):>3} "
              f"{fmt(agg('busyTimeMsPerSecond'), 6)} "
              f"{fmt(agg('backPressuredTimeMsPerSecond'), 6)} "
              f"{fmt(agg('idleTimeMsPerSecond'), 6)} "
              f"{fmt(agg('numRecordsInPerSecond', 'sum'), 10)} "
              f"{fmt(agg('numRecordsOutPerSecond', 'sum'), 10)}")

    print()
    print("Los tres primeros suman ~1000 ms/s por subtarea: busy = trabajo,")
    print("bp = el de abajo no acepta, idle = no hay nada que hacer.")

    cp = curl(args.pod, args.namespace, f"/jobs/{jid}/checkpoints")
    if not cp:
        print("\n(sin estadísticas de checkpoint)")
        return
    counts = cp.get("counts", {})
    latest = (cp.get("latest") or {}).get("completed") or {}
    summary = cp.get("summary", {})

    def stat(block, field="avg"):
        try:
            return summary[block][field]
        except (KeyError, TypeError):
            return None

    print()
    print("CHECKPOINTS")
    print(f"  completados={counts.get('completed')}  fallidos={counts.get('failed')}"
          f"  en curso={counts.get('in_progress')}  restaurados={counts.get('restored')}")
    dur, size = stat("end_to_end_duration"), stat("state_size")
    if dur is not None:
        print(f"  duración media={dur/1000:.1f}s  máx={stat('end_to_end_duration','max')/1000:.1f}s")
    if size is not None:
        print(f"  tamaño medio={size/1024/1024:.1f} MB")
    if latest:
        print(f"  último: {latest.get('end_to_end_duration', 0)/1000:.1f}s, "
              f"{latest.get('state_size', 0)/1024/1024:.1f} MB, "
              f"alineación={latest.get('alignment_buffered', 0)/1024/1024:.1f} MB")
    interval = None
    cfg = curl(args.pod, args.namespace, f"/jobs/{jid}/checkpoints/config")
    if cfg:
        interval = cfg.get("interval")
        print(f"  intervalo configurado={interval} ms  modo={cfg.get('mode')}"
              f"  timeout={cfg.get('timeout')} ms")
    if dur and interval and dur > 0.3 * interval:
        print()
        print("  !! El checkpoint ocupa una fracción grande de su propio intervalo.")
        print("     Ese es el candidato a explicar la contrapresión sin CPU ocupada.")


if __name__ == "__main__":
    main()
