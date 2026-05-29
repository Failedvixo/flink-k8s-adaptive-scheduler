#!/bin/bash
# Offline contextual bandit (LinUCB) meta-scheduler.
# Arms = {FCFS, BALANCED, SARSA}; weights are pre-trained from results/autoscaler-*
# and shipped inside the scheduler image as a classpath resource.
#
# Pipeline (each run_strategy_experiment also runs mvn + docker build + minikube load):
#   1. Re-train LinUCB weights from current results/
#   2. Run the experiment on each arrival distribution (CONSTANT, SINE, STEP)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defensive: prevent Nexmark env-var leak from a parent shell — see experiment-offline-v2.sh.
unset JOB_CLASS EXTRA_JOB_ARGS HEAVY_VERTEX_PATTERN

echo "=========================================="
echo "  [PRE] Re-training offline bandit weights"
echo "=========================================="
if ! python3 "${SCRIPT_DIR}/scripts/train_offline_bandit.py"; then
  echo "ERROR: offline bandit training failed — aborting"
  exit 1
fi
echo ""

set +e   # individual runs handle their own errors
source "${SCRIPT_DIR}/run-experiment-common.sh"

# Three runs across arrival distributions so the offline bandit
# can be compared 1:1 against the other strategies in plot_results.py.
run_strategy_experiment "OFFLINE_BANDIT" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT" "false" "autoscaler-sine"   60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT" "false" "autoscaler-step"   60000 300 8 10 2500 STEP     2 15000 "true"
