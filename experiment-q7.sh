#!/bin/bash
# Nexmark Q7 against the 3 arrival distributions, looping over scheduler
# strategies. Output: results/q7-{const,sine,step}/{STRATEGY}/
#
# Override the strategy list via STRATEGIES env var.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEFAULT_LIST="FCFS BALANCED LEAST_LOADED BANDIT SARSA OFFLINE_BANDIT OFFLINE_BANDIT_V2 OFFLINE_BANDIT_V3 ADAPTIVE DEFAULT"
STRATEGIES="${STRATEGIES:-$DEFAULT_LIST}"

export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
export EXTRA_JOB_ARGS="q7 0.5 1000"
export HEAVY_VERTEX_PATTERN="q7-max-bid"

trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

echo "=========================================="
echo "  Q7 q7-max-bid — strategies: $STRATEGIES"
echo "  JOB_CLASS=$JOB_CLASS"
echo "  HEAVY_VERTEX_PATTERN=$HEAVY_VERTEX_PATTERN"
echo "=========================================="

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

for STRATEGY in $STRATEGIES; do
  ADAPTIVE_FLAG="false"
  [ "$STRATEGY" = "ADAPTIVE" ] && ADAPTIVE_FLAG="true"

  echo ""
  echo "######################################################"
  echo "###  STRATEGY=$STRATEGY  (adaptive=$ADAPTIVE_FLAG)"
  echo "######################################################"

  run_strategy_experiment "$STRATEGY" "$ADAPTIVE_FLAG" "q7-const" 100000 600 8 10 2500 CONSTANT 2 15000 "true"
  run_strategy_experiment "$STRATEGY" "$ADAPTIVE_FLAG" "q7-sine"   60000 600 8 10 2500 SINE     2 15000 "true"
  run_strategy_experiment "$STRATEGY" "$ADAPTIVE_FLAG" "q7-step"   60000 600 8 10 2500 STEP     2 15000 "true"
done
