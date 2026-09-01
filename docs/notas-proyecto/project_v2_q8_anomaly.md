---
name: v2-underperforms-v1-on-q8-open-thesis-question
description: "V1 OFFLINE_BANDIT beats V2-new on Q8 stateful join, +42% in SINE. Open investigation; entry point for Q8 root-cause work."
metadata: 
  node_type: memory
  type: project
  originSessionId: f97d07a6-511b-4264-9779-f97b646675d5
---

**Observation** (verified against `results/plots/summary_q8.csv` on 2026-05-21):

| Q8 dist | V1 Tput/core | V2-new Tput/core | Winner | Δ |
|---|---:|---:|---|---:|
| CONST | 4,330 | 4,403 | V2 | +1.7% (noise) |
| **SINE** | **1,842** | 1,298 | **V1** | **+42%** |
| **STEP** | **2,343** | 2,146 | **V1** | **+9%** |

The pattern is consistent on cost/Mevent too (V1 has lower cost in SINE/STEP). V1 wins clearly on stateful + dynamic workloads.

**Working hypothesis**: V1's arms = {FCFS, BALANCED, SARSA} include FCFS (static, no reassignment) and SARSA (slow temporal-difference learning). V2's arms = {BANDIT (UCB1 online), LEAST_LOADED, BALANCED} are all reactive — they re-decide based on instantaneous CPU. On a Q8 windowed join the operator state accumulates over 10s — pulling pods around in reaction to short-lived CPU spikes likely thrashes the partition assignment and causes state-shuffling work that masquerades as additional load.

**Why**: This is the most interesting open thread for the thesis. Either we explain it (it becomes a documented model limitation that motivates feature-space extension) or we fix it (a V3 that's stable on stateful workloads).
**How to apply**: When investigating, the entry points are:
- `results/q8-sine/OFFLINE_BANDIT/scheduler-logs.txt` vs `.../OFFLINE_BANDIT_V2/scheduler-logs.txt` — count `[SCHEDULING]` decisions and arm switches over time. Thrashing hypothesis predicts V2 has more decisions and more arm changes.
- `taskmanager-placement.txt` in both — if V2 reassigns more pods across nodes during the run, that's direct evidence.
- `autoscaler.log` snapshots — compare CPU per-node trajectories; thrashing manifests as oscillation.
- Flink REST `accumulated-backpressured-time` / `accumulated-idle-time` per vertex (already in job-details.json) — backpressure on the join vertex would indicate state pressure.

If hypothesis confirmed, two thesis-grade fixes:
1. Add backpressure/idle features to the LinUCB context vector so V2 can learn to *not* react when the heavy vertex is backpressured (cf. [[project_status]] under "Q8 anomaly").
2. Add a cooldown / hysteresis to the arm-selection loop so changes only fire when busy% delta exceeds a threshold for N consecutive snapshots.

Both are 2-3h coding + 1 night of cluster runs each.
