#!/bin/bash
# ============================================
# Switch the cluster to the RocksDB state backend, with the memory to run it
# ============================================
#
# WHY (2026-09-02, from CAPSys, EuroSys'25; figures CORRECTED 2026-09-28 against the PDF).
# The paper's Figure 3(b) is titled "Q2-join: contention on state access" and shows that
# co-locating the tasks of a tumbling-window join costs throughput and raises backpressure.
# Its Q2-join is "two sources, two map operators, and a tumbling window join that can
# accumulate large state" — the paper's own query, NOT Nexmark Q8, which it never mentions;
# the resemblance to our Q8 is ours to argue, not theirs. The earlier version of this comment
# quoted "110k rec/s and 4% backpressure, dropping to 91k and 32%": those numbers were
# fabricated and fall outside the figure's own axes (75k-100k rec/s, 0-25%). Cite the
# phenomenon and the figure, never those values. This cluster runs the default
# HashMapStateBackend, so state lives on the JVM heap, there is no state-access
# dimension to contend on, and that experiment cannot be reproduced here at all.
# CAPS's own cost model measures the state dimension as "uncompressed bytes read
# from and written to RocksDB"; with hashmap that quantity does not exist.
#
# THE MEMORY IS THE WHOLE POINT, NOT A DETAIL. RocksDB is paid for out of Flink's
# MANAGED memory, and this cluster currently sets it to 16m — deliberately, because
# with HashMapStateBackend managed memory is reserved and idle, so freeing it went
# to the task heap (see scripts/patch-flink-property.sh). Switching the backend
# without giving the managed pool back is not a half-measure: RocksDB gets 16 MB of
# block cache and write buffers, thrashes, and the run measures the thrashing.
#
# AND MANAGED MEMORY IS SPLIT PER SLOT, which on this cluster is not uniform:
#
#     TaskManager   cores   slots   managed/slot at 256m
#     fast            4       2         128m
#     medium          2       4          64m
#     slow            1       6          43m
#
# So the same operator gets a three-times-larger RocksDB cache on `fast` than on
# `slow`, for free, from the slot counts this cluster already has.
#
# THAT FREE VERSION IS NOT USABLE, though, and it took until 2026-09-14 to see why:
# it makes memory-per-slot track the core count exactly, so both dimensions point at
# the same machine and the placement problem stays one-dimensional. The managed pool
# is therefore set PER CLASS below to break that tie deliberately. Read the MANAGED_*
# block for the reasoning and the arithmetic.
#
# The patches are strategic-merge on the live deployments and do NOT edit the
# versioned manifests, so `kubectl apply -f kubernetes/flink-taskmanager-classes.yaml`
# restores the previous state, as does --revert.
#
# REQUIRES shared checkpoint storage (scripts/setup-shared-checkpoints.sh). RocksDB
# incremental checkpoints on local disk break on the first rescale that moves a
# subtask, which is every rescale this thesis measures.
#
# Usage:
#   PROCESS=1280m scripts/setup-rocksdb-state.sh   # per-class managed, metrics on
#   MANAGED=192m  scripts/setup-rocksdb-state.sh   # same pool on all three (old way)
#   ROCKSDB_METRICS=0 scripts/setup-rocksdb-state.sh
#   scripts/setup-rocksdb-state.sh --check
#   scripts/setup-rocksdb-state.sh --revert        # back to hashmap + 16m, metrics off

set -eu

NAMESPACE=flink
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# MANAGED MEMORY IS NOW PER CLASS, AND DELIBERATELY NOT PROPORTIONAL TO CORES
# (2026-09-14). Giving every class the same managed pool made memory-per-slot track
# the core count exactly — fast had both the most cores AND the most memory per slot,
# slow the least of both. That correlation is fatal to the RL objective: a
# state-bound operator and a CPU-bound one then PREFER THE SAME MACHINE, so
# "characterise what limits this operator" has no consequence, the load collapses back
# to a scalar, and LPT is already exact on a scalar. There is nothing for an agent to
# learn that a greedy does not already do.
#
# These values break the tie on purpose: `medium` becomes the memory machine and
# `fast` stays the CPU machine, so a slice that is limited by state access has a real
# reason to prefer two cores with 80 MB/slot over four cores with 48.
#
#     class    cores  slots  managed   per slot   taskHeap
#     fast       4      2      96m       48 MB     397 MB
#     medium     2      4     320m       80 MB     173 MB
#     slow       1      6     192m       32 MB     301 MB
#
# The arithmetic behind taskHeap, so the next change can be checked without a cluster:
# with PROCESS=1280m, overhead = max(10%, 192m) = 192, metaspace = 256, leaving
# totalFlink = 832; framework heap+offheap = 256 and network = max(10% of totalFlink,
# 64m) = 83, so taskHeap = 493 - managed. All three stay above the 128 MB floor that
# report_memory enforces, and the node total is unchanged at 3 x 1280m.
#
# Set MANAGED to override all three at once (the old uniform behaviour); leave it
# empty to use the per-class values.
MANAGED="${MANAGED:-}"
MANAGED_FAST="${MANAGED_FAST:-96m}"
MANAGED_MEDIUM="${MANAGED_MEDIUM:-320m}"
MANAGED_SLOW="${MANAGED_SLOW:-192m}"

