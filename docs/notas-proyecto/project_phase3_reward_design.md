---
name: project-phase3-reward-design
description: "Phase 3 design decision — drive bandit/SARSA from Flink's own metrics (busy%, backpressure) aggregated per TaskManager, not from K8s node metrics or the structural 'balanced' proxy."
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e512007-ca15-415d-b402-fd9c09f45b62
  modified: 2026-08-04T22:51:27.272Z
---

**DECIDED 2026-08-04 (user's call).** The bandit/SARSA reward and state for the Flink-fork arm
selection come from **Flink's own metrics**, system-agnostic — not from the K8s Metrics Server, and
not from the structural `balanced` counter used to validate Phase 2.

**Why `balanced` is not just incomplete but can be wrong.** It counts slices per TM, not load. In
TopSpeedWindowing, slice#0 carries the `source` operator on top of the window while the others carry
only the window, so a 1/1/1 spread scores "balanced" while the TM holding slice#0 genuinely does more
work. Structural counting cannot see this by construction. It was the right instrument to prove the
mechanism works in Phase 2; it is the wrong reward.

**Why K8s node metrics are the wrong granularity.** The existing external strategies score on node
CPU%/mem% from the Metrics Server, but there are 5 TMs on 3 nodes — two TMs on one node collapse into
a single number. A placement that packs two heavy slices onto one TM and one that separates them
*within the same node* are indistinguishable at node level, while the assigner decides at TM level.

**Verified available in-cluster 2026-08-04 (Flink 1.18):**
- Per subtask, `/jobs/{jid}/vertices/{vid}/subtasks/metrics`: `busyTimeMsPerSecond`,
  `idleTimeMsPerSecond`, `backPressuredTimeMsPerSecond` (+ `hard`/`soft` variants),
  `numRecordsInPerSecond`, `numRecordsOutPerSecond`.
- Per TaskManager, `/taskmanagers/{id}/metrics`: `Status.JVM.CPU.Load`, `Status.JVM.Memory.Heap.Used`,
  `Status.JVM.CPU.Time`.
- **THE JOIN KEY:** `/jobs/{jid}/vertices/{vid}` returns `taskmanager-id` per subtask. That is what
  lets task metrics be aggregated to TM granularity — the granularity the assigner actually decides
  at. Without it you have per-subtask metrics and per-TM decisions with no way to connect them.
- `busyTimeMsPerSecond` is already a RATE, so it sidesteps the cumulative-busy-time pitfall in
  [[feedback_pitfalls]] (that one applies to `accumulated-busy-time`).

**Reward shape (two axes, they measure different things):**
1. *Placement quality* = dispersion of load across TMs: aggregate subtask `busyTimeMsPerSecond` by
   `taskmanager-id`, then coefficient of variation (or max−min). Low dispersion = the real work got
   spread, not just the slice count.
2. *Outcome* = throughput/core, kept for comparability with the existing K8s-side experiments.

Dispersion also helps the known parallelism confound: throughput changes when parallelism goes 4→8
even with identical placement, but per-TM dispersion does not.

**Three caveats to respect:**
- MEASUREMENT WINDOW: a rescale restarts the job from checkpoint; metrics during the warm-up are
  garbage. Discard the first N seconds and measure in steady state — fix and document N, it changes
  the result.
- BACKPRESSURE IS STATE, NOT REWARD: a backpressured subtask says the bottleneck is DOWNSTREAM, not
  that its placement is bad. Use it in the SARSA context; rewarding it conflates bottleneck location
  with placement quality.
- FLAT WITHOUT SKEW: with homogeneous TMs and symmetric slices, dispersion is ~0 under every arm and
  there is nothing to learn. Needs `HEAVY_VERTEX_PATTERN` Q5/Q8 (see [[project_nexmark_integration]]),
  not TopSpeedWindowing. Pairs with the still-open problem that ROUND_ROBIN and LEAST_LOADED are
  behaviourally identical (8/8 both) — better metrics do not fix an arm set whose arms all do the same
  thing. See [[project_flink_core_fork]].
