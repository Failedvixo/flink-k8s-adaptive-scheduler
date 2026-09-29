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
    STOCK|FCFS|DEFAULT|ROUND_ROBIN|LEAST_LOADED|LPT|PACK|ACO|GA|OPTIMAL|RL) ;;
    *)
        log_error "Unknown strategy '$STRATEGY' (expected STOCK, FCFS, ROUND_ROBIN, LEAST_LOADED, LPT, PACK, ACO, GA, OPTIMAL or RL)"
        exit 1
        ;;
esac

# The cost weights belong to the DEPLOYMENT, not to a campaign run: they change what the
# JobManager's cost function means, and the JobManager is a different process in a different pod
# from the shell that launches a campaign. Setting them there does nothing at all — the run would
# silently measure weight 0 while the log said otherwise.
#
# THESIS_COST_COMMUNICATION defaults to 0 because the term did not exist when the earlier campaigns
# ran; switching it on changes what ACO and GA optimise.
COST_BALANCE="${THESIS_COST_BALANCE:-1.0}"
COST_LOCALITY="${THESIS_COST_LOCALITY:-1.0}"
COST_COMMUNICATION="${THESIS_COST_COMMUNICATION:-0.0}"
# DISPERSION scores balance as the spread of load across TaskManagers; MAKESPAN scores it as the
# busiest machine alone. They are different problems, not two readings of one: a sum is separable
# and a greedy solves it exactly, while min-max is the NP-hard one and is what actually sets
# end-to-end latency. Default stays DISPERSION so every earlier campaign keeps its meaning.
COST_BALANCE_METRIC="${THESIS_COST_BALANCE_METRIC:-DISPERSION}"

FORK_DIR="${FORK_DIR:-$HOME/projects/flink-custom-scheduler}"
# Flink 2.x RUNS on Java 11 but BUILDS against 17 — compiling the patched classes with 11
# fails outright ("invalid target release: 17"), so this is not a preference.
JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
NAMESPACE=flink
FLINK_DIST_JAR=flink-dist-2.3.0.jar
THESIS_DIR=/var/thesis
HOST_JAR_PATH=$THESIS_DIR/flink-dist-thesis.jar
ARM_FILE=$THESIS_DIR/arm
CLASS_DIR="$FORK_DIR/flink-runtime/target/classes"
CLASS_PACKAGE=org/apache/flink/runtime/scheduler/adaptive/allocator

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

echo "=========================================="
echo "  Deploy thesis fork  (strategy: $STRATEGY)"
echo "  cost weights: balance=$COST_BALANCE locality=$COST_LOCALITY communication=$COST_COMMUNICATION"
echo "  balance metric: $COST_BALANCE_METRIC"
echo "=========================================="
echo ""

# ============================================
# 1. Preconditions
# ============================================
# The fork repo keeps 1.18 on `main` and 2.3 on `flink-2.3`, so it is entirely possible to
# have the wrong branch checked out. Compiling 1.18 classes and patching them into a 2.3.0 jar
# does not fail loudly — it produces a JobManager that crashes on startup or, worse, silently
# falls back — so the version is checked against the jar this script is about to patch.
# Read AFTER </parent>: the first <version> in Flink's root pom belongs to the parent POM
# (org.apache:apache) and yields something like "35" rather than the Flink version.
FORK_VERSION=$(sed -n '/<\/parent>/,$p' "$FORK_DIR/pom.xml" 2>/dev/null |
    grep -m1 -oP '(?<=<version>)[^<]+' || echo "")
EXPECTED_VERSION="${FLINK_DIST_JAR#flink-dist-}"
EXPECTED_VERSION="${EXPECTED_VERSION%.jar}"
if [ "$FORK_VERSION" != "$EXPECTED_VERSION" ]; then
    log_error "Fork at $FORK_DIR declares Flink $FORK_VERSION but this script patches $EXPECTED_VERSION."
    log_error "Check out the matching branch (git -C $FORK_DIR branch -a) and retry."
    exit 1
fi
log_info "Fork version: $FORK_VERSION (matches $FLINK_DIST_JAR)"

log_info "[1/6] Checking cluster..."
if ! kubectl get nodes >/dev/null 2>&1; then
    log_error "Cannot reach the cluster. Is minikube running? (minikube start)"
    exit 1
fi
kubectl config set-context --current --namespace="$NAMESPACE" >/dev/null

