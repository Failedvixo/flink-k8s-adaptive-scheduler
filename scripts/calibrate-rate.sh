#!/bin/bash
# ============================================
# Find the input rate this cluster can actually sustain, instead of assuming one
# ============================================
#
# WHY (2026-09-02). Every campaign so far ran at 60000 rec/s, a number inherited
# from CETSA's experimental setup — a 96-core, 12-VM cloud cluster. This one is a
# laptop. The consequence was measured on 2026-08-31: throughput correlates
# POSITIVELY with backpressure (r = +0.694), which is the signature of a source
# that cannot keep up rather than a job that is congested. The generator competes
# for CPU with the operators it is meant to feed, so the low-throughput episodes
# were STARVED, not saturated, and the analysis has had to filter them out ever
# since (--min-backpressure 280).
#
# CAPSys does not guess the rate either (§3.1): "we configure the target input rate
# to match the capacity of the resource cluster by gradually increasing the input
# rate until it saturates all workers". This is that procedure, automated.
#
# WHAT "SUSTAINED" MEANS HERE, and it is the whole measurement. The job is asked
# for a rate; the source reports what it actually emitted. While the cluster keeps
# up, those two track each other. The first rate at which the source falls short of
# what it was asked for is the ceiling — and everything above it measures the
# generator's limits, not the placement's.
#
# The answer is a property of (cluster, query, parallelism, state backend). It has
# to be recomputed after any of them changes — in particular after switching to
# RocksDB, which is why scripts/setup-rocksdb-state.sh should run first.
#
# Requires: the cluster up, the fork deployed, and the benchmark jar BUILT locally
# (cd flink-nexmark-job && mvn package). The jar is uploaded here rather than
# assumed: /tmp/nexmark.jar lives inside the JobManager POD, so every rollout —
# and scripts/deploy-thesis-fork.sh is a rollout — takes it with it. Assuming it
# was still there cost a 15-minute run on 2026-09-02 in which every rate failed to
# submit and the report read "no rate was sustained".
#
# Usage:
#   scripts/calibrate-rate.sh
#   QUERY=q8 SUBMIT_PAR=3 scripts/calibrate-rate.sh
#   RATES="40000 50000 60000 70000" scripts/calibrate-rate.sh    # refine a boundary

set -eu

NAMESPACE=flink
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

QUERY="${QUERY:-q8}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.NexmarkRealJob}"
SUBMIT_PAR="${SUBMIT_PAR:-3}"
JOB_WINDOW="${JOB_WINDOW:-10}"
DIST="${DIST:-CONSTANT}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
CPU_LOAD="${CPU_LOAD:-0}"
MAX_EVENT_AGE="${MAX_EVENT_AGE:-0}"
# A doubling-ish ramp by default: it costs one step per factor and the refinement
# pass around the boundary is a second, cheap invocation with an explicit list.
RATES="${RATES:-10000 20000 40000 80000 160000}"
WARMUP="${WARMUP:-90}"
WINDOW="${WINDOW:-60}"
SAMPLES="${SAMPLES:-6}"
# How far below the requested rate still counts as keeping up. Below this the
# source is the bottleneck and the episode says nothing about placement.
TOLERANCE="${TOLERANCE:-0.95}"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT_DIR="$ROOT_DIR/results/rate-calibration/$STAMP"
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/calibrate.log"

# RETRIED, like every other call into this cluster. A single `kubectl get nodes`
# was the very first thing these scripts did, and a transient TLS handshake
# timeout — seen repeatedly on 2026-09-07 with the cluster demonstrably alive and
# the API server at two restarts in fifteen hours — aborted the run before it
# started. Three smoke tests were lost to it and read as failures of the
# experiment's design.
_reachable=0
for _try in 1 2 3 4; do
    if kubectl get nodes >/dev/null 2>&1; then _reachable=1; break; fi
    sleep 3
done
if [ "$_reachable" != 1 ]; then
    echo "ERROR: cluster unreachable after 4 attempts (minikube start)" >&2
    exit 1
fi
JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)
[ -n "$JM_POD" ] || { echo "ERROR: no JobManager pod" >&2; exit 1; }

