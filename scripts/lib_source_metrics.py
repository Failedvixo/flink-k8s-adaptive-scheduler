#!/usr/bin/env python3
"""Median output rate, backpressure and busy time across ALL source vertices.

WHY THIS EXISTS (2026-09-02). calibrate-rate.sh used to pick "the source" as the
first vertex with no inputs in the job plan. That held while the project's own
generator produced one tagged stream. The reference Nexmark implementation has a
source per event type — Q8 has two, persons at rate/4 and auctions at 3*rate/4 —
so the old rule measured one branch and compared it against the total requested
rate. Both arms reported a ratio of exactly 0.25 and the script concluded no rate
was sustainable, when the job was emitting precisely what it had been asked for.

Output rate is SUMMED over the sources, because together they are the job's input.
Backpressure and busy time are AVERAGED, because they are per-subtask fractions of
a second and summing them would exceed 1000 for no reason.
"""

import argparse
import json
import statistics as st
import subprocess
import time

METRICS = ["numRecordsOutPerSecond", "backPressuredTimeMsPerSecond",
           "busyTimeMsPerSecond"]


def jm(pod, namespace, path):
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
    ap.add_argument("--job", required=True)
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--interval", type=float, default=10)
    args = ap.parse_args()

    detail = jm(args.pod, args.namespace, f"/jobs/{args.job}")
    if not detail:
        print("0 0 nan 0")
        return
    plan = detail.get("plan", {}).get("nodes", [])
    sources = [n["id"] for n in plan if not n.get("inputs")]
    if not sources:
        print("0 0 nan 0")
        return

    rows = []
    for i in range(args.samples):
        total_out, bps, busies = 0.0, [], []
        for vid in sources:
            m = jm(args.pod, args.namespace,
                   f"/jobs/{args.job}/vertices/{vid}/subtasks/metrics"
                   f"?get={','.join(METRICS)}")
            got = {x["id"]: x for x in m} if m else {}

            def val(key, field):
                try:
                    return float(got[key][field])
                except (KeyError, TypeError, ValueError):
                    return float("nan")

            out = val("numRecordsOutPerSecond", "sum")
            if out == out:
                total_out += out
            for key, acc in (("backPressuredTimeMsPerSecond", bps),
                             ("busyTimeMsPerSecond", busies)):
                v = val(key, "avg")
                if v == v:
                    acc.append(v)
        rows.append((total_out,
                     st.mean(bps) if bps else float("nan"),
                     st.mean(busies) if busies else float("nan")))
        if i < args.samples - 1:
            time.sleep(args.interval)

    def med(idx):
        vals = [r[idx] for r in rows if r[idx] == r[idx]]
        return st.median(vals) if vals else float("nan")

    print(f"{med(0)} {med(1)} {med(2)} {len(sources)}")


if __name__ == "__main__":
    main()
