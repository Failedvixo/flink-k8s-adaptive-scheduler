#!/bin/bash
# ============================================
# Per-record operator costs, measured where nothing is competing
# ============================================
#
# WHY (2026-09-02, from CAPSys §5 "Cost profiling"). scripts/publish-loads.sh
# measures busyTimeMsPerSecond DURING the campaign, with every operator spread
# across machines and contending with the others. That number is not "what this
# operator costs" but "what it cost in that placement, with those neighbours, on
# that machine" — so the input to the placement decision depends on the placement.
#
# busyTimeMsPerSecond is also capped at 1000 per subtask, so an operator starved of
# CPU cannot report more: it reports LESS, because it spends its time waiting. A
# cost function reads that as cheap and packs more work onto the machine already
# struggling. The feedback runs the wrong way.
#
# HOW WE DEVIATE FROM CAPSys, and why. They deploy each operator alone on its own
# TaskManager. With 3 TaskManagers and a 4-group query that needs several runs.
# Instead this profiles the whole job at a rate below the calibrated ceiling, where
# every machine has headroom. Absence of contention is the property isolation buys;
# below saturation there is little contention to remove.
#
# THE RATE IS A TRADE-OFF, and the first attempt got it wrong in both directions.
# Too LOW and there is nothing to measure: at 10000 rec/s the busy medians were 0,
# 3, 6, 8, 30, 31 and 47 out of 1000, and two subtasks of the SAME operator doing
# the SAME work reported 30 and exactly 0. That is quantisation, not cost. Too HIGH
# and the threads contend. Note that zero backpressure does NOT certify absence of
# contention: it says the job keeps up with the source, not that its threads each
# get a core. At the calibrated ceiling (75000 for q8) the job runs at its CPU
# limit, which is the worst place to profile.
#
# The rule is the LOWEST rate that still resolves, and it need not be guessed:
# profile at two rates and compare the per-record costs. If they agree, contention
# is not distorting them in that range — the property CAPSys buys with isolation,
# demonstrated instead of assumed. If they diverge, go lower.
#
# All sampling and arithmetic live in scripts/profile_operators.py. Doing it from
# bash needed five nested levels of quoting and broke at runtime in a way `bash -n`
# could not see; this script now only submits the job and cancels it.
#
# THE STATE DIMENSION NEEDS METRICS THAT ARE OFF BY DEFAULT:
#   scripts/profile-operators.sh --enable-metrics    # one-time, restarts the TMs
#
# Usage:
#   scripts/profile-operators.sh                      # q8 at 60000, publishes nothing
#   PROFILE_RATE=40000 scripts/profile-operators.sh
#   scripts/profile-operators.sh --publish 75000      # write /var/thesis/loads

set -eu

NAMESPACE=flink
NODE="${THESIS_NODE:-minikube}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

QUERY="${QUERY:-q8}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.NexmarkRealJob}"
SUBMIT_PAR="${SUBMIT_PAR:-3}"
JOB_WINDOW="${JOB_WINDOW:-10}"
DIST="${DIST:-CONSTANT}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
PROFILE_RATE="${PROFILE_RATE:-60000}"
WARMUP="${WARMUP:-120}"
SAMPLES="${SAMPLES:-9}"
INTERVAL="${INTERVAL:-4}"

TMS="flink-tm-fast flink-tm-medium flink-tm-slow"

if [ "${1:-}" = "--enable-metrics" ]; then
    echo "Enabling RocksDB native metrics on the TaskManagers (they restart)..."
    for key in bytes-read bytes-written block-cache-hit block-cache-miss; do
        for d in $TMS; do
            current=$(kubectl get deploy "$d" -n "$NAMESPACE" \
                -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}')
            case "$current" in *"state.backend.rocksdb.metrics.$key:"*) continue ;; esac
            export UPDATED="$current
    state.backend.rocksdb.metrics.$key: true"
            kubectl patch deploy "$d" -n "$NAMESPACE" --type=strategic \
                -p "$(python3 "$SCRIPT_DIR/lib_env_patch.py")" >/dev/null
        done
    done
    for d in $TMS; do
        kubectl rollout status "deployment/$d" -n "$NAMESPACE" --timeout=300s
    done
    echo "Done. Clean up the failed pods a rollout leaves behind:"
    echo "  kubectl delete pods -n $NAMESPACE --field-selector status.phase=Failed"
    exit 0
