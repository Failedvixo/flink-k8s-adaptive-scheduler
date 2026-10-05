#!/usr/bin/env python3
"""Per-subtask output of every source of the running job, with the machine each one runs on.

WHY (2026-10-04). Q5's calibration found the bid source emitting 71% of the requested rate at
10000 bids/s while every operator downstream sat ~90% idle with no backpressure — so the loss
is in the source, not the job. The emission loop reproduces 100% offline; what differs in the
cluster is WHERE each source subtask runs. A subtask starved of CPU on the one-core machine
falls more than MAX_EVENT_AGE behind and starts skipping, and the vertex total cannot show
that: only the per-subtask rates next to their TaskManager can.

Usage: scripts/source-subtasks.py            # needs a RUNNING job
"""
import json
import subprocess
import sys

NS = "flink"


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
    detail = jm(pod, f"/jobs/{jid}") or {}
    for v in detail.get("vertices", []):
        if not v["name"].startswith("Source"):
            continue
        info = jm(pod, f"/jobs/{jid}/vertices/{v['id']}") or {}
        print(f"{v['name']}  par={v.get('parallelism')}")
        total = 0.0
        for s in info.get("subtasks", []):
            i = s["subtask"]
            # Ask which metrics exist instead of guessing the operator prefix: a legacy source
            # counts its output at OPERATOR scope ("Source__bid-source.numRecordsOutPerSecond"),
            # and the task-scope counter of the same name reads 0 for it.
            base = f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics"
            ids = [m["id"] for m in (jm(pod, base) or [])]
            extras = ("bidsHandedToCollect", "loopLagEpochs", "loopMaxGapMs", "skippedEpochs",
                      "numRecordsOut")
            wanted = [m for m in ids if m.endswith("numRecordsOutPerSecond")
                      or m.endswith("generatorBusyMsPerSecond")
                      or m.split(".")[-1] in extras]
            got = {m["id"]: m.get("value") for m in
                   (jm(pod, f"{base}?get={','.join(wanted)}") if wanted else []) or []}
            outs = [float(val) for k, val in got.items()
                    if k.endswith("numRecordsOutPerSecond") and val not in (None, "")]
            out = max(outs) if outs else 0.0
            total += out
            busy = next((val for k, val in got.items()
                         if k.endswith("generatorBusyMsPerSecond")), "?")
            print(f"  sub {i}  {s.get('taskmanager-id', '?'):14}  out={out:8.0f}/s  "
                  f"generador ocupado={busy} ms/s")
            diag = {k.split(".")[-1]: val for k, val in got.items()
                    if k.split(".")[-1] in extras}
            if diag:
                print("         " + "  ".join(f"{k}={val}" for k, val in sorted(diag.items())))
        print(f"  total {total:.0f}/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
