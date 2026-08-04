#!/bin/bash
# ============================================
# Measure how each SlotAssigner strategy spreads slices at rescale
# ============================================
#
# The assigner only has a real choice when the slot pool holds MORE free slots
# than the job needs. That happens on a scale-DOWN: the pool keeps the slots it
# already had while the job asks for fewer slices. This script reproduces that
# event repeatedly and records where the slices landed.
#
# Per repetition: submit at -p SUBMIT_PAR, wait for the initial assignment, then
# PUT resource-requirements with upperBound=TARGET_PAR and read the assignment
# the rescale triggers.
#
# The stock (DEFAULT) placement follows the incidental iteration order of the
# free slots, so its spread varies from run to run — which is why this measures
# a distribution over repetitions instead of trusting a single pair of runs.
#
# Usage:
#   scripts/measure-assigner-spread.sh [STRATEGY] [REPETITIONS]
#
# Assumes the JobManager already runs the fork with the given strategy:
#   scripts/deploy-thesis-fork.sh STRATEGY

set -u

STRATEGY="${1:-DEFAULT}"
REPS="${2:-8}"
SUBMIT_PAR="${SUBMIT_PAR:-6}"
TARGET_PAR="${TARGET_PAR:-3}"
NAMESPACE=flink
JOB_JAR=/opt/flink/examples/streaming/TopSpeedWindowing.jar

RESULTS_DIR="${RESULTS_DIR:-results/assigner-spread}"
mkdir -p "$RESULTS_DIR"
OUT="$RESULTS_DIR/${STRATEGY}-$(date +%Y%m%d-%H%M%S).csv"

# Right after a rollout an old pod can still be terminating, and it sorts ahead
# of the new one, so pick the newest pod that is actually Running.
JM=$(kubectl get pods -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')
RUNNING_STRATEGY=$(kubectl get pod -n "$NAMESPACE" "$JM" \
    -o jsonpath='{.spec.containers[0].env[?(@.name=="THESIS_ASSIGN_STRATEGY")].value}')

if [ "$RUNNING_STRATEGY" != "$STRATEGY" ]; then
    echo "ERROR: JobManager runs strategy '$RUNNING_STRATEGY', not '$STRATEGY'."
    echo "       Run: scripts/deploy-thesis-fork.sh $STRATEGY"
    exit 1
fi

echo "=========================================="
echo "  Assigner spread: $STRATEGY  ($REPS reps)"
echo "  Rescale: p=$SUBMIT_PAR -> upperBound=$TARGET_PAR"
echo "  JobManager: $JM"
echo "=========================================="
echo "rep,slices,free_slots,tms_available,tms_used,max_slices_per_tm,balanced,spread" > "$OUT"

count_assign_lines() {
    kubectl logs -n "$NAMESPACE" "$JM" 2>/dev/null | grep -c "THESIS_ASSIGN"
}

# Blocks until the assigner has logged $1 more lines than $2, or gives up.
wait_for_assign_lines() {
    local target="$1"
    for _ in $(seq 1 40); do
        [ "$(count_assign_lines)" -ge "$target" ] && return 0
        sleep 2
    done
    return 1
}

for rep in $(seq 1 "$REPS"); do
    printf "[rep %2d/%d] " "$rep" "$REPS"

    before=$(count_assign_lines)

    JID=$(kubectl exec -n "$NAMESPACE" "$JM" -- \
        flink run -d -p "$SUBMIT_PAR" "$JOB_JAR" 2>/dev/null | grep -oP 'JobID \K\w+')
    if [ -z "${JID:-}" ]; then
        echo "submit failed, skipping"
        continue
    fi

    # First assignment: the initial placement, where freeSlots == slices.
    if ! wait_for_assign_lines $((before + 1)); then
        echo "no initial assignment, skipping"
        kubectl exec -n "$NAMESPACE" "$JM" -- flink cancel "$JID" >/dev/null 2>&1
        continue
    fi

    # Flink rejects a partial vertex payload, so every vertex must be present.
    # Only the parallel one gets a widened range; the source stays pinned.
    payload=$(kubectl exec -n "$NAMESPACE" "$JM" -- curl -s "http://localhost:8081/jobs/$JID" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
reqs = {}
for v in data.get('vertices', []):
    if v.get('parallelism') == 1:
        reqs[v['id']] = {'parallelism': {'lowerBound': 1, 'upperBound': 1}}
    else:
        reqs[v['id']] = {'parallelism': {'lowerBound': 1, 'upperBound': ${TARGET_PAR}}}
print(json.dumps(reqs))")

    http=$(kubectl exec -n "$NAMESPACE" "$JM" -- curl -s -o /dev/null -w "%{http_code}" \
        -X PUT "http://localhost:8081/jobs/$JID/resource-requirements" \
        -H "Content-Type: application/json" -d "$payload" 2>/dev/null)
    if [ "$http" != "200" ]; then
        echo "rescale failed (HTTP $http), skipping"
        kubectl exec -n "$NAMESPACE" "$JM" -- flink cancel "$JID" >/dev/null 2>&1
        continue
    fi

    # Second assignment: the rescale, where freeSlots > slices.
    if ! wait_for_assign_lines $((before + 2)); then
        echo "no rescale assignment, skipping"
        kubectl exec -n "$NAMESPACE" "$JM" -- flink cancel "$JID" >/dev/null 2>&1
        continue
    fi

    line=$(kubectl logs -n "$NAMESPACE" "$JM" 2>/dev/null | grep "THESIS_ASSIGN" | tail -1)

    slices=$(echo "$line" | grep -oP 'slices=\K[0-9]+')
    free_slots=$(echo "$line" | grep -oP 'freeSlots=\K[0-9]+')
    tms_avail=$(echo "$line" | grep -oP 'tmsAvailable=\K[0-9]+')
    tms_used=$(echo "$line" | grep -oP 'tmsUsed=\K[0-9]+')
    spread=$(echo "$line" | grep -oP 'spread=\{\K[^}]*')
    max_per_tm=$(echo "$spread" | tr ',' '\n' | grep -oP '=\K[0-9]+' | sort -rn | head -1)

    # "Balanced" = the slices were dealt over as many TaskManagers as possible,
    # i.e. no TM carries an extra slice while another sits idle.
    ideal_tms=$(( slices < tms_avail ? slices : tms_avail ))
    if [ "$tms_used" -eq "$ideal_tms" ]; then balanced=yes; else balanced=no; fi

    echo "$rep,$slices,$free_slots,$tms_avail,$tms_used,$max_per_tm,$balanced,\"$spread\"" >> "$OUT"
    echo "slices=$slices freeSlots=$free_slots tmsUsed=$tms_used/$tms_avail maxPerTM=$max_per_tm balanced=$balanced"

    kubectl exec -n "$NAMESPACE" "$JM" -- flink cancel "$JID" >/dev/null 2>&1
    sleep 3
done

echo ""
echo "=========================================="
total=$(($(wc -l < "$OUT") - 1))
bal=$(grep -c ',yes,' "$OUT" || true)
echo "  $STRATEGY: $bal/$total repetitions balanced"
echo "  -> $OUT"
echo "=========================================="
