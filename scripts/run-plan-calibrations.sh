#!/bin/bash
# ============================================
# Rate calibration by FIXED placements: the capacity of each placement of the heavy operator
# ============================================
#
# WHY (2026-10-20). The campaign rate of a query is where the best placement at parallelism 2
# keeps up (>= 95%) and a bad one does not — the condition in which placement matters. Measuring
# it with the bandit would choose the evaluation rate by the method under evaluation, and
# calibrate-rate.sh measures at parallelism 3 with wherever the subtasks land (Q5 "failed" at
# 2000 ev/s while a good placement holds 4000). So, as for Q5 on 2026-10-19: one job per rate,
# and compare-plans.py applies four FIXED placements of the heavy operator in turns — all on
# fast, split fast+medium, all on medium, all on slow — and measures what each delivers. At a
# rate above a placement's capacity, what it delivers IS its capacity. No algorithm involved.
#
# Two rates per query, a lower and a higher guess, so at least one is above the best capacity.
# Q3 holds its state for 10 min (TTL), so its job warms up 10 min before the first plan; a
# replan restores state from the checkpoint, so later plans need only the normal settle.
#
# Usage:
#   scripts/run-plan-calibrations.sh                  # q8 q3 q6 q11
#   QUERIES="q6" RATES_Q6="10000 20000" scripts/run-plan-calibrations.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
QUERIES="${QUERIES:-q8 q3 q6 q11}"
LOG_DIR="${LOG_DIR:-$HOME/thesis-logs}"
NAMESPACE=flink
NODE="${THESIS_NODE:-minikube}"
SETTLE="${SETTLE:-120}"
WINDOW="${WINDOW:-120}"

jm_pod() {
    kubectl get pods -n "$NAMESPACE" -l component=jobmanager --field-selector=status.phase=Running \
        -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null
}
cancel_all() {
    local jm; jm=$(jm_pod)
    [ -n "$jm" ] || return 0
    kubectl exec -n "$NAMESPACE" "$jm" -- flink list 2>/dev/null | awk '/RUNNING/ {print $4}' \
        | while read -r jid; do
            kubectl exec -n "$NAMESPACE" "$jm" -- flink cancel "$jid" >/dev/null 2>&1
        done
    sleep 10
}

# Four placements of the heavy operator per query; the rest fixed so that only the heavy one
# moves as far as the slots allow (fast has 2 slots, medium 4, slow 6). Rule names are
# substrings of operator names; ":N" is subtask N. "bid-source", not "bid", in Q6: the heavy
# operator q6-winning-bids also contains "bid".
plans_for() {
    case "$1" in
        q8|q3) echo \
"heavy-fast=join=tm-3-fast,person=tm-2-medium,auction=tm-2-medium,sink=tm-1-slow
heavy-split=join:0=tm-3-fast,join:1=tm-2-medium,auction:0=tm-3-fast,auction:1=tm-2-medium,person=tm-2-medium,sink=tm-1-slow
heavy-medium=join=tm-2-medium,person=tm-2-medium,auction=tm-3-fast,sink=tm-1-slow
heavy-slow=join=tm-1-slow,person=tm-2-medium,auction=tm-2-medium,sink=tm-1-slow" ;;
        q6) echo \
"heavy-fast=q6-winning=tm-3-fast,bid-source=tm-2-medium,auction=tm-2-medium,avg=tm-1-slow,sink=tm-1-slow
heavy-split=q6-winning:0=tm-3-fast,q6-winning:1=tm-2-medium,auction:0=tm-3-fast,auction:1=tm-2-medium,bid-source=tm-2-medium,avg=tm-1-slow,sink=tm-1-slow
heavy-medium=q6-winning=tm-2-medium,auction=tm-2-medium,bid-source=tm-3-fast,avg=tm-1-slow,sink=tm-1-slow
heavy-slow=q6-winning=tm-1-slow,bid-source=tm-2-medium,auction=tm-2-medium,avg=tm-1-slow,sink=tm-1-slow" ;;
        q11) echo \
