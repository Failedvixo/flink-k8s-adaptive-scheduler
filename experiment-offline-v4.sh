#!/bin/bash
# Offline contextual bandit (LinUCB) — V4 variant.
# Expanded arm set: {FCFS, BALANCED, LEAST_LOADED, BANDIT, SARSA} — every
# base scheduling strategy currently implemented. Theta weights are trained
# offline on the q2-* Nexmark scenario (highest variance between strategies
# observed during the sweep, CV=34.4% on q2/sine).
#
# Reward signal: throughput_per_core (efficiency-aligned with Nexmark canon).
#
# Pipeline:
#   1. Re-train V4 LinUCB weights from results/q2-{const,sine,step}/
#   2. Run the experiment on each arrival distribution (CONSTANT, SINE, STEP)
#      against the q2 Nexmark workload itself.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defensive guard: q2 IS a Nexmark workload, but the trainer above runs Python
# without touching cluster env. The cluster runs below need JOB_CLASS etc. set
# explicitly per cell since experiment-offline-v* historically targeted
# autoscaler-* (synthetic, no JOB_CLASS).
export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
export EXTRA_JOB_ARGS="q2 0.5 1000"
export HEAVY_VERTEX_PATTERN="q2-selection"

if [ "${SKIP_V4_TRAIN:-0}" = "1" ]; then
  echo "=========================================="
  echo "  [PRE] SKIP_V4_TRAIN=1 — reusing existing weights at"
  echo "        scheduler/src/main/resources/offline_bandit_v4_weights.json"
  echo "=========================================="
  if [ ! -f "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v4_weights.json" ]; then
    echo "ERROR: SKIP_V4_TRAIN=1 but weights file does not exist"
    exit 1
  fi
else
  echo "=========================================="
  echo "  [PRE] Re-training offline bandit V4 weights (ridge regression)"
  echo "  Arms:   FCFS, BALANCED, LEAST_LOADED, BANDIT, SARSA (5 base strategies)"
  echo "  Scope:  q2-{const,sine,step} (high-variance Nexmark scenario)"
  echo "  Reward: throughput_per_core"
  echo "=========================================="
  if ! python3 "${SCRIPT_DIR}/scripts/train_offline_bandit.py" \
          --arms FCFS,BALANCED,LEAST_LOADED,BANDIT,SARSA \
          --dists q2-const,q2-sine,q2-step \
          --reward throughput_per_core \
          --out "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v4_weights.json"; then
    echo "ERROR: offline bandit V4 training failed — aborting"
    exit 1
  fi
fi
echo ""

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

run_strategy_experiment "OFFLINE_BANDIT_V4" "false" "q2-const" 100000 600 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V4" "false" "q2-sine"   60000 600 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V4" "false" "q2-step"   60000 600 8 10 2500 STEP     2 15000 "true"
