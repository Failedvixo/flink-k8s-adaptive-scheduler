#!/bin/bash
# ============================================
# Put the job in the configuration the campaigns measure, then profile it
# ============================================
#
# WHY THIS EXISTS (measured 2026-09-20). The load vector was being measured by
# submitting Q8 straight at parallelism 2, and that submission cannot produce a
# legitimate placement: the slot pool is built from what the JOB asked for, not from
# what the cluster has, so at parallelism 2 there are 8 free slots and `tm-2-medium`
# never enters the decision at all. The measured job placed 6 of its 8 slices on
# `tm-1-slow` — one core, six slots — and one on `tm-3-fast`, which is the degenerate
# spread {slow=6, fast=2} that run-paired-campaign.sh documents for the same reason.
#
# What came out of it was not a cost vector. Every source and every operator on the
# slow machine was backpressured (up to 326 ms/s), the two subtasks of one Map read
# 103 and 8 ms/s on the SAME machine at the SAME rate, and the join — the one operator
# that got a machine to itself — sat 94% idle. Busy time under starvation measures the
# queue, not the work.
#
# So the vector has to be measured the way the campaign runs: submit at SUBMIT_PAR so
# all twelve slots (and therefore all three machines) are in the pool, rescale to
# TARGET_PAR, WAIT for the rescale to land — it takes about 156 s, see
# lib_wait_rescale.py — and only then sample.
#
# Usage:
#   scripts/profile-job.sh                    # q8 at 12000 rec/s, 3 -> 2
#   RATE=20000 scripts/profile-job.sh
#   RATE=12000 PUBLISH=1 scripts/profile-job.sh   # also publish the vector
#
# PUBLISH=1 runs publish-loads.sh once the job is settled. Read the report first: a
# vector measured with backpressure anywhere is not worth publishing.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE=flink

RATE="${RATE:-12000}"
# Generous on purpose: submission + rescale + settle is already ~6 min, and the report
# and the publish sample for SAMPLES x INTERVAL each. A job that ends mid-sweep reports
# the vertices it got to and NaN for the rest — which is how the sink came back empty
# on 2026-09-20.
DURATION="${DURATION:-3600}"
QUERY="${QUERY:-q8}"
SUBMIT_PAR="${SUBMIT_PAR:-3}"
TARGET_PAR="${TARGET_PAR:-2}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
MAX_EVENT_AGE="${MAX_EVENT_AGE:-5000}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.ref.RefNexmarkJob}"
JAR="${JAR:-/tmp/nexmark.jar}"
SETTLE="${SETTLE:-120}"
SAMPLES="${SAMPLES:-9}"
INTERVAL="${INTERVAL:-5}"
PUBLISH="${PUBLISH:-0}"
KEEP="${KEEP:-0}"

JM_POD=$(kubectl get pods -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running -o jsonpath='{.items[-1:].metadata.name}')
[ -n "$JM_POD" ] || { echo "ERROR: no hay JobManager en ejecución" >&2; exit 1; }

jm_curl() { kubectl exec -n "$NAMESPACE" "$JM_POD" -- curl -s -m 20 "http://localhost:8081$1" 2>/dev/null; }

running_job() {
    jm_curl "/jobs/overview" | python3 -c "
import json, sys
jobs = [j for j in json.load(sys.stdin).get('jobs', []) if j.get('state') == 'RUNNING']
print(jobs[0]['jid'] if jobs else '')" 2>/dev/null
}

OLD=$(running_job)
if [ -n "$OLD" ]; then
    echo "! ya hay un job corriendo ($OLD); se cancela para no medir dos a la vez"
    kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel "$OLD" >/dev/null 2>&1
    sleep 15
fi

echo "=========================================="
echo "  Perfilado — $QUERY a $RATE rec/s, $SUBMIT_PAR -> $TARGET_PAR ($SLOT_SHARING)"
echo "=========================================="
kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink run -d -c "$JOB_CLASS" "$JAR" \
    "$RATE" "$DURATION" "$SUBMIT_PAR" 10 0 CONSTANT "$SUBMIT_PAR" "$MAX_EVENT_AGE" \
    "$QUERY" 0 0 "$SLOT_SHARING" >/dev/null 2>&1

for _ in $(seq 1 24); do
    JID=$(running_job); [ -n "$JID" ] && break; sleep 5
done
[ -n "${JID:-}" ] || { echo "ERROR: el job no llegó a RUNNING" >&2; exit 1; }
echo "  job $JID sometido a paralelismo $SUBMIT_PAR — los 12 slots entran al pool"
sleep 90

PAYLOAD=$(jm_curl "/jobs/$JID" | TARGET="$TARGET_PAR" python3 -c "
import json, os, sys
t = int(os.environ['TARGET'])
# EVERY vertex goes in the payload: a partial one is rejected outright.
reqs = {}
for v in json.load(sys.stdin).get('vertices', []):
    par = max(v.get('parallelism', 1), 1)
    w = t if par > 1 else par
    reqs[v['id']] = {'parallelism': {'lowerBound': w, 'upperBound': w}}
print(json.dumps(reqs))" 2>/dev/null)
[ -n "$PAYLOAD" ] || { echo "ERROR: no se pudo construir el payload" >&2; exit 1; }

CODE=""
for _try in 1 2 3; do
    CODE=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- curl -s -o /dev/null -w "%{http_code}" \
        -m 20 -X PUT "http://localhost:8081/jobs/$JID/resource-requirements" \
        -H "Content-Type: application/json" -d "$PAYLOAD" 2>/dev/null)
    case "$CODE" in 200|202) break ;; esac
    sleep 3
done
echo "  PUT paralelismo=$TARGET_PAR -> HTTP $CODE"
case "$CODE" in 200|202) ;; *) echo "ERROR: el reescalado no fue aceptado" >&2; exit 1 ;; esac

# The PUT is acknowledged instantly; the placement arrives about two and a half
# minutes later. Measuring before that profiles the SUBMISSION's placement.
python3 "$SCRIPT_DIR/lib_wait_rescale.py" --pod "$JM_POD" --job "$JID" \
    --target "$TARGET_PAR" --namespace "$NAMESPACE" --timeout "${RESCALE_TIMEOUT:-300}"

echo "  asentando ${SETTLE}s antes de muestrear..."
sleep "$SETTLE"

python3 "$SCRIPT_DIR/check-loads.py" --jm-pod "$JM_POD" --samples "$SAMPLES" --interval "$INTERVAL"

if [ "$PUBLISH" = "1" ]; then
    echo ""
    # Same span as the report above. A short span does not average out the contention
    # that busy time absorbs — on `tm-1-slow` two subtasks of the same operator, at the
    # same rate and with no backpressure, read 2.0 and 97.0 ms/s.
    LOAD_SAMPLES="$SAMPLES" LOAD_INTERVAL="$INTERVAL" "$SCRIPT_DIR/publish-loads.sh"
fi

if [ "$KEEP" = "1" ]; then
    echo ""
    echo "job $JID sigue corriendo (KEEP=1). Para cancelarlo:"
    echo "  kubectl exec -n $NAMESPACE $JM_POD -- flink cancel $JID"
else
    kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel "$JID" >/dev/null 2>&1
    echo ""
    echo "job cancelado"
fi
