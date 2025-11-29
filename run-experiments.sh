#!/bin/bash

SCHEDULER_DIR="scheduler"
RESULTS_DIR="results/baseline-experiments"

mkdir -p $RESULTS_DIR

echo "=========================================="
echo "  EXPERIMENTOS DE BASELINE"
echo "=========================================="

# Función para cambiar estrategia en el scheduler
change_strategy() {
  local STRATEGY=$1
  
  echo ""
  echo "Configurando scheduler para: $STRATEGY"
  
  cd $SCHEDULER_DIR
  
  # Backup si no existe
  if [ ! -f src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.backup ]; then
    cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java \
       src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.backup
  fi
  
  # Restaurar backup
  cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.backup \
     src/main/java/com/thesis/scheduler/AdaptiveScheduler.java
  
  # Modificar para estrategia fija
  case $STRATEGY in
    "FCFS")
      sed -i '/private SchedulingStrategyType selectStrategyForCpu/,/^    }$/c\
    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {\
        return SchedulingStrategyType.FCFS;\
    }' src/main/java/com/thesis/scheduler/AdaptiveScheduler.java
      ;;
    "BALANCED")
      sed -i '/private SchedulingStrategyType selectStrategyForCpu/,/^    }$/c\
    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {\
        return SchedulingStrategyType.BALANCED;\
    }' src/main/java/com/thesis/scheduler/AdaptiveScheduler.java
      ;;
    "LEAST_LOADED")
      sed -i '/private SchedulingStrategyType selectStrategyForCpu/,/^    }$/c\
    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {\
        return SchedulingStrategyType.LEAST_LOADED;\
    }' src/main/java/com/thesis/scheduler/AdaptiveScheduler.java
      ;;
    "ADAPTIVE")
      # Ya está en backup, no hacer nada
      ;;
  esac
  
  # Recompilar
  echo "  Compilando scheduler..."
  mvn clean package -DskipTests -q
  
  # Reconstruir imagen Docker
  echo "  Construyendo imagen Docker..."
  docker build -t adaptive-scheduler:$STRATEGY . -q
  
  # Cargar en Minikube
  echo "  Cargando en Minikube..."
  minikube image load adaptive-scheduler:$STRATEGY
  
  # Actualizar deployment
  kubectl set image deployment/adaptive-scheduler -n kube-system \
    adaptive-scheduler=adaptive-scheduler:$STRATEGY
  
  # Esperar que se reinicie
  kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=60s
  
  sleep 10
  
  cd ..
}

# Función para ejecutar experimento
run_experiment() {
  local STRATEGY=$1
  local EXP_NAME="exp-$STRATEGY"
  
  echo ""
  echo "=========================================="
  echo "  EXPERIMENTO: $STRATEGY"
  echo "=========================================="
  
  # Cambiar scheduler
  change_strategy $STRATEGY
  
  # Limpiar jobs anteriores
  kubectl exec -n flink deployment/flink-jobmanager -- flink list 2>/dev/null | \
    grep -oP '[0-9a-f]{32}' | \
    xargs -I {} kubectl exec -n flink deployment/flink-jobmanager -- flink cancel {} 2>/dev/null
  
  # Reiniciar TaskManagers
  kubectl scale deployment flink-taskmanager -n flink --replicas=0
  sleep 10
  kubectl scale deployment flink-taskmanager -n flink --replicas=3
  kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s
  
  sleep 10
  
  # Copiar JAR si es necesario
  kubectl cp flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar \
    flink/$(kubectl get pod -n flink -l component=jobmanager -o jsonpath='{.items[0].metadata.name}'):/tmp/nexmark.jar 2>/dev/null || true
  
  # Ejecutar job
  echo "  Ejecutando Nexmark job..."
  JOB_OUTPUT=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    flink run -d /tmp/nexmark.jar 50000 180 4 10 2>&1)
  
  JOB_ID=$(echo "$JOB_OUTPUT" | grep -oP 'JobID \K[0-9a-f]{32}' || echo "")
  
  if [ -z "$JOB_ID" ]; then
    echo "  ERROR: No se pudo obtener Job ID"
    echo "$JOB_OUTPUT"
    return 1
  fi
  
  echo "  Job ID: $JOB_ID"
  
  # Esperar 30s y verificar estado
  sleep 30
  
  JOB_STATUS=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s http://localhost:8081/jobs/$JOB_ID | jq -r '.state' 2>/dev/null || echo "UNKNOWN")
  
  echo "  Job Status: $JOB_STATUS"
  
  if [ "$JOB_STATUS" != "RUNNING" ] && [ "$JOB_STATUS" != "FINISHED" ]; then
    echo "  ERROR: Job no está corriendo"
    kubectl exec -n flink deployment/flink-jobmanager -- flink list -a
    return 1
  fi
  
  # Esperar que termine (180s + 30s buffer)
  echo "  Esperando que el job termine (3 minutos)..."
  sleep 210
  
  # Recolectar métricas
  echo "  Recolectando métricas..."
  
  mkdir -p "$RESULTS_DIR/$EXP_NAME"
  
  # Métricas del job
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$JOB_ID" > "$RESULTS_DIR/$EXP_NAME/job-details.json"
  
  # Scheduler logs
  kubectl logs -n kube-system -l app=adaptive-scheduler --tail=200 > "$RESULTS_DIR/$EXP_NAME/scheduler-logs.txt"
  
  # TaskManager placement
  kubectl get pods -n flink -l component=taskmanager -o wide > "$RESULTS_DIR/$EXP_NAME/taskmanager-placement.txt"
  
  # Node metrics
  kubectl top nodes > "$RESULTS_DIR/$EXP_NAME/node-metrics.txt" 2>&1
  
  # Pod metrics
  kubectl top pods -n flink > "$RESULTS_DIR/$EXP_NAME/pod-metrics.txt" 2>&1
  
  # Extraer métricas clave
  echo "  Extrayendo métricas clave..."
  
  DURATION=$(jq -r '.duration' "$RESULTS_DIR/$EXP_NAME/job-details.json" 2>/dev/null || echo "N/A")
  
  # Calcular throughput de cada vertex
  jq -r '.vertices[] | "\(.name): \(.metrics."read-records") records"' \
    "$RESULTS_DIR/$EXP_NAME/job-details.json" 2>/dev/null > "$RESULTS_DIR/$EXP_NAME/throughput.txt" || echo "N/A" > "$RESULTS_DIR/$EXP_NAME/throughput.txt"
  
  echo "  Experimento $STRATEGY completado"
  echo "  Duración: $DURATION ms"
  echo "  Resultados en: $RESULTS_DIR/$EXP_NAME/"
}

