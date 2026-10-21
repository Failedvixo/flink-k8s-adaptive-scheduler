#!/bin/bash
# ============================================
# Step calibrations with the bandit at parallelism 2: where does a GOOD placement stop keeping up?
# ============================================
#
# WHY (2026-10-20). calibrate-rate.sh measures at parallelism 3 with whatever placement the
# heavy subtasks land on, and with CPU 2/1/0.5 one of three subtasks almost surely lands on a
# starved machine: Q5 "failed" at 2000 ev/s while two campaigns had it at 99% at 4000 with a good
# placement. The campaign rate is where the best placement at parallelism 2 keeps up (>= 95%) and
# a bad one does not — so it is measured with the bandit, which finds a good placement, over a
# ladder of rates (STEPS, 5 min each, aligned to the fixed origin, so 30 min cover every step).
#
# Runs one after the other, each through run-replan-experiment.sh (which cleans up after itself);
# a failed query does not stop the others. Logs in ~/thesis-logs/escalones-<query>-cpu2105.log.
#
# Usage:
#   scripts/run-step-calibrations.sh                 # q6, q11, q8
#   QUERIES="q8" scripts/run-step-calibrations.sh    # only some
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
QUERIES="${QUERIES:-q6 q11 q8}"
LOG_DIR="${LOG_DIR:-$HOME/thesis-logs}"
NAMESPACE=flink

leftover_jobs() {
    local jm
    jm=$(kubectl get pods -n "$NAMESPACE" -l component=jobmanager --field-selector=status.phase=Running \
        -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)
    [ -n "$jm" ] || return 0
    kubectl exec -n "$NAMESPACE" "$jm" -- flink list 2>/dev/null | awk '/RUNNING/ {print $4}' \
        | while read -r jid; do
            echo "  ! job $jid seguía corriendo: se cancela antes de seguir"
            kubectl exec -n "$NAMESPACE" "$jm" -- flink cancel "$jid" >/dev/null 2>&1
        done
}

for q in $QUERIES; do
    case "$q" in
        q6)  RATE=20000; WIN=10;     STEPS="5000,10000,20000,30000,40000,60000" ;;
        q11) RATE=60000; WIN=10;     STEPS="20000,40000,60000,80000,100000,120000" ;;
        q8)  RATE=15000; WIN=10;     STEPS="12000,14000,16000,18000,20000,22000" ;;
        q3)  RATE=15000; WIN=10;     STEPS="8000,10000,12000,14000,16000,18000" ;;
        q5)  RATE=4000;  WIN="60:1"; STEPS="2000,3000,4000,5000,6000,7000" ;;
        *) echo "consulta desconocida: $q" >&2; continue ;;
    esac
    echo "################ $q: escalones $STEPS (5 min cada uno)  ($(date +%H:%M))"
    leftover_jobs
    QUERY="$q" JOB_WINDOW="$WIN" RATE="$RATE" DIST="STEPS:300:$STEPS" DURATION=1800 ARM=BANDIT \
        "$SCRIPT_DIR/run-replan-experiment.sh" 2>&1 | tee "$LOG_DIR/escalones-$q-cpu2105.log"
    echo "################ $q terminado ($(date +%H:%M))"
done
leftover_jobs
echo "listo: $QUERIES"
