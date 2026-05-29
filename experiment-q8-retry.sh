#!/bin/bash
# Re-run only the 5 Q8 combinations that failed in the 2026-05-21 run:
#   ADAPTIVE × {const, sine, step}   (job FAILED / RESTARTING — likely an ADAPTIVE-Q8 bug)
#   BANDIT  × sine                   (RESTARTING tput=18)
#   SARSA   × sine                   (RESTARTING tput=11)
#
# Same env vars and 12-arg invocation as experiment-q8.sh, so the existing
# results/q8-{dist}/{STRATEGY}/ are overwritten cleanly.
#
# Wall time: 5 runs × ~10 min ≈ 50 min.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
export EXTRA_JOB_ARGS="q8 0.5 1000"
export HEAVY_VERTEX_PATTERN="new-users-join"

# Same safety net as experiment-q8.sh in case a DEFAULT slips in via Ctrl-C, etc.
trap '_ec=$?; kubectl patch deployment flink-taskmanager -n flink --type=strategic \
  -p "{\"spec\":{\"template\":{\"spec\":{\"schedulerName\":\"adaptive-scheduler\"}}}}" 2>/dev/null || true; exit $_ec' EXIT INT TERM

echo "=========================================="
echo "  Q8 retry — 5 failed combinations"
echo "=========================================="

set +e
source "${SCRIPT_DIR}/run-experiment-common.sh"

run_strategy_experiment "ADAPTIVE" "true" "q8-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "BANDIT"   "false" "q8-sine"  60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "SARSA"    "false" "q8-sine"  60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "ADAPTIVE" "true"  "q8-sine"  60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "ADAPTIVE" "true"  "q8-step"  60000 300 8 10 2500 STEP     2 15000 "true"
