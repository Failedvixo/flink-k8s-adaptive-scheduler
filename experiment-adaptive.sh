#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "ADAPTIVE" "true" "autoscaler-sine" 60000 300 8 10 2500 SINE 2 15000 "true"