#!/bin/bash
# Nexmark Q7 (highest bid — global windowed max) over the FORK's placement arms.
#
# Each arm runs the query alone, under every arrival distribution, so the
# campaign can find where the arms disagree — a scenario in which they tie is one
# no meta-scheduler can win on. The autoscaler drives the rescales, which are the
# only moments the arm is applied; the rescaled vertex here is `q7-max-bid`.
#
# Requires: the JobManager on the fork (scripts/deploy-thesis-fork.sh) and the
# REST port-forward (kubectl port-forward -n flink svc/flink-jobmanager 8081:8081).
#
#   ./experiment-fork-q7.sh                      # 6 arms x 3 dists, ~6 h
#   PILOT=1 ./experiment-fork-q7.sh              # 5 min, SINE only, ~35 min
#   ARMS="BANDIT SARSA" ./experiment-fork-q7.sh  # the meta-schedulers
#
# Results (slim by default): results/fork-campaign/q7-<dist>/<ARM>/

set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

QUERIES=q7 exec "$SCRIPT_DIR/scripts/run-fork-campaign.sh"
