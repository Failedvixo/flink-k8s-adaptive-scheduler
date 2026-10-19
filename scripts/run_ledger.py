#!/usr/bin/env python3
"""The whole-run ledger: what the job processed and how fast, every few seconds, for the WHOLE run.

WHY (2026-10-13, Vicente). The monitor's decision windows are the wrong basis for evaluating arms:
  * they leave out the settling time after every move — the minutes an arm that moves (the bandit)
    pays and an arm that never moves does not, so averaging the windows flatters the mover;
  * under a variable rate, a window at 2000 ev/s weighs as much as one at 6000 ev/s, although losing
    a fifth of the stream costs three times more events at the top of the sine.
This samples independently of any monitor, identically for every arm, from start to end:

  delivered  cumulative records emitted by the sources (write-records, summed over source
             vertices; restarts reset the counter, and are bridged by adding the last value seen)
  offered    cumulative records the profile asked for (RATE x the DIST multiplier, integrated)
  latency    per-record p50 and p99 at the sinks (the slowest sink subtask), as the gauges report

The run's throughput is delivered / offered over the whole run; its latency is the time-weighted
distribution of the samples. Bridging a restart misses what the sources emitted between the last
counter refresh and the restart (up to ~10 s of a few thousand ev/s, well under 1% of an hour per
restart) — a cost charged to moving, so it is conservative for an arm that moves.

Usage: scripts/run_ledger.py --rate 6000 --dist SINE:3600:2000,6000 --duration 3600 --out ledger.csv
"""
import argparse
import csv
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import agent_v5_loop as v5loop                                 # noqa: E402


def snapshot(base, with_latency=True):
    jid = ac.running_job(base)
    if not jid:
        return None
    d = ac.rest(base, f"/jobs/{jid}")
    if not d:
        return None
    sources, sinks = ac.graph_roles(d)
    records = 0.0
    for v in d.get("vertices", []):
        if v["id"] in sources:
            try:
                records += float((v.get("metrics") or {}).get("write-records") or 0)
            except (TypeError, ValueError):
                pass
    started = max((v.get("start-time", 0) for v in d.get("vertices", [])), default=0)
    p50 = p99 = None
    for v in d.get("vertices", []) if with_latency else []:
        if v["id"] not in sinks:
            continue
        for i in range(int(v.get("parallelism", 0))):
            g = ac.sink_latency(base, jid, v["id"], i)
            if g.get("latencySamples", 0) > 0 and g.get("latencyP50Ms", -1) >= 0:
                p50 = max(p50 or 0.0, g["latencyP50Ms"])
                p99 = max(p99 or 0.0, g.get("latencyP99Ms", g["latencyP50Ms"]))
    return {"records": records, "started": started, "p50": p50, "p99": p99,
            "running": all(v.get("status") == "RUNNING" for v in d.get("vertices", []))}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--dist", default="CONSTANT")
    ap.add_argument("--duration", type=float, required=True)
    # The counter is read every 2 s, though Flink refreshes it every ~10 s, so the moment it last
    # changed before a restart is known to ~2 s: that is what bridging a restart relies on.
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--latency-every", type=int, default=5, help="read latency every N samples")
    ap.add_argument("--out", required=True)
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--jm-pod", default="")
    args = ap.parse_args()
    ac._NAMESPACE = args.namespace
    ac._JM_POD = args.jm_pod or subprocess.run(
        ["kubectl", "get", "pods", "-n", args.namespace, "-l", "component=jobmanager",
         "--field-selector=status.phase=Running", "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True, timeout=30).stdout.strip() or None
    base = ac.DEFAULT_REST

    # A deadline kill must still leave the summary (2026-10-19): end the loop on SIGTERM instead of
    # dying in it. See the timeout in run-replan-experiment.sh for why it was being killed.
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))

    t0 = time.time()
    prev_t, offered = t0, 0.0
    bridge, last_records, last_started, restarts = 0.0, None, None, 0
    last_change_t, recent_rate = None, 0.0
    stalled = 0.0
    rows = []
    with open(args.out, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["t", "elapsed_s", "offered_rate", "offered_cum", "delivered_cum", "p50_ms",
                    "p99_ms", "running", "restarts"])
        while time.time() - t0 < args.duration and not stop:
            time.sleep(max(0.0, args.interval - (time.time() - prev_t)))
            now = time.time()
            rate_now = args.rate * v5loop.offered_multiplier(args.dist, args.rate, prev_t, now)
            offered += rate_now * (now - prev_t)
            prev_t = now
            tick = len(rows)
            s = snapshot(base, with_latency=(tick % args.latency_every == 0))
            if s is None:
                w.writerow([round(now, 1), round(now - t0, 1), round(rate_now), round(offered),
                            round(bridge + (last_records or 0)), "", "", False, restarts])
                stalled += args.interval
                continue
            if last_started is not None and (s["started"] != last_started or
                                             (last_records is not None and s["records"] < last_records)):
                # Bridge the restart: the old attempt's last counter value, plus what it emitted
                # between that value's refresh (~ when we last saw it change) and the restart
                # (the new attempt's start time), at the rate it was running. Without the second
                # term every restart cost up to ~10-20 s of events — 1.7% of a 10-min test for one
                # instantaneous restart, which would have been charged to the arm that moves.
                restart_t = s["started"] / 1000.0 if s["started"] else now
                gap = max(0.0, min(restart_t, now) - (last_change_t or restart_t))
                bridge += (last_records or 0.0) + recent_rate * min(gap, 3 * args.interval)
                restarts += 1
                last_records, last_change_t = s["records"], now
            elif last_records is not None and s["records"] != last_records:
                if last_change_t is not None and now > last_change_t:
                    recent_rate = (s["records"] - last_records) / (now - last_change_t)
                last_change_t = now
            elif last_change_t is None:
                last_change_t = now
            delivered = bridge + s["records"]
            last_records, last_started = s["records"], s["started"]
            if not s["running"]:
                stalled += args.interval
            rows.append((now - t0, rate_now, s["p50"], s["p99"]))
            w.writerow([round(now, 1), round(now - t0, 1), round(rate_now), round(offered),
                        round(delivered), s["p50"] if s["p50"] is not None else "",
                        s["p99"] if s["p99"] is not None else "", s["running"], restarts])
            handle.flush()

    # The last counter value is up to one refresh (~10 s) old: add what the sources emitted since,
    # at the rate they were running — the same correction as for a restart, at the run's end.
    total_delivered = bridge + (last_records or 0.0)
    if last_change_t is not None:
        total_delivered += recent_rate * min(max(0.0, time.time() - last_change_t), 15.0)
    p50s = [p for _t, _r, p, _q in rows if p is not None]
    p99s = [q for _t, _r, _p, q in rows if q is not None]
    print(f"registro de la corrida: {args.out}")
    print(f"  entregado / pedido en toda la corrida: {total_delivered:,.0f} / {offered:,.0f} = "
          f"{total_delivered / offered:.1%}" if offered else "  sin tasa pedida")
    if p50s:
        print(f"  latencia p50: media {sum(p50s) / len(p50s) / 1000:.1f} s; "
              f"p99: media {sum(p99s) / len(p99s) / 1000:.1f} s, máxima {max(p99s) / 1000:.1f} s")
    print(f"  reinicios: {restarts}; tiempo sin todas las tareas corriendo: ~{stalled:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