"heavy-fast=q11-sessions=tm-3-fast,bid-source=tm-2-medium,sink=tm-1-slow
heavy-split=q11-sessions:0=tm-3-fast,q11-sessions:1=tm-2-medium,bid-source=tm-2-medium,sink=tm-1-slow
heavy-medium=q11-sessions=tm-2-medium,bid-source=tm-3-fast,sink=tm-1-slow
heavy-slow=q11-sessions=tm-1-slow,bid-source=tm-2-medium,sink=tm-1-slow" ;;
    esac
}

for q in $QUERIES; do
    case "$q" in
        q8)  RATES="${RATES_Q8:-15000 22000}";  WIN=10; WARM=0;   SUBMIT=3 ;;
        # Q3 runs with a 60 s TTL since 2026-10-21 (RefNexmarkJob.q3StateTtlSec): 2 min fill it.
        q3)  RATES="${RATES_Q3:-20000 30000}";  WIN=10; WARM=120; SUBMIT=3 ;;
        q6)  RATES="${RATES_Q6:-60000 120000}"; WIN=10; WARM=0;   SUBMIT=3 ;;
        # Q11 has three stages: submitted at 3 it asks for 9 slots, and the pool can come up
        # without fast at all (2026-10-20: "libres slow 6, medium 3"). At 4 it asks for all 12.
        q11) RATES="${RATES_Q11:-40000 80000}"; WIN=10; WARM=0;   SUBMIT=4 ;;
        *) echo "consulta sin planes definidos: $q" >&2; continue ;;
    esac
    PLAN_ARGS=()
    while IFS= read -r p; do PLAN_ARGS+=(--plan "$p"); done <<< "$(plans_for "$q")"
    for rate in $RATES; do
        LOG="$LOG_DIR/planes-$q-r$rate-cpu2105.log"
        echo "################ $q a $rate ev/s: 4 planes fijos  ($(date +%H:%M)) -> $LOG"
        {
            cancel_all
            docker exec "$NODE" sh -c "rm -f /var/thesis/assignment /var/thesis/replan; \
                rm -rf /var/thesis/minio/flink-checkpoints/checkpoints/* /var/thesis/minio/.minio.sys/multipart/*" \
                </dev/null >/dev/null 2>&1
            "$SCRIPT_DIR/publish-arm.sh" RL
            for m in fast medium slow; do "$SCRIPT_DIR/limit-disk.sh" "$m" off >/dev/null 2>&1; done
            "$SCRIPT_DIR/limit-disk.sh" fast 5M
            JOB_S=$(( WARM + 4 * (SETTLE + WINDOW) + 900 ))
            KEEP=1 QUERY="$q" RATE="$rate" DIST=CONSTANT DURATION="$JOB_S" JOB_WINDOW="$WIN" SUBMIT_PAR="$SUBMIT" \
                SETTLE=10 SAMPLES=1 INTERVAL=1 "$SCRIPT_DIR/profile-job.sh" 2>&1 | grep -E "reescalado|ERROR|sometido|PUT"
            echo "  slices: $(docker exec "$NODE" head -1 /var/thesis/slices 2>/dev/null)"
            if [ "$WARM" -gt 0 ]; then
                echo "  calentando ${WARM}s para que el estado se llene (TTL de $q)"
                sleep "$WARM"
            fi
            (cd "$ROOT_DIR" && python3 -u scripts/compare-plans.py --rate "$rate" --reps 1 \
                --settle "$SETTLE" --window "$WINDOW" "${PLAN_ARGS[@]}")
            cancel_all
            "$SCRIPT_DIR/limit-disk.sh" fast off >/dev/null 2>&1
        } 2>&1 | tee "$LOG"
    done
done
echo ""
echo "RESUMEN de todas las calibraciones:"
for f in "$LOG_DIR"/planes-*-cpu2105.log; do
    echo "== $(basename "$f" .log)"
    sed -n '/RESUMEN a/,$p' "$f"
done
