#!/bin/bash
# ============================================
# Change ONE Flink property on a live deployment, keeping every other one
# ============================================
#
# `FLINK_PROPERTIES` is a single multi-line env var, so the obvious
# `kubectl set env` replaces the WHOLE block and silently drops the thesis fork's
# settings, the s3/MinIO checkpoint config and the TaskManager memory split. This
# reads the current block, rewrites only the requested key, and patches it back
# as a strategic merge — the same approach scripts/setup-shared-checkpoints.sh
# uses, generalised so each new knob does not need its own script.
#
# The versioned manifest is NOT touched, so
# `kubectl apply -f kubernetes/flink-manifests.yaml` still reverts everything.
#
# Usage:
#   scripts/patch-flink-property.sh <jobmanager|taskmanager|both> <key> <value>
#   scripts/patch-flink-property.sh --show <key>
#
# Examples (the knobs this thesis has had to set, and why):
#   # the pool must not release its surplus slots mid-measurement, or the
#   # assigner's decision space changes under the window
#   scripts/patch-flink-property.sh jobmanager slot.idle.timeout 300000
#
#   # key groups are integer-divided among subtasks, so a maxParallelism that
#   # does not divide the parallelism creates real per-subtask load imbalance
#   scripts/patch-flink-property.sh both pipeline.max-parallelism 8
#
#   # managed memory is for RocksDB/batch; with HashMapStateBackend it is
#   # reserved and idle, and freeing it goes straight to the task heap
#   scripts/patch-flink-property.sh taskmanager taskmanager.memory.managed.size 16m

set -eu

NAMESPACE=flink

# Checked before anything else: with the cluster down every kubectl below returns
# empty, and an unguarded `grep || echo unset` would report "unset -> Flink
# default" for properties that are in fact set — the most misleading answer this
# script could give.
if ! kubectl get nodes >/dev/null 2>&1; then
    echo "ERROR: cluster unreachable (minikube start)" >&2
    exit 1
fi

if [ "${1:-}" = "--show" ]; then
    KEY="${2:?usage: --show <key>}"
    for component in jobmanager taskmanager; do
        printf '%-14s ' "$component"
        PROPS=$(kubectl get deploy "flink-$component" -n "$NAMESPACE" \
            -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='FLINK_PROPERTIES')].value}" 2>/dev/null)
        printf '%s\n' "$PROPS" | grep -E "^\s*${KEY}:" || echo "(unset -> Flink default)"
    done
    exit 0
fi

TARGET="${1:?usage: patch-flink-property.sh <jobmanager|taskmanager|both> <key> <value>}"
KEY="${2:?missing key}"
VALUE="${3:?missing value}"

case "$TARGET" in
    jobmanager|taskmanager) COMPONENTS="$TARGET" ;;
    both)                   COMPONENTS="jobmanager taskmanager" ;;
    *) echo "ERROR: target must be jobmanager, taskmanager or both" >&2; exit 1 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for component in $COMPONENTS; do
    CURRENT=$(kubectl get deploy "flink-$component" -n "$NAMESPACE" \
        -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='FLINK_PROPERTIES')].value}")
    if [ -z "$CURRENT" ]; then
        echo "ERROR: flink-$component has no FLINK_PROPERTIES" >&2; exit 1
    fi

    KEY="$KEY" VALUE="$VALUE" COMPONENT="$component" PROPS="$CURRENT" \
        python3 - > "$WORK/$component.json" <<'PY'
import json, os

key, value = os.environ["KEY"], os.environ["VALUE"]
kept = [ln for ln in os.environ["PROPS"].splitlines()
        if ln.strip() and not ln.strip().startswith(key + ":")]
kept.append(f"{key}: {value}")

print(json.dumps({
    "spec": {"template": {"spec": {"containers": [
        {"name": os.environ["COMPONENT"],
         "env": [{"name": "FLINK_PROPERTIES", "value": "\n".join(kept)}]}
    ]}}}
}))
PY

    KEPT=$(python3 -c "
import json,sys
p=json.load(open('$WORK/$component.json'))
print(len(p['spec']['template']['spec']['containers'][0]['env'][0]['value'].splitlines()))")
    kubectl patch deployment "flink-$component" -n "$NAMESPACE" \
        --type=strategic --patch-file "$WORK/$component.json" >/dev/null
    echo "flink-$component: $KEY = $VALUE   ($KEPT properties total)"
done

echo "Restarting..."
for component in $COMPONENTS; do
    kubectl rollout restart "deployment/flink-$component" -n "$NAMESPACE" >/dev/null
done
for component in $COMPONENTS; do
    kubectl rollout status "deployment/flink-$component" -n "$NAMESPACE" --timeout=300s
done

# The benchmark jar lives in the JobManager's /tmp and does not survive a restart;
# every experiment script re-uploads it, but a manual `flink run` right after this
# would fail with a confusing "file not found".
case "$COMPONENTS" in
    *jobmanager*) echo "Note: /tmp/nexmark.jar in the JobManager is gone — the experiment scripts re-upload it." ;;
esac