managed_for() {
    case "$1" in
        *fast)   echo "${MANAGED:-$MANAGED_FAST}" ;;
        *medium) echo "${MANAGED:-$MANAGED_MEDIUM}" ;;
        *slow)   echo "${MANAGED:-$MANAGED_SLOW}" ;;
        *)       echo "${MANAGED:-192m}" ;;
    esac
}

# PROCESS SIZE IS PER CLASS TOO (2026-09-14), because raising one machine's managed
# pool has to come with room for it: taskHeap is whatever is LEFT, so giving `fast`
# 384m of managed at PROCESS=1280m leaves 109 MB of task heap and trips the 128 MB
# floor. Raising every class instead would cost 3x the memory on a node that has 7 GB
# total and already spends 6.4 — so the machine under test gets the headroom and the
# others stay where they are. PROCESS still overrides all three when set.
PROCESS_FAST="${PROCESS_FAST:-}"
PROCESS_MEDIUM="${PROCESS_MEDIUM:-}"
PROCESS_SLOW="${PROCESS_SLOW:-}"

process_for() {
    case "$1" in
        *fast)   echo "${PROCESS:-$PROCESS_FAST}" ;;
        *medium) echo "${PROCESS:-$PROCESS_MEDIUM}" ;;
        *slow)   echo "${PROCESS:-$PROCESS_SLOW}" ;;
        *)       echo "${PROCESS:-}" ;;
    esac
}

# ROCKSDB NATIVE METRICS — the second resource dimension has to be OBSERVABLE, not
# just present (2026-09-14). The agent's per-slice state needs a state-access signal,
# and `busyTimeMsPerSecond` cannot supply it: Justin (arXiv 2505.19739) shows a task
# with slow state access reports HIGH busyness while barely using the CPU, so busy
# conflates the two dimensions we are trying to separate.
#
# The six enabled here are the cheap ticker counters, chosen to match what the related
# work actually measures:
#   bytes-read / bytes-written  -> CAPSys defines its state-access cost as exactly
#                                  "uncompressed bytes read from and written to the
#                                  RocksDB state backend"
#   iter-bytes-read             -> window state is scanned through iterators, so for
#                                  tumbling and sliding windows most reads land here
#                                  and NOT in bytes-read
#   block-cache-hit / -miss     -> Justin's cache hit rate, his threshold is 80%
#   stall-micros                -> time RocksDB stalled writes; direct evidence that
#                                  the slice is limited by state and not by CPU
#
# They are COUNTERS, monotonic since the task started, so any consumer has to
# difference two samples to get a rate. Histogram-based native metrics are not enabled:
# those do carry a measurable overhead, and the ratios above are enough.
ROCKSDB_METRICS="${ROCKSDB_METRICS:-1}"
ROCKSDB_METRIC_KEYS="bytes-read bytes-written iter-bytes-read block-cache-hit block-cache-miss stall-micros"
# Optional, and probably needed. Flink splits the process size into framework heap,
# task heap, managed, network, metaspace and overhead; the current 1024m fits only
# because managed is 16m. Taking 256m for RocksDB comes out of the task heap, and if
# what is left is negative the TaskManager refuses to start with an explicit
# "TaskManager memory configuration failed" — loudly, which is the good case. Raise
# this if that happens, and watch the laptop's total: three TaskManagers at 1536m
# plus the JobManager is already 5.6 GB.
PROCESS="${PROCESS:-}"
PATCH="$SCRIPT_DIR/patch-flink-property.sh"

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