JM_POD=$(kubectl get pods -n "$NAMESPACE" -l app=flink,component=jobmanager --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
if [ -z "$JM_POD" ]; then
    log_error "No JobManager pod found in namespace $NAMESPACE"
    exit 1
fi
# TaskManagers always run the official image, so their flink-dist jar is a
# pristine base even when the JM is already running a patched one.
TM_POD=$(kubectl get pods -n "$NAMESPACE" -l app=flink,component=taskmanager --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')
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
# `|| true` is load-bearing: with `set -e` a failing kubectl cp aborts the script on this
# very line, and with stderr silenced it does so WITHOUT PRINTING ANYTHING — the check below
# was unreachable dead code. Observed 2026-08-26: the deploy stopped after "[3/6]" with no
# message at all. Keep the failure, report it, and show what kubectl actually said.
kubectl cp -n "$NAMESPACE" "$TM_POD:/opt/flink/lib/$FLINK_DIST_JAR" "$WORK_DIR/thesis.jar" \
    >/dev/null 2>"$WORK_DIR/cp.err" || true
if [ ! -s "$WORK_DIR/thesis.jar" ]; then
    log_error "Failed to copy the base jar out of $TM_POD"
    [ -s "$WORK_DIR/cp.err" ] && sed 's/^/         kubectl: /' "$WORK_DIR/cp.err" >&2
    exit 1
fi

# Every class of the allocator package that the fork rebuilt, inner and
# synthetic classes included: missing e.g. ThesisSlotAssigner\$1 (the switch map)
# only fails at runtime, with a NoClassDefFoundError inside the scheduler.
#
# Rewritten in Python rather than with `jar uf`, for two reasons found on 2.3:
#   * JDK 17's jar tool VALIDATES the module descriptor on every update, and the 2.3
#     flink-dist carries a shaded module-info whose ModulePackages attribute does not
#     list every Jackson package — so `jar uf` dies with
#     InvalidModuleDescriptorException before writing anything. JDK 11 did not check.
#   * the old loop spawned one JVM per class file.
# Appending duplicate entries would dodge the validation but leave two copies of
# SlotSharingSlotAllocator in the archive, with the winner decided by the reader; this
# substitutes instead, so the jar has exactly one of each class.
CLASS_COUNT=$(CLASS_DIR="$CLASS_DIR" CLASS_PACKAGE="$CLASS_PACKAGE" JAR="$WORK_DIR/thesis.jar" python3 - <<'PY'
import os, pathlib, shutil, sys, zipfile

class_dir = pathlib.Path(os.environ["CLASS_DIR"])
package = os.environ["CLASS_PACKAGE"]
jar = pathlib.Path(os.environ["JAR"])

patched = {
    str(f.relative_to(class_dir)): f
    for f in sorted((class_dir / package).rglob("*.class"))
}
if not patched:
    print("0")
    sys.exit(1)

tmp = jar.with_suffix(".patched")
with zipfile.ZipFile(jar) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
    for item in src.infolist():
        if item.filename in patched:
            continue
        dst.writestr(item, src.read(item.filename))
    for name, path in patched.items():
        dst.writestr(name, path.read_bytes())
shutil.move(tmp, jar)
print(len(patched))
PY
)
[ -n "$CLASS_COUNT" ] && [ "$CLASS_COUNT" -gt 0 ] || { log_error "No allocator classes were patched into the jar"; exit 1; }
log_info "      Patched $CLASS_COUNT allocator classes into the jar"

# ============================================
# 4. Ship it to the control-plane node
# ============================================
log_info "[4/6] Copying jar to minikube node..."
minikube ssh -n minikube -- sudo mkdir -p "$THESIS_DIR"
# WRITABLE BY THE JOBMANAGER (2026-09-16). The RL arm publishes the slice layout here,
# and the Flink container runs as uid 9999: with the hostPath left root-owned and 755
# every write failed, the agent saw no layout, and a whole run was discarded epoch by
# epoch with "sin layout de slices". The scripts keep writing here with sudo, so the
# mode is widened rather than the ownership changed.
minikube ssh -n minikube -- sudo chmod 777 "$THESIS_DIR"
minikube cp "$WORK_DIR/thesis.jar" "minikube:$HOST_JAR_PATH"
minikube ssh -n minikube -- sudo chmod 644 "$HOST_JAR_PATH"

# Publish the starting arm before the JobManager comes up, so its very first
# assignment already follows the requested strategy instead of the env fallback.
"$(dirname "$0")/publish-arm.sh" "$STRATEGY" >/dev/null

# ============================================
# 5. Patch the JobManager deployment
# ============================================
# THE THESIS DIRECTORY IS MOUNTED READ-WRITE since 2026-09-16 (it was read-only). The RL
# arm publishes the slice layout to /var/thesis/slices on every decision: slices are built
# inside the JobManager from the slot sharing groups and are invisible over the REST API, so
# an external agent cannot decide a placement — nor respect a machine's capacity — without
# it. The write is best effort in the fork, so a read-only mount would only cost that file,
# never a rescale.
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
              {"name": "THESIS_ARM_FILE", "value": "$ARM_FILE"},
              {"name": "THESIS_COST_BALANCE", "value": "$COST_BALANCE"},
              {"name": "THESIS_COST_LOCALITY", "value": "$COST_LOCALITY"},
              {"name": "THESIS_COST_COMMUNICATION", "value": "$COST_COMMUNICATION"},
              {"name": "THESIS_COST_BALANCE_METRIC", "value": "$COST_BALANCE_METRIC"}
            ],
            "volumeMounts": [
              {
                "name": "thesis-flink-dist",
                "mountPath": "/opt/flink/lib/$FLINK_DIST_JAR"
              },
              {
                "name": "thesis-arm",
                "mountPath": "$THESIS_DIR",
                "readOnly": false
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
