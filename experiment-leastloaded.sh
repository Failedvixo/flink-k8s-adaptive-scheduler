#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "LEAST_LOADED" "false" "autoscaler-step" 60000 300 8 10 2500 STEP 2 15000 "true"