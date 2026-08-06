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
#   scripts/publish-arm.sh STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|ACO|GA
#   scripts/publish-arm.sh --read      # what is published right now

set -eu

NODE="${THESIS_NODE:-minikube}"
THESIS_DIR=/var/thesis
ARM_FILE="$THESIS_DIR/arm"
TMP_FILE="$THESIS_DIR/.arm.tmp"

ARM="${1:-}"

if [ "$ARM" = "--read" ]; then
    minikube ssh -n "$NODE" -- "sudo cat $ARM_FILE 2>/dev/null || echo '(none published)'" | tr -d '\r'
    exit 0
fi

case "$ARM" in
    STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|ACO|GA) ;;
    *)
        echo "Usage: $0 STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|ACO|GA  |  $0 --read" >&2
        exit 1
        ;;
esac

minikube ssh -n "$NODE" -- \
    "sudo mkdir -p $THESIS_DIR && printf '%s\n' '$ARM' | sudo tee $TMP_FILE >/dev/null && sudo chmod 644 $TMP_FILE && sudo mv $TMP_FILE $ARM_FILE"

echo "$ARM"
