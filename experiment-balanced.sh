#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "BALANCED" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"