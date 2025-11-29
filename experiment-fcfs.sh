#!/bin/bash

echo "=========================================="
echo "  EXPERIMENTO: FCFS - Alta Carga"
echo "=========================================="

STRATEGY="FCFS"
RESULTS_DIR="results/high-load/$STRATEGY"

mkdir -p "$RESULTS_DIR"

# PASO 1: Editar código del scheduler
echo ""
echo "[1/8] Editando AdaptiveScheduler.java para FCFS..."
cd scheduler

# Backup
if [ ! -f src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.ORIGINAL ]; then
  cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java \
     src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.ORIGINAL
fi

# Restaurar original
cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.ORIGINAL \
   src/main/java/com/thesis/scheduler/AdaptiveScheduler.java

# Modificar con Python (más confiable que sed)
python3 << 'PYTHON'
with open('src/main/java/com/thesis/scheduler/AdaptiveScheduler.java', 'r') as f:
    content = f.read()

# Buscar y reemplazar el método completo
old_method = '''    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {
        if (cpuUsage > cpuHighThreshold) {
            return SchedulingStrategyType.LEAST_LOADED;
        } else if (cpuUsage > cpuLowThreshold) {
            return SchedulingStrategyType.BALANCED;
        } else {
            return SchedulingStrategyType.FCFS;
        }
    }'''

new_method = '''    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {
        // EXPERIMENTO: FCFS FIJO
        return SchedulingStrategyType.FCFS;
    }'''

content = content.replace(old_method, new_method)

with open('src/main/java/com/thesis/scheduler/AdaptiveScheduler.java', 'w') as f:
    f.write(content)

print("Método modificado exitosamente")
PYTHON

# PASO 2: Compilar
echo ""
echo "[2/8] Compilando scheduler..."
mvn clean package -DskipTests

if [ $? -ne 0 ]; then
  echo "ERROR: Compilación falló"
  cd ..
  exit 1
fi

# PASO 3: Docker build
echo ""
echo "[3/8] Construyendo imagen Docker..."
docker build -t adaptive-scheduler:fcfs . -q

# PASO 4: Cargar en Minikube
echo ""
echo "[4/8] Cargando imagen en Minikube..."
minikube image load adaptive-scheduler:fcfs

# PASO 5: Desplegar
echo ""
echo "[5/8] Desplegando scheduler..."
kubectl set image deployment/adaptive-scheduler -n kube-system \
  adaptive-scheduler=adaptive-scheduler:fcfs

kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=60s

cd ..

# PASO 6: Preparar cluster
echo ""
echo "[6/8] Preparando cluster..."

# Limpiar jobs anteriores
kubectl exec -n flink deployment/flink-jobmanager -- flink list 2>/dev/null | \
  grep -oP '[0-9a-f]{32}' | \
  xargs -I {} kubectl exec -n flink deployment/flink-jobmanager -- flink cancel {} 2>/dev/null

# Escalar TaskManagers
kubectl scale deployment flink-taskmanager -n flink --replicas=0
sleep 10
kubectl scale deployment flink-taskmanager -n flink --replicas=5
kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s

sleep 10

# Verificar scheduler
echo ""
echo "Verificando scheduler usa FCFS:"
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=5 | grep -i "strategy\|fcfs"

# PASO 7: Ejecutar job con ALTA CARGA
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
echo "Job iniciado - esperando 30s..."
sleep 30

# Verificar estado
JOB_STATUS=$(kubectl exec -n flink deployment/flink-jobmanager -- \
  curl -s http://localhost:8081/jobs/$JOB_ID | jq -r '.state' 2>/dev/null)

echo "Job Status: $JOB_STATUS"

# Monitorear
echo ""
echo "Monitoreando job (tomando muestras cada 30s)..."

for i in {1..6}; do
  echo ""
  echo "=== Muestra $i/6 (t+$((i*30))s) ==="
  
  # CPU de nodos
  kubectl top nodes
  
  # Estado de TaskManagers
  echo ""
  kubectl get pods -n flink -l component=taskmanager -o wide | awk '{print $1, $7}'
  
  sleep 30
done

# Esperar que termine
echo ""
echo "Esperando que el job termine..."
sleep 30

# PASO 8: Recolectar evidencias
echo ""
echo "[8/8] Recolectando evidencias..."

# Job final
kubectl exec -n flink deployment/flink-jobmanager -- \
  curl -s "http://localhost:8081/jobs/$JOB_ID" > "$RESULTS_DIR/job-details.json"

kubectl exec -n flink deployment/flink-jobmanager -- \
  flink list -a > "$RESULTS_DIR/jobs-list.txt"

# Scheduler logs
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=300 > "$RESULTS_DIR/scheduler-logs.txt"

# Placement
kubectl get pods -n flink -l component=taskmanager -o wide > "$RESULTS_DIR/taskmanager-placement.txt"

# Recursos
kubectl top nodes > "$RESULTS_DIR/node-metrics.txt" 2>&1
kubectl top pods -n flink > "$RESULTS_DIR/pod-metrics.txt" 2>&1

# Extraer métricas
DURATION=$(jq -r '.duration' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")
STATE=$(jq -r '.state' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")

# Generar resumen
cat > "$RESULTS_DIR/SUMMARY.txt" << SUMMARY
========================================
EXPERIMENTO: FCFS - Alta Carga
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

========================================
SUMMARY

echo ""
echo "=========================================="
echo "  EXPERIMENTO FCFS COMPLETADO"
echo "=========================================="
echo ""
cat "$RESULTS_DIR/SUMMARY.txt"
echo ""
echo "Resultados detallados en: $RESULTS_DIR/"
