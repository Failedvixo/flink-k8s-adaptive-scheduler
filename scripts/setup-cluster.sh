#!/bin/bash
# ============================================
# Complete Cluster Setup Script
# ============================================

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

echo "=========================================="
echo "  Flink Adaptive Scheduler - Setup"
echo "=========================================="
echo ""

# ============================================
# Check prerequisites
# ============================================

log_info "Checking prerequisites..."

if ! command -v kubectl &> /dev/null; then
    log_error "kubectl not found. Install from: https://kubernetes.io/docs/tasks/tools/"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    log_error "docker not found. Install from: https://www.docker.com/get-started"
    exit 1
fi

if ! command -v minikube &> /dev/null; then
    log_error "minikube not found. Install from: https://minikube.sigs.k8s.io/docs/start/"
    exit 1
fi

if ! command -v mvn &> /dev/null; then
    log_error "maven not found. Install from: https://maven.apache.org/download.cgi"
    exit 1
fi

log_info "All prerequisites found ✓"
echo ""

# ============================================
# Start Minikube
# ============================================

log_info "Starting Minikube cluster..."

if minikube status &> /dev/null; then
    log_warn "Minikube already running. Delete old cluster? (y/N)"
    read -r response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        minikube delete
    else
        log_info "Using existing cluster"
    fi
fi

if ! minikube status &> /dev/null; then
    log_info "Creating new cluster with 3 nodes..."
    minikube start \
        --nodes=3 \
        --cpus=4 \
        --memory=8192 \
        --driver=docker
fi

log_info "Labeling nodes..."
kubectl label nodes minikube node-role=control-plane --overwrite
kubectl label nodes minikube-m02 node-role=worker --overwrite
kubectl label nodes minikube-m03 node-role=worker --overwrite

log_info "Cluster nodes:"
kubectl get nodes
echo ""

# ============================================
# Build Scheduler
# ============================================

log_info "Building Adaptive Scheduler..."
cd scheduler

mvn clean package -DskipTests
if [ $? -ne 0 ]; then
    log_error "Maven build failed"
    exit 1
fi

log_info "Building Docker image..."
docker build -t adaptive-scheduler:1.0 .

log_info "Loading image to minikube..."
minikube image load adaptive-scheduler:1.0

cd ..
log_info "Scheduler built ✓"
echo ""

# ============================================
# Deploy Scheduler
# ============================================

log_info "Deploying Adaptive Scheduler..."

kubectl apply -f kubernetes/scheduler-manifests.yaml

log_info "Waiting for scheduler to be ready..."
kubectl wait --for=condition=available --timeout=120s \
    deployment/adaptive-scheduler -n kube-system || {
    log_error "Scheduler deployment failed"
    kubectl logs -n kube-system -l app=adaptive-scheduler --tail=50
    exit 1
}

log_info "Scheduler deployed ✓"
echo ""

# ============================================
# Deploy Flink
# ============================================

log_info "Deploying Flink cluster..."

kubectl apply -f kubernetes/flink-manifests.yaml

log_info "Waiting for Flink JobManager..."
kubectl wait --for=condition=available --timeout=120s \
    deployment/flink-jobmanager -n flink || {
    log_error "Flink JobManager deployment failed"
    kubectl logs -n flink -l component=jobmanager --tail=50
    exit 1
}

sleep 10

log_info "Flink cluster deployed ✓"
echo ""

# ============================================
# Verify
# ============================================

log_info "=========================================="
log_info "  Deployment Complete!"
log_info "=========================================="
echo ""

log_info "Scheduler status:"
kubectl get pods -n kube-system -l app=adaptive-scheduler

echo ""
log_info "Flink cluster status:"
kubectl get pods -n flink -o wide

echo ""
log_info "=========================================="
log_info "  Access Information"
log_info "=========================================="
echo ""

echo "1. View Scheduler Logs:"
echo "   kubectl logs -f -n kube-system -l app=adaptive-scheduler"
echo ""

echo "2. View Flink Logs:"
echo "   kubectl logs -f -n flink -l component=taskmanager"
echo ""

echo "3. Access Flink UI:"
echo "   kubectl port-forward -n flink svc/flink-jobmanager 8081:8081"
echo "   Open: http://localhost:8081"
echo ""

echo "4. Monitor TaskManager placement:"
echo "   watch kubectl get pods -n flink -l component=taskmanager -o wide"
echo ""

echo "5. Submit test job:"
echo "   kubectl exec -n flink deployment/flink-jobmanager -- \\"
echo "     flink run /opt/flink/examples/streaming/WordCount.jar"
echo ""

log_info "Environment ready! 🚀"