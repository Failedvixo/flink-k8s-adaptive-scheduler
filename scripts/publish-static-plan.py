#!/usr/bin/env python3
"""Publish a hand-written placement for the RL arm, keyed by task.

WHY (2026-09-17). The first RL evaluation beat LPT by 23.6%, and two readings fit it: that
what matters is HOW MANY slices land on the one-core machine (LPT puts four there, the agent
two), or WHICH stage lands there (LPT starves the person branch, which is cheap in CPU but
gates the join's watermark, the minimum of its inputs). A fixed plan per arm separates them:
same spread, different stage on the slow machine.

A spec names one vertex per slice and a machine class per subtask:

    new-users-join: fast fast
    person-source:  slow slow

One vertex is enough: the fork resolves a slice from any of its members, so the watermark and
map operators that share the person source's slot follow it. Vertices and TaskManagers are
resolved against the RUNNING job, so the file is valid whatever order the groups come out in.

Usage:
  scripts/publish-static-plan.py --spec plans/person_media.plan --jm-pod "$JM" [--width 8]
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac  # noqa: E402
from characterizer_agent import node_command, PLAN_FILE  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--spec", required=True)
ap.add_argument("--jm-pod", default="")
ap.add_argument("--namespace", default="flink")
ap.add_argument("--node", default="minikube")
ap.add_argument("--width", type=int, default=8, help="slice count the plan is for")
args = ap.parse_args()
ac._JM_POD = args.jm_pod or None
ac._NAMESPACE = args.namespace

jid = ac.running_job(ac.DEFAULT_REST)
detail = ac.rest(ac.DEFAULT_REST, f"/jobs/{jid}") if jid else None
tms = ac.registered_taskmanagers(ac.DEFAULT_REST)
if not detail or not tms:
    sys.exit("ERROR: no running job or no TaskManagers to resolve the plan against")

lines = [f"slices={args.width}"]
for raw in Path(args.spec).read_text().splitlines():
    raw = raw.split("#", 1)[0].strip()
    if not raw:
        continue
    name, _, machines = raw.partition(":")
    vertex = [v for v in detail["vertices"] if v.get("name", "").endswith(name.strip())]
    if len(vertex) != 1:
        sys.exit(f"ERROR: '{name.strip()}' matches {len(vertex)} vertices, expected exactly one")
    for subtask, cls in enumerate(machines.split()):
        tm = [t for t in tms if t.endswith(f"-{cls}")]
        if len(tm) != 1:
            sys.exit(f"ERROR: machine class '{cls}' matches {tm}")
        lines.append(f"task {vertex[0]['id']}:{subtask} {tm[0]}")

content = "\n".join(lines) + "\n"
_, error = node_command(
    args.node, ["sh", "-c", f"cat > {PLAN_FILE}.tmp && mv -f {PLAN_FILE}.tmp {PLAN_FILE}"
                            f" && chmod 644 {PLAN_FILE}"], content=content)
if error:
    sys.exit(f"ERROR: could not publish the plan — {error}")
print(content, end="")
