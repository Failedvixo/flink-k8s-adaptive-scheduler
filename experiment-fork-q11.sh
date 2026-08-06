#!/bin/bash
# Nexmark Q11 (user sessions — session windows) over the FORK's placement arms.
#
# Each arm runs the query alone, under every arrival distribution, so the
# campaign can find where the arms disagree — a scenario in which they tie is one
# no meta-scheduler can win on. The autoscaler drives the rescales, which are the
# only moments the arm is applied; the rescaled vertex here is `q11-sessions`.
#
# Requires: the JobManager on the fork (scripts/deploy-thesis-fork.sh) and the
# REST port-forward (kubectl port-forward -n flink svc/flink-jobmanager 8081:8081).
#
#   ./experiment-fork-q11.sh                      # 6 arms x 3 dists, ~6 h
#   PILOT=1 ./experiment-fork-q11.sh              # 5 min, SINE only, ~35 min
#   ARMS="BANDIT SARSA" ./experiment-fork-q11.sh  # the meta-schedulers
#
# Results (slim by default): results/fork-campaign/q11-<dist>/<ARM>/

set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

QUERIES=q11 exec "$SCRIPT_DIR/scripts/run-fork-campaign.sh"
