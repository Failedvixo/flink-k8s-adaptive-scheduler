#!/usr/bin/env python3
"""Cancel every job that is not already finished, so a profiling or calibration
run starts from an empty cluster rather than sharing it with a leftover."""
import argparse, json, subprocess

ap = argparse.ArgumentParser()
ap.add_argument("--pod", required=True)
ap.add_argument("--namespace", default="flink")
args = ap.parse_args()


def curl(*extra):
    return subprocess.run(["kubectl", "exec", "-n", args.namespace, args.pod, "--",
                           "curl", "-s", "-m", "15", *extra],
                          capture_output=True, text=True, timeout=40).stdout


try:
    jobs = json.loads(curl("http://localhost:8081/jobs")).get("jobs", [])
except (json.JSONDecodeError, subprocess.SubprocessError, ValueError):
    jobs = []
for job in jobs:
    if job.get("status") in ("RUNNING", "CREATED", "RESTARTING"):
        curl("-X", "PATCH", f"http://localhost:8081/jobs/{job['id']}?mode=cancel")
        print(f"  cancelled {job['id']}")
