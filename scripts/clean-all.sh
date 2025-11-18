#!/bin/bash
# ============================================
# Clean All Resources
# ============================================

set -e

echo "=========================================="
echo "  Cleaning All Resources"
echo "=========================================="
echo ""

# Delete Flink
echo "[1/3] Deleting Flink cluster..."
kubectl delete namespace flink --ignore-not-found=true

# Delete Scheduler
echo "[2/3] Deleting Adaptive Scheduler..."
kubectl delete deployment adaptive-scheduler -n kube-system --ignore-not-found=true
kubectl delete configmap scheduler-config -n kube-system --ignore-not-found=true
kubectl delete serviceaccount adaptive-scheduler -n kube-system --ignore-not-found=true
kubectl delete clusterrolebinding adaptive-scheduler --ignore-not-found=true
kubectl delete clusterrole adaptive-scheduler --ignore-not-found=true

# Optional: Delete Minikube cluster
echo ""
echo "[3/3] Delete Minikube cluster? (y/N)"
read -r response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    minikube delete
    echo "✓ Cluster deleted"
else
    echo "✓ Cluster kept running"
fi

echo ""
echo "=========================================="
echo "  Cleanup Complete!"
echo "=========================================="