#!/bin/bash
# DEFAULT baseline on the old ConfigurableGraphJob benchmark.
# Patches flink-taskmanager.schedulerName=default-scheduler so vanilla
# kube-scheduler places pods (bypassing adaptive-scheduler entirely).
#
# Output: results/autoscaler-{const,sine,step}/DEFAULT/
#         — same shape as the other 8 strategies, so comparisons are 1:1.
#
# Total wall time: ~30 min (3 dists × ~10 min each).

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defensive: prevent Nexmark env-var leak from a parent shell — see experiment-offline-v2.sh.
unset JOB_CLASS EXTRA_JOB_ARGS HEAVY_VERTEX_PATTERN

# Safety net: restore schedulerName if anything bails mid-run, otherwise
# the next experiment would accidentally run under default-scheduler.
trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

echo "=========================================="
echo "  DEFAULT (vanilla kube-scheduler) — old benchmark"
echo "  ConfigurableGraphJob × {CONSTANT, SINE, STEP}"
echo "=========================================="

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

run_strategy_experiment "DEFAULT" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "DEFAULT" "false" "autoscaler-sine"   60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "DEFAULT" "false" "autoscaler-step"   60000 300 8 10 2500 STEP     2 15000 "true"
