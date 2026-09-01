---
name: feedback-user-runs-commands
description: "Vicente runs all cluster and experiment commands himself — hand him the commands, never execute them."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 12e50a9f-8d6b-48f3-9061-9be2fe03218f
  modified: 2026-08-20T18:42:42.220Z
---

**Never run cluster or experiment commands. Give Vicente the commands and let him run them.**
Stated 2026-08-20: "no levantes nada, siempre dame los comandos para hacerlo yo". This covers
`minikube start`, `kubectl apply/scale/delete`, `deploy-thesis-fork.sh`, `set-taskmanager-classes.sh`,
`run-placement-experiment.sh`, `publish-*.sh` — anything that changes the cluster or launches a run.

**Why:** he is driving a fragile 3-node minikube on a 7.7 GB WSL host that has crashed repeatedly,
and he wants to see and control what touches it. It is also how he keeps track of what state the
cluster is actually in between sessions.

**How to apply:** read-only inspection is fine and useful — `kubectl get/describe`, reading result
CSVs, `git status`, grepping source. Analysis of finished runs is expected. But anything that starts,
stops, scales, deploys or measures goes to him as a copy-pasteable block, in the multi-line
`VAR=value \` style with `2>&1 | tee /tmp/<name>.log` at the end, which is the format he asked for.
Wait for him to report back rather than polling the cluster for progress.

Related: [[project_flink_core_fork]].
