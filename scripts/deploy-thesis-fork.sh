#!/bin/bash
# ============================================
# Deploy the forked Flink scheduler into the JobManager
# ============================================
#
# Ships the thesis fork's allocator classes into the running JobManager without
# rebuilding or reloading the 921MB Flink image (which saturates WSL). Only the
# JM runs custom code, so the patched flink-dist jar is placed on the minikube
# control-plane node via hostPath and mounted over the original jar; the
# TaskManagers keep the official image.
#
# Usage:
#   scripts/deploy-thesis-fork.sh [STRATEGY]
#
#   STRATEGY  STOCK | FCFS | ROUND_ROBIN | LEAST_LOADED | LPT | ACO | GA  (default: ROUND_ROBIN)
#
#   STOCK delegates to the assigner unpatched Flink would have used, so it is the
#   experimental baseline; FCFS is iteration order unconditionally, which stock
#   Flink only does on a first submission (it was called DEFAULT before Phase 3,
#   and that name is still accepted). ACO and GA search the balance-versus-state-
#   locality cost instead of following a fixed rule.
#
# Since Phase 3 the strategy is ALSO the initial contents of the arm file the
# JobManager watches (/var/thesis/arm), so the meta-scheduler can change arms
# mid-run with scripts/publish-arm.sh and no restart. The env var stays as the
# fallback the JobManager uses when no arm has been published.
#
# Revert with:  kubectl apply -f kubernetes/flink-manifests.yaml

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

STRATEGY="${1:-ROUND_ROBIN}"
case "$STRATEGY" in
    STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|LPT|ACO|GA) ;;
    *)
        log_error "Unknown strategy '$STRATEGY' (expected STOCK, FCFS, ROUND_ROBIN, LEAST_LOADED, LPT, ACO or GA)"
        exit 1
        ;;
esac

FORK_DIR="${FORK_DIR:-$HOME/projects/flink-custom-scheduler}"
JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"
NAMESPACE=flink
FLINK_DIST_JAR=flink-dist-1.18.0.jar
THESIS_DIR=/var/thesis
HOST_JAR_PATH=$THESIS_DIR/flink-dist-thesis.jar
ARM_FILE=$THESIS_DIR/arm
CLASS_DIR="$FORK_DIR/flink-runtime/target/classes"
CLASS_PACKAGE=org/apache/flink/runtime/scheduler/adaptive/allocator

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

echo "=========================================="
echo "  Deploy thesis fork  (strategy: $STRATEGY)"
echo "=========================================="
echo ""

# ============================================
# 1. Preconditions
# ============================================
log_info "[1/6] Checking cluster..."
if ! kubectl get nodes >/dev/null 2>&1; then
    log_error "Cannot reach the cluster. Is minikube running? (minikube start)"
    exit 1
fi
kubectl config set-context --current --namespace="$NAMESPACE" >/dev/null

JM_POD=$(kubectl get pods -n "$NAMESPACE" -l app=flink,component=jobmanager -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -z "$JM_POD" ]; then
    log_error "No JobManager pod found in namespace $NAMESPACE"
    exit 1
fi
# TaskManagers always run the official image, so their flink-dist jar is a
# pristine base even when the JM is already running a patched one.
TM_POD=$(kubectl get pods -n "$NAMESPACE" -l app=flink,component=taskmanager -o jsonpath='{.items[0].metadata.name}')
log_info "      JobManager: $JM_POD / base jar from TaskManager: $TM_POD"

# ============================================
# 2. Compile the fork
# ============================================
log_info "[2/6] Compiling flink-runtime..."
(cd "$FORK_DIR" && JAVA_HOME="$JAVA_HOME" mvn -q compile -pl flink-runtime -Dfast)

# ============================================
# 3. Patch the jar
# ============================================
log_info "[3/6] Building patched $FLINK_DIST_JAR..."
kubectl cp -n "$NAMESPACE" "$TM_POD:/opt/flink/lib/$FLINK_DIST_JAR" "$WORK_DIR/thesis.jar" >/dev/null 2>&1
if [ ! -s "$WORK_DIR/thesis.jar" ]; then
    log_error "Failed to copy the base jar out of $TM_POD"
    exit 1
