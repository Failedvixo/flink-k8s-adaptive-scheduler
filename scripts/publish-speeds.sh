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

case "${1:-}" in
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

CONTENT=""
UNLIMITED=0
while read -r TID IP HW; do
    [ -n "$TID" ] || continue
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
