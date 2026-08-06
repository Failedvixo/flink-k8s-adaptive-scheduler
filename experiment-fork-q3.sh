#!/bin/bash
# Nexmark Q3 (local item suggestion — keyed state join) over the FORK's placement arms.
#
# Each arm runs the query alone, under every arrival distribution, so the
# campaign can find where the arms disagree — a scenario in which they tie is one
# no meta-scheduler can win on. The autoscaler drives the rescales, which are the
# only moments the arm is applied; the rescaled vertex here is `q3-state-join`.
#
# Requires: the JobManager on the fork (scripts/deploy-thesis-fork.sh) and the
# REST port-forward (kubectl port-forward -n flink svc/flink-jobmanager 8081:8081).
#
#   ./experiment-fork-q3.sh                      # 6 arms x 3 dists, ~6 h
#   PILOT=1 ./experiment-fork-q3.sh              # 5 min, SINE only, ~35 min
#   ARMS="BANDIT SARSA" ./experiment-fork-q3.sh  # the meta-schedulers
#
# Results (slim by default): results/fork-campaign/q3-<dist>/<ARM>/

set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

QUERIES=q3 exec "$SCRIPT_DIR/scripts/run-fork-campaign.sh"
