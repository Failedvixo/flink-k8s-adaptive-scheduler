#!/bin/bash
# ============================================
# Switch the cluster between one uniform TaskManager pool and three speed classes
# ============================================
#
# WHY. The offline bench proves the homogeneous cluster poses a degenerate problem:
# on identical TaskManagers a load-aware greedy is exactly optimal (0.0% gap up to 8
# slices), so every arm necessarily ties and STOCK ties with them. Heterogeneity is
# the strong lever — 9-14% at 4 slices — and the premise of Li et al. rather than a
# detail of their setup.
#
# This flips the cluster between the two worlds without touching anything else, so a
# campaign can be run twice and the difference attributed to the machines:
#
#   --heterogeneous   flink-tm-fast (2 cores) + flink-tm-medium (1.5) + flink-tm-slow (1),
#                     one replica each, and the uniform pool scaled to zero. Six slots,
#                     same as three uniform TaskManagers, so only speed changes.
#   --homogeneous     the uniform pool back at TM_REPLICAS, classes removed, speeds cleared.
#   --status          what the cluster looks like and what speed vector is published.
#
# Publishing the speeds is part of the switch on purpose: a heterogeneous cluster the
# assigner believes is uniform is worse than either world, since every arm would then
# optimise towards an equal share the machines cannot deliver.
#
# The switch destroys TaskManagers, so it must not run under a job that matters. Any
# RUNNING job is reported and confirmation required.

set -eu

NAMESPACE=flink
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/../kubernetes/flink-taskmanager-classes.yaml"
CLASSES="flink-tm-fast flink-tm-medium flink-tm-slow"
TM_REPLICAS="${TM_REPLICAS:-3}"
# Which speed class the assigner reaches FIRST when every machine is still idle.
#
# This is an experimental condition, not a detail. The greedy arms break ties at zero load by
# taking the first TaskManager in the assigner's order, and on unequal machines that choice is
# unrecoverable: measured in the offline bench, the same LEAST_LOADED sits at 1.3% from optimal
# with the fast machine first and 43% with it last. LPT does not move.
#
# The order is the TaskManagers' resource ids sorted as TEXT, so it used to be decided by which
# pod got which IP — a lottery that produced the WRONG condition three times in a row on
# 2026-08-19, once because ".10" sorts before ".9". Each class now carries an explicit
# taskmanager.resource-id, so the condition is chosen here and survives reboots.
ORDER="${ORDER:-slow-first}"

jm_curl() {
    kubectl exec -n "$NAMESPACE" deployment/flink-jobmanager -- \
        curl -s -m 15 "http://localhost:8081$1" 2>/dev/null
}

running_job() {
    jm_curl "/jobs/overview" | python3 -c "
import json, sys
try:
    jobs = [j for j in json.load(sys.stdin).get('jobs', []) if j.get('state') == 'RUNNING']
    print(jobs[0]['jid'] if jobs else '')
except Exception:
    print('')" 2>/dev/null || true
}

show_status() {
    echo "TaskManager deployments:"
    kubectl get deploy -n "$NAMESPACE" -l component=taskmanager \
        -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,CPU:.spec.template.spec.containers[0].resources.limits.cpu' \
        2>/dev/null || true
    kubectl get deploy flink-taskmanager -n "$NAMESPACE" \
        -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,CPU:.spec.template.spec.containers[0].resources.limits.cpu' \
        2>/dev/null || true
    echo ""
    echo "published speeds:"
    "$SCRIPT_DIR/publish-speeds.sh" --read | sed 's/^/  /'
}

confirm_no_job() {
    local jid
    jid=$(running_job)
    if [ -n "$jid" ]; then
        echo "WARNING: job $jid is RUNNING; this removes TaskManagers under it." >&2
        printf "continue? [y/N] "
        read -r answer
        case "$answer" in y|Y|yes|YES) ;; *) echo "aborted"; exit 1 ;; esac
    fi
}

case "${1:-}" in
    --heterogeneous)
        [ "${2:-}" = "--order" ] && ORDER="${3:?--order needs slow-first or fast-first}"
        case "$ORDER" in
            slow-first|fast-first) ;;
            *) echo "ERROR: --order must be slow-first or fast-first" >&2; exit 1 ;;
        esac
        confirm_no_job
        echo "applying the three speed classes ($ORDER)..."
        ORDER="$ORDER" python3 -c "
import os, sys, yaml

# rank 1 sorts first, and 'first' is the machine every greedy tie-break reaches.
ranks = ({'slow': 1, 'medium': 2, 'fast': 3} if os.environ['ORDER'] == 'slow-first'
         else {'fast': 1, 'medium': 2, 'slow': 3})
docs = [d for d in yaml.safe_load_all(open('$MANIFEST')) if d]
for d in docs:
    cls = d['metadata']['labels']['speed-class']
    env = [e for e in d['spec']['template']['spec']['containers'][0]['env']
           if e['name'] == 'FLINK_PROPERTIES'][0]
    env['value'] = '\n'.join(
        'taskmanager.resource-id: tm-%d-%s' % (ranks[cls], cls)
        if line.startswith('taskmanager.resource-id:') else line
        for line in env['value'].splitlines()) + '\n'
yaml.dump_all(docs, sys.stdout)" | kubectl apply -f - >/dev/null
        # The uniform pool goes to zero rather than being deleted: it is the thing to come
        # back to, and its manifest carries knobs (limits.cpu, managed memory) that were
        # set live and exist nowhere else.
        kubectl scale deployment flink-taskmanager -n "$NAMESPACE" --replicas=0 >/dev/null
        for D in $CLASSES; do
            kubectl rollout status "deployment/$D" -n "$NAMESPACE" --timeout=300s >/dev/null
        done
        echo "waiting for the JobManager to register all three..."
        for _ in $(seq 1 30); do
            COUNT=$(jm_curl "/taskmanagers" | python3 -c "
import json, sys
try:
    print(len(json.load(sys.stdin).get('taskmanagers', [])))
except Exception:
    print(0)" 2>/dev/null || echo 0)
            [ "$COUNT" -ge 3 ] && break
            sleep 5
        done
        echo "publishing the speed vector..."
        "$SCRIPT_DIR/publish-speeds.sh"
        echo ""
        echo "condition: $ORDER — the first line above is the machine every greedy tie-break"
        echo "reaches while the cluster is idle."
        echo ""
        show_status
        ;;
    --homogeneous)
        confirm_no_job
        echo "removing the speed classes..."
        kubectl delete -f "$MANIFEST" --ignore-not-found >/dev/null
        kubectl scale deployment flink-taskmanager -n "$NAMESPACE" --replicas="$TM_REPLICAS" >/dev/null
        kubectl rollout status deployment/flink-taskmanager -n "$NAMESPACE" --timeout=300s >/dev/null
        # Identical machines must not be described by a stale vector saying otherwise.
        "$SCRIPT_DIR/publish-speeds.sh" --clear
        echo ""
        show_status
        ;;
    --status)
        show_status
        ;;
    *)
        echo "Usage: $0 --heterogeneous [--order slow-first|fast-first] | --homogeneous | --status" >&2
        exit 1
        ;;
esac
