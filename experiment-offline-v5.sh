#!/bin/bash
# Offline contextual bandit (LinUCB) — V5 variant.
# Same arm set as V4-no-SARSA: {FCFS, BALANCED, LEAST_LOADED, BANDIT}.
# Extended 13-feature context vector:
#   bias, avg_cpu, max_cpu, min_cpu, cpu_imbalance, cpu_velocity,
#   avg_mem, elapsed_norm, saturation,                         (V1..V4)
#   mem_velocity, mem_imbalance, busy_inst, busy_velocity      (V5+)
#
# busy_inst is the heavy-vertex busy% pulled from Flink REST in runtime
# (cached for 10s). In training we read it from autoscaler.log per snapshot.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
export EXTRA_JOB_ARGS="q2 0.5 1000"
export HEAVY_VERTEX_PATTERN="q2-selection"

if [ "${SKIP_V5_TRAIN:-0}" = "1" ]; then
  echo "=========================================="
  echo "  [PRE] SKIP_V5_TRAIN=1 — reusing existing weights at"
  echo "        scheduler/src/main/resources/offline_bandit_v5_weights.json"
  echo "=========================================="
  if [ ! -f "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v5_weights.json" ]; then
    echo "ERROR: SKIP_V5_TRAIN=1 but weights file does not exist"
    exit 1
  fi
else
  echo "=========================================="
  echo "  [PRE] Re-training offline bandit V5 weights (ridge, 13 features)"
  echo "  Arms:   FCFS, BALANCED, LEAST_LOADED, BANDIT"
  echo "  Scope:  q2-{const,sine,step}"
  echo "  Reward: throughput_per_core"
  echo "  Feats:  13 (V4=9 + mem_velocity + mem_imbalance + busy_inst + busy_velocity)"
  echo "=========================================="
  if ! python3 "${SCRIPT_DIR}/scripts/train_offline_bandit.py" \
          --arms FCFS,BALANCED,LEAST_LOADED,BANDIT \
          --dists q2-const,q2-sine,q2-step \
          --reward throughput_per_core \
          --feature-dim 13 \
          --out "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v5_weights.json"; then
    echo "ERROR: offline bandit V5 training failed — aborting"
    exit 1
  fi
fi
echo ""

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

run_strategy_experiment "OFFLINE_BANDIT_V5" "false" "q2-const" 100000 600 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V5" "false" "q2-sine"   60000 600 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V5" "false" "q2-step"   60000 600 8 10 2500 STEP     2 15000 "true"
