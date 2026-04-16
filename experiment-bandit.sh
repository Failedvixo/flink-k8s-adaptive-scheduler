# experiment-bandit.sh
#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "BANDIT" "false" "STEP" 200000 180 8 10 1000 STEP