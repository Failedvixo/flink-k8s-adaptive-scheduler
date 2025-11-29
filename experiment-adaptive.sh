#!/bin/bash

echo "=========================================="
echo "  EXPERIMENTO: ADAPTIVE - Alta Carga"
echo "=========================================="

STRATEGY="ADAPTIVE"
RESULTS_DIR="results/high-load/$STRATEGY"

mkdir -p "$RESULTS_DIR"

echo ""
echo "[1/8] Restaurando AdaptiveScheduler.java original (ADAPTIVE)..."
cd scheduler

cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.ORIGINAL \
   src/main/java/com/thesis/scheduler/AdaptiveScheduler.java

echo ""
echo "[2/8] Compilando scheduler..."
mvn clean package -DskipTests

if [ $? -ne 0 ]; then
  echo "ERROR: Compilación falló"
  cd ..
  exit 1
fi

echo ""
echo "[3/8] Construyendo imagen Docker..."
docker build -t adaptive-scheduler:adaptive . -q

echo ""
echo "[4/8] Cargando imagen en Minikube..."
minikube image load adaptive-scheduler:adaptive

echo ""
echo "[5/8] Desplegando scheduler..."
kubectl set image deployment/adaptive-scheduler -n kube-system \
  adaptive-scheduler=adaptive-scheduler:adaptive

kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=60s

cd ..

echo ""
echo "[6/8] Preparando cluster..."

kubectl exec -n flink deployment/flink-jobmanager -- flink list 2>/dev/null | \
  grep -oP '[0-9a-f]{32}' | \
  xargs -I {} kubectl exec -n flink deployment/flink-jobmanager -- flink cancel {} 2>/dev/null

kubectl scale deployment flink-taskmanager -n flink --replicas=0
sleep 10
kubectl scale deployment flink-taskmanager -n flink --replicas=5
kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s

sleep 10

echo ""
echo "Verificando scheduler usa ADAPTIVE:"
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=10

echo ""
echo "[7/8] Ejecutando job Nexmark (200k evt/s, paralelismo 8)..."

JOB_OUTPUT=$(kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark.jar 200000 180 8 10 2>&1)

JOB_ID=$(echo "$JOB_OUTPUT" | grep -oP 'JobID \K[0-9a-f]{32}')

if [ -z "$JOB_ID" ]; then
  echo "ERROR: No se pudo obtener Job ID"
  echo "$JOB_OUTPUT"
  exit 1
fi

echo "Job ID: $JOB_ID"
sleep 30

JOB_STATUS=$(kubectl exec -n flink deployment/flink-jobmanager -- \
  curl -s http://localhost:8081/jobs/$JOB_ID | jq -r '.state' 2>/dev/null)

echo "Job Status: $JOB_STATUS"

echo ""
echo "Monitoreando job (tomando muestras cada 30s)..."

for i in {1..6}; do
  echo ""
  echo "=== Muestra $i/6 (t+$((i*30))s) ==="
  kubectl top nodes
  echo ""
  kubectl get pods -n flink -l component=taskmanager -o wide | awk '{print $1, $7}'
  echo ""
  kubectl logs -n kube-system -l app=adaptive-scheduler --tail=3 | grep -i "strategy"
  sleep 30
done

echo ""
echo "Esperando que el job termine..."
sleep 30

echo ""
echo "[8/8] Recolectando evidencias..."

kubectl exec -n flink deployment/flink-jobmanager -- \
  curl -s "http://localhost:8081/jobs/$JOB_ID" > "$RESULTS_DIR/job-details.json"

kubectl exec -n flink deployment/flink-jobmanager -- \
  flink list -a > "$RESULTS_DIR/jobs-list.txt"

kubectl logs -n kube-system -l app=adaptive-scheduler --tail=300 > "$RESULTS_DIR/scheduler-logs.txt"
kubectl get pods -n flink -l component=taskmanager -o wide > "$RESULTS_DIR/taskmanager-placement.txt"
kubectl top nodes > "$RESULTS_DIR/node-metrics.txt" 2>&1
kubectl top pods -n flink > "$RESULTS_DIR/pod-metrics.txt" 2>&1

DURATION=$(jq -r '.duration' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")
STATE=$(jq -r '.state' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")

# Contar switches
SWITCHES=$(grep -c "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" || echo "0")

cat > "$RESULTS_DIR/SUMMARY.txt" << SUMMARY
========================================
EXPERIMENTO: ADAPTIVE - Alta Carga
========================================

Configuración:
- Eventos/seg: 200,000
- Duración: 180 segundos
- Paralelismo: 8
- TaskManagers: 5

Resultados:
- Job ID: $JOB_ID
- Estado final: $STATE
- Duración: $DURATION ms

Distribución de TaskManagers:
$(awk 'NR>1 {print $7}' "$RESULTS_DIR/taskmanager-placement.txt" | sort | uniq -c)

Scheduling decisions:
$(grep -c "\[SCHEDULING\]" "$RESULTS_DIR/scheduler-logs.txt" || echo "0")

Strategy switches:
$SWITCHES

Switches detectados:
$(grep "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" || echo "Ninguno")

========================================
SUMMARY

echo ""
echo "=========================================="
echo "  EXPERIMENTO ADAPTIVE COMPLETADO"
echo "=========================================="
echo ""
cat "$RESULTS_DIR/SUMMARY.txt"
echo ""
echo "Resultados detallados en: $RESULTS_DIR/"

EOF
