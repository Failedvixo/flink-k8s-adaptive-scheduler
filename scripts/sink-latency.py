#!/usr/bin/env python3
"""Per-record latency of the running job, read from the sinks' LatencyMeter gauges.

Prints, for every sink subtask, the p50, p99 and mean latency (ms) of the results it emitted in
the last minute, and how many results that is. Latency = when the result left the sink minus
when its input entered the graph (see LatencyMeter.java).

Usage: scripts/sink-latency.py            # needs a RUNNING job built with LatencyMeter
"""
import json
import subprocess
import sys

NS = "flink"
GAUGES = ("latencyP50Ms", "latencyP99Ms", "latencyMeanMs", "latencySamples")


def jm(pod, path):
    out = subprocess.run(["kubectl", "exec", "-n", NS, pod, "--", "curl", "-s", "-m", "15",
                          f"http://localhost:8081{path}"],
                         capture_output=True, text=True, timeout=40).stdout
    try:
        return json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return None


def main():
    pod = subprocess.run(["kubectl", "get", "pods", "-n", NS, "-l", "component=jobmanager",
                          "--field-selector=status.phase=Running",
                          "-o", "jsonpath={.items[-1:].metadata.name}"],
                         capture_output=True, text=True).stdout.strip()
    jobs = [j for j in (jm(pod, "/jobs/overview") or {}).get("jobs", [])
            if j.get("state") == "RUNNING"]
    if not jobs:
        print("ERROR: no hay job RUNNING", file=sys.stderr)
        return 2
    jid = jobs[0]["jid"]
    found = False
    for v in (jm(pod, f"/jobs/{jid}") or {}).get("vertices", []):
        if not v["name"].startswith("Sink"):
            continue
        print(f"{v['name']}  par={v.get('parallelism')}")
        for i in range(int(v.get("parallelism", 0))):
            base = f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics"
            ids = [m["id"] for m in (jm(pod, base) or []) if m["id"].split(".")[-1] in GAUGES]
            if not ids:
                print(f"  sub {i}: sin LatencyMeter (¿jar viejo?)")
                continue
            found = True
            got = {m["id"].split(".")[-1]: m.get("value")
                   for m in (jm(pod, f"{base}?get={','.join(ids)}") or [])}
            n = int(float(got.get("latencySamples") or 0))
            # The meter keeps at most 50 000 results (LatencyMeter.CAPACITY): at Q8's rate that is
            # less than a minute, and the figures then cover only the most recent results.
            span = ("último minuto" if n < 50000
                    else "últimos 50 000 resultados (menos de un minuto: búfer lleno)")
            print(f"  sub {i}: p50={got.get('latencyP50Ms')} ms  p99={got.get('latencyP99Ms')} ms  "
                  f"media={got.get('latencyMeanMs')} ms  ({span})")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