LOCAL_JAR="$ROOT_DIR/flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar"
[ -f "$LOCAL_JAR" ] || {
    echo "ERROR: build the benchmark first (cd flink-nexmark-job && mvn package)" >&2
    exit 1
}
echo "Uploading benchmark jar to $JM_POD..."
kubectl cp "$LOCAL_JAR" "$NAMESPACE/$JM_POD:/tmp/nexmark.jar"

# A submission that fails for a reason unrelated to the rate — a missing jar, a
# wrong class, a cluster short of slots — must not be reported as a rate the
# cluster could not sustain. The first failure aborts instead.
SUBMIT_FAILURES=0

# Read the REST API from inside the pod, the way run-placement-experiment.sh does:
# a port-forward dies silently under load and leaves curl talking to a dead tunnel.
jm() { kubectl exec -n "$NAMESPACE" "$JM_POD" -- curl -s -m 15 "http://localhost:8081$1" 2>/dev/null; }

cancel_all() {
    for jid in $(jm /jobs | python3 -c "
import json,sys
try:
    print(' '.join(j['id'] for j in json.load(sys.stdin).get('jobs',[])
                   if j.get('status') in ('RUNNING','CREATED','RESTARTING')))
except Exception: pass" 2>/dev/null); do
        kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
            curl -s -m 15 -X PATCH "http://localhost:8081/jobs/$jid?mode=cancel" >/dev/null 2>&1 || true
    done
}

# A SWEEP LONGER THAN TWO STEPS LIES WITHOUT THIS (2026-09-07). Something accumulates in
# the TaskManager JVMs across jobs, so the THIRD measurement of a sweep comes out degraded
# whatever rate it asks for: three identical runs at 32000 rec/s gave 0.982 / 0.979 / 0.873,
# and every ascending sweep that evening had the same shape (1.000/0.997/0.842,
# 1.000/0.956/0.705, 0.996/1.012/0.717). Because the sweep goes low-to-high, the artefact
# lands on the high rates and manufactures a ceiling that is not there — it produced a
# "knee" between 28000 and 29000 that vanished once the drift was controlled. Restarting
# between rates makes each step a measurement of the cluster rather than of its position in
# the list. The published speed vector is unaffected: it is keyed by taskmanager.resource-id,
# which the manifests pin, not by pod IP.
TM_DEPLOYMENTS="${TM_DEPLOYMENTS:-flink-tm-fast flink-tm-medium flink-tm-slow}"
RESTART_TMS="${RESTART_TMS:-1}"

restart_taskmanagers() {
    [ "$RESTART_TMS" = "1" ] || return 0
    local expected registered=0 waited=0 name
    expected=$(echo "$TM_DEPLOYMENTS" | wc -w | tr -d ' ')
    for name in $TM_DEPLOYMENTS; do
        kubectl rollout restart -n "$NAMESPACE" "deploy/${name%%:*}" >/dev/null 2>&1 || true
    done
    for name in $TM_DEPLOYMENTS; do
        kubectl rollout status -n "$NAMESPACE" "deploy/${name%%:*}" --timeout=180s >/dev/null 2>&1 || true
    done
    # The rollout finishing is not the same as the JobManager seeing them again: a
    # TaskManager bound to the old pod needs a heartbeat timeout to re-register.
    while [ "$waited" -lt 210 ]; do
        registered=$(jm /overview | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("taskmanagers", 0))' 2>/dev/null || echo 0)
        [ "$registered" = "$expected" ] && break
        sleep 10
        waited=$((waited + 10))
    done
    [ "$registered" = "$expected" ] || \
        echo "  ! sólo $registered/$expected TaskManagers registrados en este escalón" >&2
}

echo "==========================================" | tee "$LOG"
echo "  Rate calibration — $QUERY at parallelism $SUBMIT_PAR" | tee -a "$LOG"
echo "  slot sharing $SLOT_SHARING, warmup ${WARMUP}s, window ${WINDOW}s" | tee -a "$LOG"
echo "  sustained = source emits >= $(python3 -c "print(int(float('$TOLERANCE')*100))")% of the requested rate" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"
printf '%10s %12s %12s %10s %10s  %s\n' \
    "pedida" "emitida" "cociente" "bp ms/s" "busy ms/s" "veredicto" | tee -a "$LOG"

BEST=""
RESULTS="[]"

for RATE in $RATES; do
    cancel_all
    sleep 5
    restart_taskmanagers

    # THE SWEEP IS ASCENDING, SO DIRT AND RATE MOVE TOGETHER (2026-09-07). MinIO keeps a
    # cancelled job's checkpoints, and the accumulation costs real capacity: with 4.3 GB
    # on the hostPath, 20000 rec/s fell to a 0.704 ratio; after wiping, the same rate
    # returned 1.000 and 28000 went from 0.956 to 0.983. Without this reset every step of
    # the sweep runs dirtier than the one before it, which biases the answer DOWNWARD in
    # exactly the direction the sweep is searching — three sweeps this evening reported
    # ceilings of 40000, 32000 and 29000 while the cluster was unchanged, each one lower
    # because the disk had more on it. Each rate now starts from the same disk, so the
    # ceiling is a property of the cluster and not of the order the rates were tried in.
    # What grows is `.minio.sys/multipart` — the parts of uploads a cancelled job never
    # finished: 3.9 GB after sixteen jobs, against 4 KB in the data directories. The
    # cancel_all above must have taken effect before these are removed, since deleting a
    # multipart directory under a live upload is what raises NoSuchUpload and fails a
    # checkpoint; `flink cancel` returns on acceptance, hence the wait.
    if [ "${CLEAN_CHECKPOINTS:-1}" = "1" ]; then
        minikube ssh -n "${THESIS_NODE:-minikube}" -- \
            "sudo rm -rf /var/thesis/minio/flink-checkpoints/checkpoints/* \
                         /var/thesis/minio/.minio.sys/multipart/* \
                         /var/thesis/minio/.minio.sys/tmp/* 2>/dev/null; \
             sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null" >/dev/null 2>&1 \
            || echo "  ! no se pudieron limpiar los checkpoints antes de $RATE" >&2
    fi

    # Duration covers the whole measurement plus slack; the job is cancelled after.
    DURATION=$(( WARMUP + WINDOW + 120 ))
    SUBMIT_OUT=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
        flink run -d -c "$JOB_CLASS" /tmp/nexmark.jar \
        "$RATE" "$DURATION" "$SUBMIT_PAR" "$JOB_WINDOW" "$CPU_LOAD" "$DIST" \
        "$SUBMIT_PAR" "$MAX_EVENT_AGE" "$QUERY" "${ZIPF_ALPHA:-0.5}" "${HOT_POOL:-1000}" \
        "$SLOT_SHARING" 2>&1)
    JID=$(echo "$SUBMIT_OUT" | grep -oP '(?<=JobID )[0-9a-f]{32}' | head -1)
    [ -n "$JID" ] || JID=$(echo "$SUBMIT_OUT" | grep -oP '[0-9a-f]{32}' | head -1)
    if [ -z "$JID" ]; then
        echo "  ! could not submit at $RATE — this is NOT a rate ceiling" | tee -a "$LOG"
        echo "$SUBMIT_OUT" | tail -20 | tee -a "$LOG"
        SUBMIT_FAILURES=$((SUBMIT_FAILURES + 1))
        break
    fi

    # Wait for RUNNING before the warm-up clock starts, or the warm-up is spent
    # waiting for slots and the measurement lands in the transient.
    for _ in $(seq 60); do
        STATE=$(jm "/jobs/$JID" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('state',''))