# Every TaskManager class is its own deployment, so a property has to be set on all
# of them; patch-flink-property.sh addresses `taskmanager`, which is the drained
# generic one, so the classes are handled here by name.
TM_DEPLOYMENTS=$(kubectl get deploy -n "$NAMESPACE" -o name 2>/dev/null |
    grep -E 'flink-tm-(fast|medium|slow)$' | sed 's|deployment.apps/||' || true)

# THE CHECK THAT MATTERS, and the one this script did not have on 2026-09-02.
# Flink derives the task heap as WHATEVER IS LEFT after framework, network, managed
# and the JVM's own overheads. Ask for more managed memory than there is room for
# and it does not fail: it hands the user code a heap of ZERO and starts anyway.
# The TaskManager registers, the slots look right, and the first job dies of an
# OutOfMemoryError that points nowhere near the setting that caused it. Measured
# on this cluster: process 1024m, metaspace 256, overhead 192, framework 256,
# network 64, managed 256 -> taskHeap 0.
report_memory() {
    local jm
    jm=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
        --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)
    [ -n "$jm" ] || { echo "  (no JobManager pod; skipping the memory check)"; return 0; }

    # TaskManagers register a few seconds after a rollout, and they do NOT arrive
    # together: measured 2026-09-02, tm-3-fast needed a dozen scheduling retries
    # while the other two were already up. Waiting for "at least one" is therefore
    # not enough — it reports a healthy two-machine cluster and hides that the FAST
    # machine, the entire point of the heterogeneity, never joined. Wait for the
    # count the deployments promise.
    local expected
    expected=$(printf '%s\n' $TM_DEPLOYMENTS | grep -c . || echo 0)
    local json=""
    local found=0
    for _ in $(seq 30); do
        json=$(kubectl exec -n "$NAMESPACE" "$jm" -- \
            curl -s -m 10 http://localhost:8081/taskmanagers 2>/dev/null)
        found=$(printf '%s' "$json" | python3 -c "
import json,sys
try: print(len(json.load(sys.stdin).get('taskmanagers', [])))
except Exception: print(0)" 2>/dev/null || echo 0)
        [ "$found" -ge "$expected" ] && break
        sleep 3
    done
    if [ "$found" -lt "$expected" ]; then
        echo "  ! only $found of $expected TaskManagers registered." >&2
        printf '%s' "$json" | python3 -c "
import json,sys
try:
    for tm in json.load(sys.stdin).get('taskmanagers', []): print('    up:', tm['id'])
except Exception: pass" >&2
        echo "    A missing machine silently changes the cluster the campaign measures." >&2
        echo "    kubectl get pods -n flink   # look for a tm-* not 1/1 Running" >&2
        return 3
    fi

    printf '%s' "$json" | python3 -c "
import json, sys
tms = json.load(sys.stdin)['taskmanagers']
bad = []
for tm in sorted(tms, key=lambda t: t['id']):
    m = tm.get('memoryConfiguration', {})
    mb = lambda k: m.get(k, 0) / 1024 / 1024
    slots = tm['slotsNumber']
    print(f\"  {tm['id'][:14]:16} slots={slots:2}  managed={mb('managedMemory'):4.0f} MB\"
          f\"  ({mb('managedMemory')/max(slots,1):3.0f} MB/slot)  taskHeap={mb('taskHeap'):4.0f} MB\")
    if mb('taskHeap') < 128:
        bad.append((tm['id'], mb('taskHeap')))
if bad:
    print()
    print('  !! TASK HEAP TOO SMALL — user code has almost nothing to run in.')
    for tid, heap in bad:
        print(f'     {tid}: {heap:.0f} MB')
    print('     Flink does NOT fail on this; the first job dies of OutOfMemoryError.')
    print('     Fix: PROCESS=1280m MANAGED=192m scripts/setup-rocksdb-state.sh')
    sys.exit(3)
" || return 3
}

show() {
    MEMORY_BAD=0
    echo "state backend and managed memory now:"
    for d in flink-jobmanager $TM_DEPLOYMENTS; do
        printf '  %-18s ' "$d"
        props=$(kubectl get deploy "$d" -n "$NAMESPACE" \
            -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}' 2>/dev/null)
        backend=$(printf '%s\n' "$props" | grep -E '^\s*state\.backend\.type:' | awk '{print $2}')
        managed=$(printf '%s\n' "$props" | grep -E '^\s*taskmanager\.memory\.managed\.size:' | awk '{print $2}')
        slots=$(printf '%s\n' "$props" | grep -E '^\s*taskmanager\.numberOfTaskSlots:' | awk '{print $2}')
        echo "backend=${backend:-hashmap (default)}  managed=${managed:-unset}  slots=${slots:-n/a}"
    done
    echo ""
    echo "memory as the TaskManagers actually derived it:"
    report_memory || MEMORY_BAD=1
    echo ""
    echo "checkpoint storage (RocksDB needs it shared):"
    "$SCRIPT_DIR/setup-shared-checkpoints.sh" --check 2>/dev/null | sed 's/^/  /' ||
        echo "  (could not read)"
    return "$MEMORY_BAD"
}

case "${1:-}" in
    --check) show; exit 0 ;;
    --revert)
        BACKEND=hashmap
        MANAGED=16m
        echo "Reverting to HashMapStateBackend with managed memory $MANAGED"
        ;;
    "") BACKEND=rocksdb ;;
    *) echo "usage: setup-rocksdb-state.sh [--check|--revert]" >&2; exit 2 ;;
