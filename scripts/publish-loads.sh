#!/bin/bash
# ============================================
# Publish the per-vertex processing cost the assigner should weigh slices by
# ============================================
#
# WHY (measured 2026-08-17): the fork's balance term used to count slices per
# TaskManager. A count cannot tell apart arrangements that differ only in WHICH
# slices share a machine — with 4 slices on 3 TMs every arrangement is 2+1+1 —
# so the term cancelled out and ACO/GA were left optimising state locality
# alone. They put both expensive slices on one TaskManager in 10 of 10
# repetitions and lost 45% of throughput doing it.
#
# This publishes what the assigner was missing: how much each vertex actually
# costs. A slice's weight is then the sum over the vertices it contains, which
# differs between slices exactly when some operator runs at a lower parallelism
# than the rest — the case that matters.
#
# The cost is `busyTimeMsPerSecond` averaged over a vertex's subtasks: CPU-time
# per second of wall clock, which is Flink's own measurement of what the paper
# calls RD^c_Tj. Dividing it by `numRecordsInPerSecond` would give the paper's
# rho^Tj_c (cost per record) — a rate-independent signature of the vertex —
# but the absolute figure is what the balance term needs, since two slices are
# only worth separating if they cost a lot RIGHT NOW.
#
# Written the same way as the arm: temp file plus atomic rename, because the
# JobManager may read at any instant and a half-written file would change a
# placement. The assigner caches it for a second and keeps the last good copy on
# a failed read.
#
# Usage:
#   scripts/publish-loads.sh              # measure the running job and publish
#   scripts/publish-loads.sh --read       # what is published right now
#   scripts/publish-loads.sh --clear      # remove it; every slice weighs 1.0 again

set -eu

NODE="${THESIS_NODE:-minikube}"
THESIS_DIR=/var/thesis
LOADS_FILE="$THESIS_DIR/loads"
TMP_FILE="$THESIS_DIR/.loads.tmp"
NAMESPACE=flink

jm_curl() {
    kubectl exec -n "$NAMESPACE" deployment/flink-jobmanager -- \
        curl -s -m 15 "http://localhost:8081$1" 2>/dev/null
}

case "${1:-}" in
    --read)
        minikube ssh -n "$NODE" -- "sudo cat $LOADS_FILE 2>/dev/null || echo '(nothing published)'" | tr -d '\r'
        exit 0
        ;;
    --clear)
        minikube ssh -n "$NODE" -- "sudo rm -f $LOADS_FILE" >/dev/null
        echo "cleared — every slice weighs 1.0 again"
        exit 0
        ;;
    # Disable/enable rather than clear/re-measure: re-measuring needs a running job,
    # and the poisoning phase of the placement experiment has to turn the weights off
    # and back on around a rescale without one.
    --disable)
        # Moving the file away does NOT disable the weights: the assigner keeps its last
        # good copy when a read fails, which is deliberate (a half-written file must not
        # change a placement) but makes deletion a no-op. Measured 2026-08-17 — the
        # poisoning phase silently kept the weights and never produced the bad layout.
        #
        # Instead publish a file the assigner can read perfectly well but that names no
        # vertex of this job: sliceLoads() then finds no match, returns null, and every
        # slice falls back to weight 1.0. Same effect, no fork change, takes effect on
        # the next read like any other publish.
        minikube ssh -n "$NODE" -- "sudo mv -f $LOADS_FILE $LOADS_FILE.off 2>/dev/null || true; \
            printf '%s\n' '00000000000000000000000000000000 1.0' | sudo tee $LOADS_FILE >/dev/null; \
            sudo chmod 644 $LOADS_FILE" >/dev/null
        echo "disabled — published a no-match file, so every slice weighs 1.0 until --enable"
        exit 0
        ;;
    --enable)
        minikube ssh -n "$NODE" -- "sudo mv -f $LOADS_FILE.off $LOADS_FILE 2>/dev/null || true" >/dev/null
        echo "enabled"
        exit 0
        ;;
esac

JID=$(jm_curl "/jobs/overview" | python3 -c "
import json, sys
jobs = [j for j in json.load(sys.stdin).get('jobs', []) if j.get('state') == 'RUNNING']
print(jobs[0]['jid'] if jobs else '')" 2>/dev/null || true)
if [ -z "$JID" ]; then
    echo "ERROR: no RUNNING job to measure" >&2; exit 1
fi

DETAIL=$(jm_curl "/jobs/$JID")
VERTICES=$(echo "$DETAIL" | python3 -c "
import json, sys
for v in json.load(sys.stdin).get('vertices', []):
    print(v['id'], v.get('parallelism', 1), v.get('name', '')[:40].replace(' ', '_'))" 2>/dev/null)

CONTENT=""
while read -r VID PAR NAME; do
    [ -n "$VID" ] || continue
    TOTAL=0; COUNT=0
    for i in $(seq 0 $((PAR - 1))); do
        BUSY=$(jm_curl "/jobs/$JID/vertices/$VID/subtasks/$i/metrics?get=busyTimeMsPerSecond" |
            python3 -c "
import json, sys, math
try:
    v = float(json.load(sys.stdin)[0]['value'])
    print(v if math.isfinite(v) else 0.0)
except Exception:
    print(0.0)" 2>/dev/null || echo 0)
        TOTAL=$(python3 -c "print($TOTAL + $BUSY)")
        COUNT=$((COUNT + 1))
    done
    # Per-SUBTASK cost, not per-vertex: a slice contains one subtask, so summing
    # vertex totals would weigh a wide operator as if every slice carried all of it.
    COST=$(python3 -c "print(round($TOTAL / max(1, $COUNT), 3))")
    echo "  $NAME  par=$PAR  ${COST} ms/s per subtask"
    CONTENT="${CONTENT}${VID} ${COST}
"
done <<< "$VERTICES"

if [ -z "$CONTENT" ]; then
    echo "ERROR: measured nothing" >&2; exit 1
fi

minikube ssh -n "$NODE" -- "sudo mkdir -p $THESIS_DIR && \
    printf '%s' '$CONTENT' | sudo tee $TMP_FILE >/dev/null && \
    sudo mv -f $TMP_FILE $LOADS_FILE && sudo chmod 644 $LOADS_FILE" >/dev/null

echo "published -> $LOADS_FILE  (takes effect on the next rescale)"
