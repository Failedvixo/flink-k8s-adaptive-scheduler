#!/bin/bash
# ============================================
# Publish how fast each TaskManager is, so the assigner can stop assuming they are equal
# ============================================
#
# WHY (offline bench, 2026-08-18). Enumerating the exact optimum shows that on
# IDENTICAL TaskManagers a load-aware greedy is already optimal — 0.0% gap up to
# 8 slices — because permuting equal machines gives the same answer. That single
# fact explains five in-cluster runs in which no arm could be told from another
# and STOCK matched them all: the cluster was posing a problem a greedy rule
# solves exactly. Heterogeneity is the lever that changes it, opening a 9-14% gap
# with only 4 slices, and it is also the premise of Li et al. — their cluster is
# heterogeneous, which is structural load for CETSA rather than incidental
# detail.
#
# The fork cannot see any of that on its own. Every cost it reasons about is
# implicitly in the same units on every machine, so a TaskManager with twice the
# CPU still looks like a machine that should carry an equal share. This publishes
# the missing vector: one "<taskManagerResourceId> <speed>" line each, where the
# speed is cores available to that TaskManager (limits.cpu=2 -> 2.0 against a
# 1.0 sibling). LPT divides by it directly, and the ACO/GA balance term aims at a
# share proportional to it instead of an equal one.
#
# The TaskManager resource id Flink reports is "<podIP>:<port>-<hash>", so the
# pod is identified by the IP prefix — no label bookkeeping and nothing to keep
# in sync when a pod is replaced.
#
# Written like the arm and the loads: temp file plus atomic rename, because the
# JobManager may read at any instant and a half-written file would change a
# placement. Unlisted TaskManagers default to 1.0, so a partial file degrades to
# "the ones I know about differ" rather than to nonsense.
#
# Usage:
#   scripts/publish-speeds.sh              # derive from the pods' CPU limits and publish
#   scripts/publish-speeds.sh --read       # what is published right now
#   scripts/publish-speeds.sh --clear      # remove it; every TaskManager is nominal again

set -eu

NODE="${THESIS_NODE:-minikube}"
THESIS_DIR=/var/thesis
SPEEDS_FILE="$THESIS_DIR/speeds"
TMP_FILE="$THESIS_DIR/.speeds.tmp"
NAMESPACE=flink

jm_curl() {
    kubectl exec -n "$NAMESPACE" deployment/flink-jobmanager -- \
        curl -s -m 15 "http://localhost:8081$1" 2>/dev/null
}

# WHAT COUNTS AS "CAPACITY" IS ITSELF A HYPOTHESIS (2026-09-14). The file this script
# writes is the denominator of the fork's LPT — `finish = (load + assigned) / speed` —
# so publishing cores declares that cores are what a slice competes for. Measured on
# this cluster the same day, that declaration is wrong for Q8: the join's RocksDB block
# cache hits only 37.8% of the time (170k hits against 280k misses) with ~670 MB read
# and ~800 MB written per subtask, which by Justin's 80% criterion (arXiv 2505.19739)
# makes it STATE-BOUND, not CPU-bound. A machine's useful capacity for that operator is
# its managed memory per slot, not its core count.
#
# --memory publishes exactly that, so the hypothesis can be tested WITHOUT the
# two-dimensional assigner the fork does not have yet: same LPT, same published loads,
# only the capacity vector changes. If placements driven by memory beat placements
# driven by cores on a state-bound query, the second dimension is worth building into
# the model; if they tie, it is not, and a hundred lines of fork change are saved.
#
# The two vectors are deliberately opposed on this cluster, because the managed pool is
# set per class precisely to decorrelate them (see setup-rocksdb-state.sh):
#
#     class    cores   speed --cpu   MB/slot   speed --memory
#     fast       4         4.0         48          1.5
#     medium     2         2.0         80          2.5
#     slow       1         1.0         32          1.0
#
# Normalised against the smallest machine in each dimension, so both vectors are ratios
# on the same footing and the arms stay comparable.
MODE="cpu"

case "${1:-}" in
    --memory)
        MODE="memory"
        shift || true
        ;;
    --read)
        minikube ssh -n "$NODE" -- "sudo cat $SPEEDS_FILE 2>/dev/null || echo '(nothing published)'" | tr -d '\r'
        exit 0
        ;;
    --clear)
        minikube ssh -n "$NODE" -- "sudo rm -f $SPEEDS_FILE" >/dev/null
        echo "cleared — every TaskManager is nominal (1.0) again"
        exit 0
        ;;
esac

