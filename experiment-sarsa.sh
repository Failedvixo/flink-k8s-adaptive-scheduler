#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
# 600k ev/s, paralelismo 8 → usa los 10 slots holgadamente y satura CPU
run_strategy_experiment "SARSA" "false" "STEP" 200000 180 8 10 1000 STEP