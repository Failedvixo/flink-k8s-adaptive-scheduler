#!/bin/bash

STRATEGY=$1

if [ -z "$STRATEGY" ]; then
  echo "Uso: ./deploy-scheduler-manual.sh <fcfs|balanced|leastloaded|adaptive>"
  exit 1
fi

echo "=========================================="
echo "  Desplegando scheduler: $STRATEGY"
echo "=========================================="

cd scheduler

# Compilar
echo ""
echo "[1/4] Compilando..."
mvn clean package -DskipTests

if [ $? -ne 0 ]; then
  echo "ERROR: Compilación falló"
  cd ..
  exit 1
fi

# Docker
echo ""
echo "[2/4] Construyendo imagen..."
docker build -t adaptive-scheduler:$STRATEGY . -q

# Minikube
echo ""
echo "[3/4] Cargando en Minikube..."
minikube image load adaptive-scheduler:$STRATEGY

# Deploy (nombre del contenedor: scheduler)
echo ""
echo "[4/4] Actualizando deployment..."
kubectl set image deployment/adaptive-scheduler -n kube-system \
  scheduler=adaptive-scheduler:$STRATEGY

# Esperar
kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=60s

cd ..

echo ""
echo "=========================================="
echo "  Scheduler desplegado: $STRATEGY"
echo "=========================================="
echo ""
echo "Verificar estrategia:"
sleep 5
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=5 | grep -i "strategy\|$STRATEGY"
