#!/bin/bash
# SARSA_META — tabular SARSA meta-scheduler (Phase 5).
#
# SARSA is the *meta-optimizer* (not a base arm): the cluster context is
# discretised into a tabular state (LOW/MED/HIGH per feature), the action is
# the index of a base arm {FCFS, BALANCED, LEAST_LOADED, BANDIT}, and
# Q(state, arm) is learned offline by on-policy TD(0) over the q2-* runs.
# At runtime the scheduler only reads the Q-table and picks argmax_a Q(s,a).
#
# Compare against OFFLINE_BANDIT_V4 / V5 (LinUCB) on the same q2 scenarios.
#
# Default state features are host metrics populated in every q2 run
# (saturation, cpu_imbalance, mem_imbalance) — busy_inst is excluded because
# it is ~0 in the q2-sine logs. Override via SARSA_STATE_FEATURES.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
export EXTRA_JOB_ARGS="q2 0.5 1000"
export HEAVY_VERTEX_PATTERN="q2-selection"

WEIGHTS="${SCRIPT_DIR}/scheduler/src/main/resources/sarsa_meta_weights.json"
STATE_FEATURES="${SARSA_STATE_FEATURES:-saturation,cpu_imbalance,mem_imbalance}"

if [ "${SKIP_SARSA_META_TRAIN:-0}" = "1" ]; then
  echo "=========================================="
  echo "  [PRE] SKIP_SARSA_META_TRAIN=1 — reusing existing Q-table at"
  echo "        ${WEIGHTS}"
  echo "=========================================="
  if [ ! -f "${WEIGHTS}" ]; then
    echo "ERROR: SKIP_SARSA_META_TRAIN=1 but Q-table file does not exist"
    exit 1
  fi
else
  echo "=========================================="
  echo "  [PRE] Training SARSA_META Q-table (tabular SARSA, TD(0))"
  echo "  Arms:    FCFS, BALANCED, LEAST_LOADED, BANDIT"
  echo "  Scope:   q2-{const,sine,step}"
  echo "  Reward:  throughput_per_core"
  echo "  State:   ${STATE_FEATURES} (3 bins each)"
  echo "=========================================="
  if ! python3 "${SCRIPT_DIR}/scripts/train_sarsa_meta.py" \
          --arms FCFS,BALANCED,LEAST_LOADED,BANDIT \
          --dists q2-const,q2-sine,q2-step \
          --reward throughput_per_core \
          --state-features "${STATE_FEATURES}" \
          --bins 3 \
          --out "${WEIGHTS}"; then
    echo "ERROR: SARSA_META training failed — aborting"
    exit 1
  fi
fi
echo ""

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

run_strategy_experiment "SARSA_META" "false" "q2-const" 100000 600 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "SARSA_META" "false" "q2-sine"   60000 600 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "SARSA_META" "false" "q2-step"   60000 600 8 10 2500 STEP     2 15000 "true"
