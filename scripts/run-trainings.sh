#!/bin/bash
# ============================================
# Train one v4 table per query, at its campaign rate, one after the other
# ============================================
#
# WHY (2026-10-20). The generalisation protocol agreed with the professor trains v4 on ONE query
# and evaluates it on the others, rotating over the five CAPSys queries; all five tables must be
# ready before the per-query sessions start. Same recipe as the Q8 table of 2026-10-19 (from
# entrena-v4.log): run-placement-experiment.sh, arm RL with the characteriser, 30 reps of the
# schedule "wide -> 2*", warm-up 180 s + window 300 s, fast's disk at 5M, the heavy operator
# pinned to 1 subtask in a random half of the reps (RANDOM_PINS), the same four agent flags.
# Each query at its campaign rate (calibrated by fixed placements, scripts/run-plan-calibrations.sh):
# where the best placement at parallelism 2 keeps up and a bad one does not.
#
# Tables: results/characterizer/<query>-v4-cpu2105-r<rate>.json. An existing table is NOT
# overwritten: that query is skipped, so the script can be relaunched after an interruption.
#
# Usage:
#   scripts/run-trainings.sh                      # q8 q3 q5 q6 q11
#   QUERIES="q5 q6" scripts/run-trainings.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
QUERIES="${QUERIES:-q8 q3 q5 q6 q11}"
LOG_DIR="${LOG_DIR:-$HOME/thesis-logs}"
REPS="${REPS:-30}"

for q in $QUERIES; do
    # rate, JOB_WINDOW, heavy operator, submit width (wide enough that all 12 slots join the pool)
    case "$q" in
        q3)  RATE=30000; WIN=10;     HEAVY=q3-state-join;   SUBMIT=3 ;;   # TTL 60 s
        q5)  RATE=4000;  WIN="60:1"; HEAVY=hot-items-count; SUBMIT=3 ;;
        q6)  RATE=95000; WIN=10;     HEAVY=q6-winning-bids; SUBMIT=3 ;;
        q8)  RATE=20000; WIN=10;     HEAVY=new-users-join;  SUBMIT=3 ;;
        q11) RATE=95000; WIN=10;     HEAVY=q11-sessions;    SUBMIT=4 ;;
        *) echo "consulta desconocida: $q" >&2; continue ;;
    esac
    TABLE="results/characterizer/$q-v4-cpu2105-r$RATE.json"
    if [ -f "$ROOT_DIR/$TABLE" ]; then
        echo "################ $q: ya existe $TABLE — se omite"
        continue
    fi
    echo "################ $q a $RATE ev/s -> $TABLE  ($(date '+%d-%m %H:%M'))"
    (cd "$ROOT_DIR" && \
     TM_DEPLOYMENTS="flink-tm-fast:1 flink-tm-medium:1 flink-tm-slow:1" WIDE_HOLD=120 \
     CHARACTERISER=1 ARMS=RL JOB_CLASS=com.thesis.benchmark.nexmark.ref.RefNexmarkJob \
     QUERY="$q" RATE="$RATE" DIST=CONSTANT JOB_WINDOW="$WIN" MAX_EVENT_AGE=5000 \
     SUBMIT_PAR="$SUBMIT" TARGET_PAR=2 SCHEDULE="$SUBMIT 2*" REPS="$REPS" \
     WARMUP=180 WINDOW=300 SLOT_SHARING=PER_STAGE DISK_LIMIT="fast:5M" \
     AGENT_QTABLE="$TABLE" \
     AGENT_ARGS_RL="--machine-load --disk-suffix .rocksdb_compact_write_bytes --disk-floor 1e6 --latency-source record" \
     RANDOM_PINS="$HEAVY:1 none" \
       scripts/run-placement-experiment.sh) 2>&1 | tee "$LOG_DIR/entrena-v4-$q-cpu2105.log"
    echo "################ $q terminado ($(date '+%d-%m %H:%M')): $(ls -la "$ROOT_DIR/$TABLE" 2>/dev/null || echo 'SIN TABLA')"
done
