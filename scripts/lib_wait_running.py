#!/usr/bin/env python3
"""Block until a job reaches RUNNING.

The warm-up clock must not start while the job is still waiting for slots, or the
measurement window lands in the transient instead of the steady state.
"""
import argparse, json, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--pod", required=True)
ap.add_argument("--job", required=True)
ap.add_argument("--namespace", default="flink")
ap.add_argument("--timeout", type=float, default=180)
args = ap.parse_args()

deadline = time.time() + args.timeout
while time.time() < deadline:
    try:
        out = subprocess.run(
            ["kubectl", "exec", "-n", args.namespace, args.pod, "--", "curl", "-s",
             "-m", "15", f"http://localhost:8081/jobs/{args.job}"],
            capture_output=True, text=True, timeout=40).stdout
        state = json.loads(out).get("state", "")
    except (json.JSONDecodeError, subprocess.SubprocessError, ValueError):
        state = ""
    if state == "RUNNING":
        sys.exit(0)
    if state in ("FAILED", "CANCELED", "FINISHED"):
        print(f"job reached {state} instead of RUNNING", file=sys.stderr)
        sys.exit(1)
    time.sleep(2)
print("timed out waiting for RUNNING", file=sys.stderr)
sys.exit(1)