fi

# Every class of the allocator package that the fork rebuilt, inner and
# synthetic classes included: missing e.g. ThesisSlotAssigner\$1 (the switch map)
# only fails at runtime, with a NoClassDefFoundError inside the scheduler.
CLASS_COUNT=0
while IFS= read -r class_file; do
    (cd "$CLASS_DIR" && jar uf "$WORK_DIR/thesis.jar" "$class_file")
    CLASS_COUNT=$((CLASS_COUNT + 1))
done < <(cd "$CLASS_DIR" && find "$CLASS_PACKAGE" -name '*.class' | sort)
log_info "      Patched $CLASS_COUNT allocator classes into the jar"

# ============================================
# 4. Ship it to the control-plane node
# ============================================
log_info "[4/6] Copying jar to minikube node..."
minikube ssh -n minikube -- sudo mkdir -p "$THESIS_DIR"
minikube cp "$WORK_DIR/thesis.jar" "minikube:$HOST_JAR_PATH"
minikube ssh -n minikube -- sudo chmod 644 "$HOST_JAR_PATH"

# Publish the starting arm before the JobManager comes up, so its very first
# assignment already follows the requested strategy instead of the env fallback.
"$(dirname "$0")/publish-arm.sh" "$STRATEGY" >/dev/null

# ============================================
# 5. Patch the JobManager deployment
# ============================================
log_info "[5/6] Patching JobManager deployment..."
kubectl patch deployment flink-jobmanager -n "$NAMESPACE" --type=strategic -p "$(cat <<EOF
{
  "spec": {
    "template": {
      "spec": {
        "nodeName": "minikube",
        "containers": [
          {
            "name": "jobmanager",
            "env": [
              {"name": "THESIS_SLOT_ASSIGNER", "value": "true"},
              {"name": "THESIS_ASSIGN_STRATEGY", "value": "$STRATEGY"},
              {"name": "THESIS_ARM_FILE", "value": "$ARM_FILE"}
            ],
            "volumeMounts": [
              {
                "name": "thesis-flink-dist",
                "mountPath": "/opt/flink/lib/$FLINK_DIST_JAR"
              },
              {
                "name": "thesis-arm",
                "mountPath": "$THESIS_DIR",
                "readOnly": true
              }
            ]
          }
        ],
        "volumes": [
          {
            "name": "thesis-flink-dist",
            "hostPath": {"path": "$HOST_JAR_PATH", "type": "File"}
          },
          {
            "name": "thesis-arm",
            "hostPath": {"path": "$THESIS_DIR", "type": "Directory"}
          }
        ]
      }
    }
  }
}
EOF
)" >/dev/null

# The jar is mounted from the node, so a deployment patch that changes nothing
# else still needs a restart to pick up a newly built jar.
kubectl rollout restart deployment/flink-jobmanager -n "$NAMESPACE" >/dev/null
log_info "      Waiting for the JobManager to come back..."
kubectl rollout status deployment/flink-jobmanager -n "$NAMESPACE" --timeout=180s

# ============================================
# 6. Report
# ============================================
# The replaced pod may still be terminating and sorts ahead of the new one, so
# ask for the newest Running pod rather than the first one listed.
NEW_JM=$(kubectl get pods -n "$NAMESPACE" -l app=flink,component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')
log_info "[6/6] Done. JobManager: $NEW_JM"
echo ""
echo "Submit a job and watch the placement:"
echo "  kubectl exec -n $NAMESPACE $NEW_JM -- flink run -d /opt/flink/examples/streaming/TopSpeedWindowing.jar"
echo "  kubectl logs -n $NAMESPACE $NEW_JM -f | grep -E 'THESIS_ASSIGN|THESIS_ARM'"
echo ""
echo "Change the arm without restarting the JobManager:"
echo "  scripts/publish-arm.sh LEAST_LOADED   # takes effect on the next rescale"
echo ""
log_warn "Revert with: kubectl apply -f kubernetes/flink-manifests.yaml"
