#!/bin/bash
# ============================================
# Point Flink's checkpoints at shared storage (MinIO)
# ============================================
#
# Without this, every TaskManager checkpoints to its own local /tmp, and the
# first rescale that moves a subtask to another TaskManager fails to restore:
#
#   FileNotFoundException: /tmp/checkpoints/<job>/chk-N/... (No such file)
#   -> BackendBuildingException -> the job restart-loops
#
# That breaks precisely the arms this thesis measures. Stock Flink anchors slices
# to the slot holding their state and mostly escapes it; ROUND_ROBIN, LEAST_LOADED,
# ACO and GA move state on purpose and crash, so a campaign run on local
# checkpoint storage would be measuring an infrastructure artefact.
#
# What it does:
#   1. deploys MinIO (kubernetes/minio.yaml) with a flink-checkpoints bucket
#   2. patches the JobManager and TaskManager deployments to
#      - load the s3-presto plugin that already ships inside the Flink image
#      - checkpoint/savepoint to s3://
#
# The patches are strategic-merge on the live deployments and do NOT edit the
# versioned manifest, so `kubectl apply -f kubernetes/flink-manifests.yaml`
# restores local checkpointing.
#
# Usage:
#   scripts/setup-shared-checkpoints.sh          # apply
#   scripts/setup-shared-checkpoints.sh --check  # report what is configured now

set -eu

NAMESPACE=flink
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUCKET=flink-checkpoints
PLUGIN=flink-s3-fs-presto-1.18.0.jar
ENDPOINT="http://minio.flink.svc.cluster.local:9000"

if [ "${1:-}" = "--check" ]; then
    echo "MinIO:"
    kubectl get pods -n "$NAMESPACE" -l app=minio --no-headers 2>/dev/null || echo "  (not deployed)"
    for component in jobmanager taskmanager; do
        echo "flink-$component checkpoint dir:"
        kubectl get deploy "flink-$component" -n "$NAMESPACE" \
            -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}' \
            2>/dev/null | grep -E "state.checkpoints.dir|s3.endpoint" | sed 's/^/  /' || echo "  (unset)"
    done
    exit 0
fi

echo "[1/3] Deploying MinIO..."
kubectl apply -f "$ROOT_DIR/kubernetes/minio.yaml" >/dev/null
kubectl rollout status deployment/minio -n "$NAMESPACE" --timeout=300s

# The Flink image already carries the S3 filesystems under /opt/flink/opt; the
# official entrypoint moves the named one into plugins/ when this variable is
# set, so no image rebuild and no extra jar shipping is needed.
patch_component() {
    local component="$1"
    local current
    current=$(kubectl get deploy "flink-$component" -n "$NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FLINK_PROPERTIES")].value}')

    # Rewrite only the storage lines, so every other tuned property (scheduler,
    # slot counts, adaptive timeouts) survives untouched.
    local updated
    updated=$(printf '%s\n' "$current" | grep -vE '^\s*(state\.checkpoints\.dir|state\.savepoints\.dir|s3\.)' )
    updated="$updated
state.checkpoints.dir: s3://$BUCKET/checkpoints
state.savepoints.dir: s3://$BUCKET/savepoints
s3.endpoint: $ENDPOINT
s3.path.style.access: true
s3.access-key: flinkcheckpoints
s3.secret-key: flinkcheckpoints"

    local patch
    patch=$(CONTAINER="$component" PROPS="$updated" PLUGIN_JAR="$PLUGIN" python3 -c '
import json, os
print(json.dumps({
    "spec": {"template": {"spec": {"containers": [{
        "name": os.environ["CONTAINER"],
        "env": [
            {"name": "FLINK_PROPERTIES", "value": os.environ["PROPS"]},
            {"name": "ENABLE_BUILT_IN_PLUGINS", "value": os.environ["PLUGIN_JAR"]},
        ],
    }]}}}
}))')

    kubectl patch deployment "flink-$component" -n "$NAMESPACE" --type=strategic -p "$patch" >/dev/null
}

echo "[2/3] Patching Flink deployments..."
for component in jobmanager taskmanager; do
    patch_component "$component"
    echo "      flink-$component -> s3://$BUCKET"
done

echo "[3/3] Restarting Flink..."
kubectl rollout restart deployment/flink-jobmanager deployment/flink-taskmanager -n "$NAMESPACE" >/dev/null
kubectl rollout status deployment/flink-taskmanager -n "$NAMESPACE" --timeout=300s
kubectl rollout status deployment/flink-jobmanager -n "$NAMESPACE" --timeout=300s

echo ""
echo "Done. Checkpoints now go to s3://$BUCKET via MinIO."
echo "The thesis fork patch survives this (a rollout restart keeps the deployment"
echo "spec); only 'kubectl apply -f kubernetes/flink-manifests.yaml' removes both."
