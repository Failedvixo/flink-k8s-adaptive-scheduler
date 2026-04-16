#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "LEAST_LOADED" "false" "step" 200000 180 8 10 1000 STEP