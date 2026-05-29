# experiment-bandit.sh
#!/bin/bash
source "$(dirname "$0")/run-experiment-common.sh"
run_strategy_experiment "BANDIT" "false" "autoscaler-step" 60000 300 8 10 2500 STEP 2 15000 "true"