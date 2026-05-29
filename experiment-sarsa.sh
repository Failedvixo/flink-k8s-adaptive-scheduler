#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
# 600k ev/s, paralelismo 8 → usa los 10 slots holgadamente y satura CPU
run_strategy_experiment "SARSA" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"