# EJECUTAR EXPERIMENTOS
run_experiment "FCFS"
sleep 30

run_experiment "BALANCED"
sleep 30

run_experiment "LEAST_LOADED"
sleep 30

run_experiment "ADAPTIVE"

# RESTAURAR SCHEDULER ORIGINAL
echo ""
echo "=========================================="
echo "  Restaurando scheduler adaptativo..."
echo "=========================================="
cd $SCHEDULER_DIR
cp src/main/java/com/thesis/scheduler/AdaptiveScheduler.java.backup \
   src/main/java/com/thesis/scheduler/AdaptiveScheduler.java
mvn clean package -DskipTests -q
docker build -t adaptive-scheduler:latest . -q
minikube image load adaptive-scheduler:latest
kubectl set image deployment/adaptive-scheduler -n kube-system \
  adaptive-scheduler=adaptive-scheduler:latest
cd ..

# GENERAR COMPARACIÓN
echo ""
echo "=========================================="
echo "  GENERANDO REPORTE COMPARATIVO"
echo "=========================================="

cat > "$RESULTS_DIR/COMPARISON.md" << 'EOFCOMP'
# Comparación de Estrategias de Scheduling

## Metodología
- **Workload**: Nexmark Benchmark
- **Configuración**: 50,000 eventos/seg, 180 segundos, paralelismo 4
- **TaskManagers**: 3 por experimento
- **Repeticiones**: 1 por estrategia

## Resultados

| Estrategia | Duración | Records Procesados | Throughput | CPU Promedio | Distribución |
|------------|----------|-------------------|------------|--------------|--------------|
EOFCOMP

for STRATEGY in FCFS BALANCED LEAST_LOADED ADAPTIVE; do
  EXP_DIR="$RESULTS_DIR/exp-$STRATEGY"
  
  if [ -f "$EXP_DIR/job-details.json" ]; then
    DURATION=$(jq -r '.duration' "$EXP_DIR/job-details.json" 2>/dev/null || echo "N/A")
    DURATION_MIN=$(echo "scale=2; $DURATION / 60000" | bc 2>/dev/null || echo "N/A")
    
    # Contar records del primer vertex (source)
    RECORDS=$(jq -r '.vertices[0].metrics."write-records" // "N/A"' "$EXP_DIR/job-details.json" 2>/dev/null)
    
    # Throughput
    if [ "$DURATION" != "N/A" ] && [ "$RECORDS" != "N/A" ]; then
      THROUGHPUT=$(echo "scale=0; $RECORDS * 1000 / $DURATION" | bc 2>/dev/null || echo "N/A")
    else
      THROUGHPUT="N/A"
    fi
    
    # CPU (si existe)
    CPU=$(grep -oP 'cpu\s+\K[0-9]+%' "$EXP_DIR/node-metrics.txt" 2>/dev/null | head -1 || echo "N/A")
    
    # Distribución
    DIST=$(awk 'NR>1 {print $7}' "$EXP_DIR/taskmanager-placement.txt" 2>/dev/null | sort | uniq -c | tr '\n' ' ' || echo "N/A")
    
    echo "| $STRATEGY | ${DURATION_MIN}m | $RECORDS | ${THROUGHPUT}/s | $CPU | $DIST |" >> "$RESULTS_DIR/COMPARISON.md"
  else
    echo "| $STRATEGY | FAILED | - | - | - | - |" >> "$RESULTS_DIR/COMPARISON.md"
  fi
done

cat >> "$RESULTS_DIR/COMPARISON.md" << 'EOFOBS'

## Observaciones

### FCFS:
- Primera llegada, primer servicio
- Puede causar desbalance de carga

### BALANCED:
- Distribución equitativa (round-robin)
- Balance predecible

### LEAST_LOADED:
- Asigna al nodo con menos CPU
- Mejor para cargas altas

### ADAPTIVE:
- Cambia estrategia según CPU del cluster
- Combina ventajas de las 3 estrategias

EOFOBS

echo ""
echo "=========================================="
echo "  EXPERIMENTOS COMPLETADOS"
echo "=========================================="
echo ""
cat "$RESULTS_DIR/COMPARISON.md"
echo ""
echo "Resultados detallados en: $RESULTS_DIR/"