except Exception: print('')" 2>/dev/null)
        [ "$STATE" = "RUNNING" ] && break
        sleep 2
    done
    sleep "$WARMUP"

    # ALL source vertices, summed. The reference Nexmark implementation has one
    # source per event type — Q8 has two — and measuring only the first made the
    # calibration report a ratio of exactly 0.25 for a job that was emitting
    # precisely what it had been asked for. See scripts/lib_source_metrics.py.
    read -r OUT_RPS BP BUSY NSRC <<< "$(python3 "$SCRIPT_DIR/lib_source_metrics.py" \
        --pod "$JM_POD" --job "$JID" --samples "$SAMPLES" \
        --interval "$(python3 -c "print(max(1, $WINDOW / $SAMPLES))")")"
    if [ "${NSRC:-0}" -eq 0 ]; then
        echo "  ! could not read any source vertex at $RATE" | tee -a "$LOG"
        SUBMIT_FAILURES=$((SUBMIT_FAILURES + 1))
        break
    fi

    RATIO=$(python3 -c "print(f'{$OUT_RPS / max($RATE, 1):.3f}')")
    SUSTAINED=$(python3 -c "print('si' if $RATIO >= $TOLERANCE else 'NO')")
    printf '%10s %12.0f %12s %10.0f %10.0f  %s\n' \
        "$RATE" "$OUT_RPS" "$RATIO" "$BP" "$BUSY" "$SUSTAINED" | tee -a "$LOG"

    RESULTS=$(RESULTS="$RESULTS" RATE="$RATE" OUT_RPS="$OUT_RPS" RATIO="$RATIO" \
        BP="$BP" BUSY="$BUSY" SUSTAINED="$SUSTAINED" python3 -c "
import json, os
r = json.loads(os.environ['RESULTS'])
r.append({'rate': int(os.environ['RATE']), 'source_out_rps': float(os.environ['OUT_RPS']),
          'ratio': float(os.environ['RATIO']), 'backpressure_ms_s': float(os.environ['BP']),
          'busy_ms_s': float(os.environ['BUSY']),
          'sustained': os.environ['SUSTAINED'] == 'si'})
print(json.dumps(r))")

    if [ "$SUSTAINED" = "si" ]; then
        BEST="$RATE"
    else
        # Stop at the first ceiling: everything above it is more of the same
        # starvation, and each step costs WARMUP + WINDOW of cluster time.
        echo "" | tee -a "$LOG"
        echo "  techo alcanzado en $RATE" | tee -a "$LOG"
        break
    fi
done

cancel_all

echo "" | tee -a "$LOG"
if [ "$SUBMIT_FAILURES" -gt 0 ]; then
    echo "ABORTADO: el job no se pudo enviar. No es un techo de tasa — revisa el" | tee -a "$LOG"
    echo "  error de arriba antes de sacar conclusiones de esta corrida." | tee -a "$LOG"
elif [ -n "$BEST" ]; then
    echo "TASA SOSTENIDA: $BEST rec/s" | tee -a "$LOG"
    echo "  Úsala como RATE en la campaña. Si el salto al siguiente escalón fue" | tee -a "$LOG"
    echo "  grande, refina:  RATES=\"...\" scripts/calibrate-rate.sh" | tee -a "$LOG"
else
    echo "NINGUNA tasa se sostuvo, ni la más baja." | tee -a "$LOG"
    echo "  El generador no alcanza a alimentar el job ni en el escalón inicial;" | tee -a "$LOG"
    echo "  baja RATES o revisa que el clúster tenga los slots que el job pide." | tee -a "$LOG"
fi

RESULTS="$RESULTS" QUERY="$QUERY" SUBMIT_PAR="$SUBMIT_PAR" BEST="${BEST:-0}" \
SLOT_SHARING="$SLOT_SHARING" WARMUP="$WARMUP" WINDOW="$WINDOW" TOLERANCE="$TOLERANCE" \
python3 -c "
import json, os
json.dump({'query': os.environ['QUERY'], 'submit_par': int(os.environ['SUBMIT_PAR']),
           'slot_sharing': os.environ['SLOT_SHARING'], 'warmup_s': int(os.environ['WARMUP']),
           'window_s': int(os.environ['WINDOW']), 'tolerance': float(os.environ['TOLERANCE']),
           'sustained_rate': int(os.environ['BEST']),
           'steps': json.loads(os.environ['RESULTS'])},
          open('$OUT_DIR/calibration.json', 'w'), indent=2)"
echo "" | tee -a "$LOG"
echo "resultados -> $OUT_DIR/" | tee -a "$LOG"