TMS=$(jm_curl "/taskmanagers" | python3 -c "
import json, re, sys
rows = []
for t in json.load(sys.stdin).get('taskmanagers', []):
    tid = t.get('id', '')
    # 'IP:port-hash' -> IP. The hardware figure is the fallback: Flink reports the
    # cgroup-limited core count, but as a whole number, so a 500m limit is invisible there.
    # The pod IP comes from the RPC path, not from the id: since the TaskManagers carry an
    # explicit taskmanager.resource-id, the id no longer contains an address at all.
    match = re.search(r'@([^:/]+):', t.get('path', '') or '')
    ip = match.group(1) if match else tid.split(':')[0]
    rows.append((tid, ip, t.get('hardware', {}).get('cpuCores', 1)))
# Sorted by resource id, which is the order the assigner itself imposes on TaskManagers.
# That makes the published file a direct record of the greedy arms' tie-breaking order —
# and on a heterogeneous cluster that order is not a detail: measured in the offline bench,
# the same load-aware greedy sits at 1.5% from optimal when the fast machine sorts FIRST
# and 43% when it sorts LAST, because ties at zero load go to the lowest-ranked machine and
# the rule never recovers. LPT is flat at 1.3% either way, which is most of its case.
for row in sorted(rows):
    print(*row)" 2>/dev/null)

if [ -z "$TMS" ]; then
    echo "ERROR: the JobManager reports no TaskManagers" >&2; exit 1
fi

# podIP -> cores, straight off the container's CPU limit (the real ceiling), falling
# back to the request, which is what the scheduler actually reserved.
LIMITS=$(kubectl get pods -n "$NAMESPACE" -l component=taskmanager -o json | python3 -c "
import json, sys

def cores(quantity):
    if not quantity:
        return None
    return float(quantity[:-1]) / 1000.0 if quantity.endswith('m') else float(quantity)

for pod in json.load(sys.stdin).get('items', []):
    ip = pod.get('status', {}).get('podIP')
    if not ip:
        continue
    resources = pod['spec']['containers'][0].get('resources', {})
    value = cores(resources.get('limits', {}).get('cpu')) or cores(resources.get('requests', {}).get('cpu'))
    if value:
        print(ip, value)" 2>/dev/null || true)

# Managed memory per slot, per class, normalised against the smallest. Read from the
# deployments rather than from the TaskManagers' REST report so it reflects what was
# CONFIGURED — the two agree, but a mismatch here would mean a rollout had not landed,
# and silently publishing the old vector is the failure mode this script already has.
MEM_SPEEDS=""
if [ "$MODE" = memory ]; then
    MEM_SPEEDS=$(for d in flink-tm-fast flink-tm-medium flink-tm-slow; do
        props=$(kubectl get deploy "$d" -n "$NAMESPACE" \
            -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}' 2>/dev/null)
        managed=$(printf '%s\n' "$props" | grep -E '^\s*taskmanager\.memory\.managed\.size:' | awk '{print $2}')
        slots=$(printf '%s\n' "$props" | grep -E '^\s*taskmanager\.numberOfTaskSlots:' | awk '{print $2}')
        echo "${d#flink-tm-} ${managed:-0} ${slots:-0}"
    done | python3 -c "
import sys
rows = []
for line in sys.stdin:
    name, managed, slots = line.split()
    mb = float(managed.rstrip('mMgG')) * (1024 if managed.lower().endswith('g') else 1)
    slots = int(slots)
    if slots:
        rows.append((name, mb / slots))
if not rows:
    sys.exit(0)
floor = min(v for _, v in rows)
for name, v in rows:
    print(name, round(v / floor, 3))")
    if [ -z "$MEM_SPEEDS" ]; then
        echo "ERROR: --memory could not read managed memory or slot counts" >&2; exit 1
    fi
fi

CONTENT=""
UNLIMITED=0
while read -r TID IP HW; do
    [ -n "$TID" ] || continue
    if [ "$MODE" = memory ]; then
        # The resource id carries the class ("tm-3-fast"), which is what the manifests
        # pin, so the class is read off the id instead of resolving pod -> deployment.
        CLASS="${TID##*-}"
        SPEED=$(echo "$MEM_SPEEDS" | awk -v c="$CLASS" '$1 == c {print $2}')
        [ -n "$SPEED" ] || SPEED=1.0
        echo "  $TID  ${SPEED}  (memoria por slot)"
        CONTENT="${CONTENT}${TID} ${SPEED}
"
        continue
    fi
    SPEED=$(echo "$LIMITS" | awk -v ip="$IP" '$1 == ip {print $2; found=1} END {if (!found) print ""}')
    if [ -z "$SPEED" ]; then
        # No limit and no request: the container may use the whole node, so its speed is
        # not a property of the pod at all. Report it rather than inventing a number —
        # an unlimited TaskManager on a 12-core node is not 1.0, and pretending otherwise
        # would publish a vector that quietly contradicts the cluster.
        SPEED="$HW"
        UNLIMITED=$((UNLIMITED + 1))
    fi
    echo "  $TID  ${SPEED} cores"
    CONTENT="${CONTENT}${TID} ${SPEED}
"
done <<< "$TMS"

if [ "$UNLIMITED" -gt 0 ]; then
    echo ""
    echo "WARNING: $UNLIMITED TaskManager(s) have no CPU limit or request; used the core count"
    echo "         Flink reports, which is the node's. Placement only changed throughput in the"
    echo "         2026-08-17 runs when limits.cpu was set — without it nothing competes for CPU"
    echo "         and the speeds published here describe a difference the cluster will not show."
fi

minikube ssh -n "$NODE" -- "sudo mkdir -p $THESIS_DIR && \
    printf '%s' '$CONTENT' | sudo tee $TMP_FILE >/dev/null && \
    sudo mv -f $TMP_FILE $SPEEDS_FILE && sudo chmod 644 $SPEEDS_FILE" >/dev/null

echo ""
echo "published -> $SPEEDS_FILE  (takes effect on the next rescale)"
