#!/bin/bash

STRATEGY=$1

if [ -z "$STRATEGY" ]; then
  echo "Uso: ./force-scheduler-change.sh <fcfs|balanced|leastloaded|adaptive>"
  exit 1
fi

echo "=========================================="
echo "  FORZANDO CAMBIO A: $STRATEGY"
echo "=========================================="

cd scheduler

# Limpiar
echo "[1/6] Limpiando compilación anterior..."
mvn clean -q

# Recompilar
echo "[2/6] Recompilando..."
mvn package -DskipTests

if [ $? -ne 0 ]; then
  echo "ERROR: Compilación falló"
  exit 1
fi

# Docker sin cache
echo "[3/6] Construyendo imagen Docker (sin cache)..."
docker build --no-cache -t adaptive-scheduler:$STRATEGY . -q

# Minikube
echo "[4/6] Cargando en Minikube..."
minikube image load adaptive-scheduler:$STRATEGY

# Eliminar pod viejo
echo "[5/6] Eliminando pod viejo..."
kubectl delete pod -n kube-system -l app=adaptive-scheduler

sleep 5

# Actualizar deployment
kubectl set image deployment/adaptive-scheduler -n kube-system \
  scheduler=adaptive-scheduler:$STRATEGY

# Esperar
kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=60s

cd ..

# Verificar
echo ""
echo "[6/6] Verificando..."
sleep 10

kubectl logs -n kube-system -l app=adaptive-scheduler --tail=10 | grep -i "strategy"

echo ""
echo "=========================================="
echo "Si NO muestra Strategy: $STRATEGY arriba,"
echo "revisa el código fuente manualmente:"
echo "  nano scheduler/src/main/java/com/thesis/scheduler/AdaptiveScheduler.java"
echo "=========================================="
