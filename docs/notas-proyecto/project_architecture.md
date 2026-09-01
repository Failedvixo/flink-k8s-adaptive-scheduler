---
name: Project architecture & calibration
description: Core design decisions, calibrated parameters, and what makes strategies distinguishable
type: project
originSessionId: 1132784d-0659-4570-bd97-16aa76782774
---
**Setup**: WSL2 + Minikube (3 nodes × 2 CPUs × 4GB), Docker driver. Namespace: `flink`.

**Custom scheduler** (Java, `kube-system` namespace): places TMs on nodes via `createNamespacedBinding()`, reads real metrics from K8s Metrics Server. 7 strategies: FCFS, BALANCED, LEAST_LOADED, PRIORITY, BANDIT (UCB1), SARSA, ADAPTIVE. Strategy selected via `FIXED_STRATEGY` env var; omit for ADAPTIVE mode (do NOT set to "ADAPTIVE" — crashes, no enum value).

**Flink cluster**: 1 JM + 5 TMs (2 slots each = 10 slots). TMs use `schedulerName: adaptive-scheduler`. Flink Adaptive Scheduler enabled (`jobmanager.scheduler: adaptive`).

**Pipeline**: Source → Filter → CPU-Load (disableChaining, independent parallelism) → LatencyTracker → Transform → Window → Sink

**Job arg order** (authoritative from GraphConfig.fromArgs):
```
args[0] = eventsPerSecond
args[1] = durationSeconds
args[2] = globalParallelism
args[3] = windowSizeSeconds
args[4] = cpuLoadIterationsPerEvent
args[5] = arrivalDistribution (CONSTANT|STEP|SINE)
args[6] = cpuLoadParallelism (0 = use globalParallelism)
args[7] = maxEventAgeMs (0 = disabled)
```

**run-experiment-common.sh `run_strategy_experiment` signature** (12 args, order matters):
```
1:STRATEGY 2:ADAPTIVE_MODE 3:WORKLOAD 4:RATE 5:DURATION 6:PARALLELISM 7:WINDOW
8:CPU_LOAD 9:ARRIVAL_DIST 10:CPU_LOAD_PAR 11:MAX_EVENT_AGE 12:USE_AUTOSCALER
```
When USE_AUTOSCALER=true: scheduler setup runs as usual (steps 1–5), then autoscaler.sh is invoked; it submits the job and writes `$RESULTS_DIR/job-id.txt`; the common script reads it and waits for FINISHED before collecting metrics. Results path uses `$WORKLOAD` (e.g. `results/autoscaler/FCFS/`).

**autoscaler.sh internals**:
- Clamps INITIAL_CPU_PAR to ≥ MIN_PAR (handles CPU_LOAD_PAR=0 input)
- Uses instantaneous busy% (delta-based: `Δaccumulated_busy / (Δt × par)`), NOT cumulative — see pitfalls memory for why
- Helper `get_cpuload_accum_ms` does one HTTP call returning busy + par
- PUT to `/jobs/{id}/resource-requirements` includes the FULL vertex payload (other vertices pinned at their current par) — partial payloads are rejected
- Logs `kubectl top nodes` on each monitor tick

**Calibrated parameters** (optimal for strategy differentiation, static experiments):
- rate=100,000 ev/s, duration=180s, parallelism=8, window=10s
- cpuLoad=2500 iter/event, maxEventAge=2000ms
- Source drop ~0%, stale drop >10% (differentiated between strategies)

**Why staleness matters**: Without it, all strategies give similar throughput (cluster not saturated). With maxEventAgeMs=2000, bad TM distribution (e.g. FCFS concentrating on 1 node) → congestion → stale drops → lower useful throughput. This is what makes strategies distinguishable.

**Open tension for elastic experiments**: stale-drop happens before the CPU work, so a busy%-driven autoscaler is blind under heavy stale-drop. Either change autoscaler signal (e.g. filter backpressure) or recalibrate cpuLoad down so the initial low-par config can keep up enough to register busy time.

**Why**: Thesis needs to show the adaptive scheduler outperforms naive approaches with measurable, statistically significant differences.
**How to apply**: When tuning parameters, always verify source drop ≈ 0% and stale drop > 10% and differentiated. If source drop is high, the cluster is saturated before the scheduler even matters.
