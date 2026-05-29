#!/bin/bash
# Offline contextual bandit (LinUCB) — V2 variant.
# Arms = {BANDIT (online), LEAST_LOADED, BALANCED} — the three dynamic strategies
# that dominate summary.csv. Coexists with the original OFFLINE_BANDIT
# (FCFS/BALANCED/SARSA) for comparison.
#
# Pipeline (each run_strategy_experiment also runs mvn + docker build + minikube load):
#   1. Re-train V2 LinUCB weights from current results/
#   2. Run the experiment on each arrival distribution (CONSTANT, SINE, STEP)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defensive: ensure no Nexmark env vars leak from a parent shell that previously
# sourced experiment-q5.sh / experiment-q8.sh. Without this, ConfigurableGraphJob
# is replaced by NexmarkRealJob and runs land in the wrong workload silently.
unset JOB_CLASS EXTRA_JOB_ARGS HEAVY_VERTEX_PATTERN

echo "=========================================="
echo "  [PRE] Re-training offline bandit V2 weights"
echo "  Arms:   BANDIT, LEAST_LOADED, BALANCED"
echo "  Reward: throughput_per_core (efficiency-aligned)"
echo "  Scope:  autoscaler-* only (Q5/Q8 stay zero-shot OOD tests)"
echo "=========================================="
if ! python3 "${SCRIPT_DIR}/scripts/train_offline_bandit.py" \
        --arms BANDIT,LEAST_LOADED,BALANCED \
        --reward throughput_per_core \
        --out "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v2_weights.json"; then
  echo "ERROR: offline bandit V2 training failed — aborting"
  exit 1
fi
echo ""

set +e   # individual runs handle their own errors
source "${SCRIPT_DIR}/run-experiment-common.sh"

run_strategy_experiment "OFFLINE_BANDIT_V2" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V2" "false" "autoscaler-sine"   60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V2" "false" "autoscaler-step"   60000 300 8 10 2500 STEP     2 15000 "true"