esac

if [ -z "$TM_DEPLOYMENTS" ]; then
    echo "ERROR: no flink-tm-{fast,medium,slow} deployments found." >&2
    echo "       Apply kubernetes/flink-taskmanager-classes.yaml first." >&2
    exit 1
fi

# The JobManager needs the backend too: it is what restores state on a rescale, and
# a JobManager that disagrees with the TaskManagers about the backend fails the
# restore rather than falling back.
echo "[1/3] JobManager -> state.backend.type=$BACKEND"
"$PATCH" jobmanager state.backend.type "$BACKEND" >/dev/null

echo "[2/3] TaskManagers -> state.backend.type=$BACKEND, managed per class"
for d in $TM_DEPLOYMENTS; do
    d_managed=$(managed_for "$d")
    current=$(kubectl get deploy "$d" -n "$NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}')
    # The metrics keys go too: --revert has to be able to take them back off, and
    # re-applying must not leave two copies of the same property in the block.
    updated=$(printf '%s\n' "$current" |
        grep -v -E '^\s*(state\.backend\.type|taskmanager\.memory\.managed\.size|state\.backend\.rocksdb\.metrics\.[a-z-]+):' )
    updated="$updated
    state.backend.type: $BACKEND
    taskmanager.memory.managed.size: $d_managed"
    if [ "$BACKEND" = rocksdb ] && [ "$ROCKSDB_METRICS" = 1 ]; then
        for k in $ROCKSDB_METRIC_KEYS; do
            updated="$updated
    state.backend.rocksdb.metrics.$k: true"
        done
    fi
    d_process=$(process_for "$d")
    if [ -n "$d_process" ]; then
        updated=$(printf '%s\n' "$updated" |
            grep -v -E '^\s*taskmanager\.memory\.process\.size:')
        updated="$updated
    taskmanager.memory.process.size: $d_process"
    fi
    # Exported, not interpolated: the property block is multi-line and json.dumps is
    # the only thing here that quotes it correctly for a strategic merge.
    export UPDATED="$updated"
    kubectl patch deploy "$d" -n "$NAMESPACE" --type=strategic -p "$(python3 -c '
import json, os
print(json.dumps({"spec": {"template": {"spec": {"containers": [
    {"name": "taskmanager", "env": [{"name": "FLINK_PROPERTIES",
     "value": os.environ["UPDATED"]}]}]}}}}))')" >/dev/null
    echo "  patched $d"
done

echo "[3/3] Waiting for rollouts..."
for d in flink-jobmanager $TM_DEPLOYMENTS; do
    kubectl rollout status "deployment/$d" -n "$NAMESPACE" --timeout=300s
done

echo ""
if show; then
    MEMORY_OK=yes
else
    MEMORY_OK=no
fi
echo ""
if [ "$MEMORY_OK" = no ]; then
    echo "STOP: fix the memory split before running anything else." >&2
    exit 3
fi
echo "NEXT, in this order:"
echo "  1. the fork's JobManager patch does not survive a rollout — redeploy it:"
echo "       scripts/deploy-thesis-fork.sh <ARM>"
echo "  2. the sustained input rate is a property of the state backend, so the old"
echo "     one no longer applies:"
echo "       QUERY=q8 scripts/calibrate-rate.sh"
echo ""
echo "IF A TaskManager CRASHLOOPS with a memory configuration error, the managed"
echo "pool did not fit in the process size. Give it room:"
echo "       PROCESS=1536m scripts/setup-rocksdb-state.sh"
echo "or ask RocksDB for less:"
echo "       MANAGED=128m scripts/setup-rocksdb-state.sh"
