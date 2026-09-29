#!/bin/bash
# ============================================
# Publish the placement arm the JobManager should use from now on
# ============================================
#
# The forked SlotAssigner re-reads /var/thesis/arm on every assignment round, so
# writing this file is how the external meta-scheduler changes strategy between
# two rescales of the SAME run — Phase 2 could only switch arms by restarting the
# JobManager, which made learning across a run impossible.
#
# The write is atomic (write a temp file, then rename over the target) because
# the JobManager may read the file at any instant: a plain truncate-and-write
# leaves a window where the assigner sees an empty file. The assigner survives
# that window by keeping its last good arm, but an atomic rename removes it.
#
# The JobManager mounts the DIRECTORY /var/thesis, not the file, on purpose: a
# single-file bind mount pins the inode, so a renamed file would never be seen
# inside the container.
#
# Usage:
#   scripts/publish-arm.sh STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|LPT|PACK|ACO|GA|OPTIMAL|RL
#   scripts/publish-arm.sh --read      # what is published right now

set -eu

NODE="${THESIS_NODE:-minikube}"
THESIS_DIR=/var/thesis
# `docker exec` FIRST, `minikube ssh` ONLY AS FALLBACK, AND BOTH BOUNDED (2026-09-28).
# `minikube ssh` does not merely fail on this host, it HANGS: measured that night, this script
# sat seven minutes inside one ssh call with no timeout, and because the driver publishes the
# arm before submitting, the whole cell stalled before the job even existed. The same defect
# had already cost a run through the checkpoint cleanup. `docker exec` runs as root inside the
# node container, so it needs no `sudo` — and `sudo` with stdin attached but no TTY is itself
# a hang, waiting for a password nobody can type. `timeout -k` is what makes the bound real:
# plain `timeout` sends TERM and then waits forever if the child ignores it.
node_run() {
    timeout -k 5 "${NODE_TIMEOUT:-30}" docker exec "$NODE" sh -c "$1" </dev/null 2>/dev/null \
        || timeout -k 5 "${NODE_TIMEOUT:-30}" minikube ssh -n "$NODE" -- "$2" </dev/null 2>/dev/null
}

ARM_FILE="$THESIS_DIR/arm"
TMP_FILE="$THESIS_DIR/.arm.tmp"

ARM="${1:-}"

if [ "$ARM" = "--read" ]; then
    node_run "cat $ARM_FILE 2>/dev/null || echo '(none published)'" \
             "sudo cat $ARM_FILE 2>/dev/null || echo '(none published)'" | tr -d '\r'
    exit 0
fi

case "$ARM" in
    STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|LPT|PACK|ACO|GA|OPTIMAL|RL) ;;
    *)
        echo "Usage: $0 STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|LPT|PACK|ACO|GA|OPTIMAL|RL  |  $0 --read" >&2
        exit 1
        ;;
esac

# RETRIED. `minikube ssh` opens a fresh SSH session against the node container and
# fails with "handshake failed: EOF" when that container is busy — measured
# 2026-09-03, mid-campaign, with the cluster under a saturating Q8. The campaign
# driver treats a failed publish as a reason to SKIP the arm, so one flaky
# handshake cost a whole 20-minute cell and left a campaign with one arm out of two.
for _attempt in 1 2 3 4 5; do
    if node_run "mkdir -p $THESIS_DIR && printf '%s\n' '$ARM' > $TMP_FILE && chmod 644 $TMP_FILE && mv $TMP_FILE $ARM_FILE" \
                "sudo mkdir -p $THESIS_DIR && printf '%s\n' '$ARM' | sudo tee $TMP_FILE >/dev/null && sudo chmod 644 $TMP_FILE && sudo mv $TMP_FILE $ARM_FILE" \
        >/dev/null 2>&1; then
        PUBLISHED=1
        break
    fi
    sleep 3
done

if [ "${PUBLISHED:-0}" != 1 ]; then
    echo "ERROR: could not write $ARM_FILE after 5 attempts (minikube ssh)" >&2
    exit 1
fi

# Read it back. Writing the file is not the same as the assigner having a valid arm,
# and a silent mismatch here is invisible until the campaign is analysed.
CONFIRMED=$(node_run "cat $ARM_FILE 2>/dev/null" "sudo cat $ARM_FILE 2>/dev/null" | tr -d '\r\n')
if [ "$CONFIRMED" != "$ARM" ]; then
    echo "ERROR: published '$ARM' but the file reads '$CONFIRMED'" >&2
    exit 1
fi

echo "$ARM"
