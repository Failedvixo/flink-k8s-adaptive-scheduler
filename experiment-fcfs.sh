#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
# Calibración inicial: 200k ev/s, 90s, paralelismo 4, ventana 10s, cpuLoad=100
run_strategy_experiment "FCFS" "false" "step" 200000 180 8 10 1000 STEP  