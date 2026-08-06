#!/bin/bash
# Nexmark Q8 (monitor new users — person ⨝ auction, tumbling 10s) over the FORK's placement arms.
#
# Each arm runs the query alone, under every arrival distribution, so the
# campaign can find where the arms disagree — a scenario in which they tie is one
# no meta-scheduler can win on. The autoscaler drives the rescales, which are the
# only moments the arm is applied; the rescaled vertex here is `new-users-join`.
#
# Requires: the JobManager on the fork (scripts/deploy-thesis-fork.sh) and the
# REST port-forward (kubectl port-forward -n flink svc/flink-jobmanager 8081:8081).
#
#   ./experiment-fork-q8.sh                      # 6 arms x 3 dists, ~6 h
#   PILOT=1 ./experiment-fork-q8.sh              # 5 min, SINE only, ~35 min
#   ARMS="BANDIT SARSA" ./experiment-fork-q8.sh  # the meta-schedulers
#
# Results (slim by default): results/fork-campaign/q8-<dist>/<ARM>/

set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

QUERIES=q8 exec "$SCRIPT_DIR/scripts/run-fork-campaign.sh"