fi

PUBLISH_AT=""
if [ "${1:-}" = "--publish" ]; then
    PUBLISH_AT="${2:?usage: --publish <target rate>}"
fi

JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)
[ -n "$JM_POD" ] || { echo "ERROR: no JobManager pod" >&2; exit 1; }

LOCAL_JAR="$ROOT_DIR/flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar"
[ -f "$LOCAL_JAR" ] || { echo "ERROR: build the benchmark first" >&2; exit 1; }
kubectl cp "$LOCAL_JAR" "$NAMESPACE/$JM_POD:/tmp/nexmark.jar"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT_DIR="$ROOT_DIR/results/operator-profile/$STAMP"
mkdir -p "$OUT_DIR"

minikube ssh -n "$NODE" -- "sudo cat /var/thesis/speeds 2>/dev/null" 2>/dev/null |
    tr -d '\r' > "$OUT_DIR/speeds.txt" || true

echo "=========================================="
echo "  Operator profiling — $QUERY at $PROFILE_RATE rec/s"
echo "  parallelism $SUBMIT_PAR, slot sharing $SLOT_SHARING"
echo "=========================================="

python3 "$SCRIPT_DIR/lib_cancel_jobs.py" --pod "$JM_POD" || true
sleep 5

# THE SLACK HAS TO COVER THE SAMPLING ITSELF, not just its nominal interval.
# Measured 2026-09-02: each round issues one `kubectl exec` per vertex plus one per
# subtask — about 32 calls at half a second to two seconds each — so nine rounds of
# a nominally 36-second sampling took several minutes and the job hit its deadline
# before the diagnostic could run, which reported an empty table for a job that had
# worked perfectly. Budget 3 seconds of round-trip per call.
ROUND_COST=$(( SAMPLES * 32 * 3 ))
DURATION=$(( WARMUP + SAMPLES * INTERVAL + ROUND_COST + 180 ))
SUBMIT_OUT=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
    flink run -d -c "$JOB_CLASS" /tmp/nexmark.jar \
    "$PROFILE_RATE" "$DURATION" "$SUBMIT_PAR" "$JOB_WINDOW" "0" "$DIST" \
    "$SUBMIT_PAR" "0" "$QUERY" "${ZIPF_ALPHA:-0.5}" "${HOT_POOL:-1000}" \
    "$SLOT_SHARING" 2>&1)
JID=$(printf '%s' "$SUBMIT_OUT" | grep -oE '[0-9a-f]{32}' | head -1)
if [ -z "$JID" ]; then
    echo "ERROR: could not submit:" >&2
    printf '%s\n' "$SUBMIT_OUT" | tail -20 >&2
    exit 1
fi
echo "  JobID: $JID"

python3 "$SCRIPT_DIR/lib_wait_running.py" --pod "$JM_POD" --job "$JID" ||
    { echo "ERROR: the job never reached RUNNING" >&2; exit 1; }

echo "  warming up ${WARMUP}s (state has to accumulate before the join costs anything)..."
sleep "$WARMUP"

echo "  sampling ${SAMPLES}x every ${INTERVAL}s..."
python3 "$SCRIPT_DIR/profile_operators.py" \
    --pod "$JM_POD" --job "$JID" --out "$OUT_DIR" --rate "$PROFILE_RATE" \
    --samples "$SAMPLES" --interval "$INTERVAL"

# Run WHILE THE JOB IS STILL ALIVE, and before cancelling it. The profile says how
# much work each operator does; this says what is stopping the pipeline, and the
# two only answer the question together. Measured 2026-09-02: at the calibrated
# ceiling the busiest vertex sat at 218 ms/s out of 1000, so whatever sets that
# ceiling is not any operator's CPU, and the profile alone cannot say what it is.
echo ""
python3 "$SCRIPT_DIR/diagnose-bottleneck.py" --pod "$JM_POD" --job "$JID" || true

python3 "$SCRIPT_DIR/lib_cancel_jobs.py" --pod "$JM_POD" || true

if [ -n "$PUBLISH_AT" ]; then
    echo ""
    echo "Publishing costs scaled to $PUBLISH_AT rec/s..."
    python3 "$SCRIPT_DIR/lib_publish_profile.py" \
        --profile "$OUT_DIR/profile.json" --target "$PUBLISH_AT" --node "$NODE"
fi
