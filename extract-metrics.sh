#!/bin/bash

#############################################
# EXTRACT-METRICS.SH - Versión Corregida
# Para flink-k8s-adaptive-scheduler
#############################################

STRATEGY=$1
JOB_ID=$2

if [ -z "$STRATEGY" ]; then
  echo "Uso: ./extract-metrics.sh <FCFS|BALANCED|LEAST_LOADED|ADAPTIVE> [JOB_ID]"
  echo ""
  echo "Ejemplos:"
  echo "  ./extract-metrics.sh BALANCED"
  echo "  ./extract-metrics.sh BALANCED e8eeef77d28ed0980f90c6e2629e2906"
  exit 1
fi

RESULTS_DIR="results/high-load/$STRATEGY"
mkdir -p "$RESULTS_DIR"

# Obtener pod del JobManager
JM_POD=$(kubectl get pod -n flink -l component=jobmanager -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -z "$JM_POD" ]; then
  echo "ERROR: No se encontró el JobManager"
  exit 1
fi

# Si no se proporciona JOB_ID, obtener el último job
if [ -z "$JOB_ID" ]; then
  echo "Buscando último job ejecutado..."
  JOB_ID=$(kubectl exec -n flink $JM_POD -- \
    curl -s "http://localhost:8081/jobs/overview" 2>/dev/null | \
    jq -r '.jobs | sort_by(.["start-time"]) | last | .jid')
  
  if [ -z "$JOB_ID" ] || [ "$JOB_ID" == "null" ]; then
    echo "ERROR: No se encontraron jobs"
    exit 1
  fi
fi

echo "=========================================="
echo "  EXTRACCIÓN DE MÉTRICAS: $STRATEGY"
echo "=========================================="
echo "Job ID: $JOB_ID"
echo ""

# Obtener datos completos del job
echo "[1/5] Obteniendo datos del job..."
JOB_DATA=$(kubectl exec -n flink $JM_POD -- \
  curl -s "http://localhost:8081/jobs/$JOB_ID" 2>/dev/null)

if [ -z "$JOB_DATA" ] || [ "$(echo "$JOB_DATA" | jq -r '.jid // empty')" == "" ]; then
  echo "ERROR: No se pudieron obtener datos del job"
  exit 1
fi

# Guardar JSON completo
echo "$JOB_DATA" > "$RESULTS_DIR/job-details.json"

# Extraer información básica
STATE=$(echo "$JOB_DATA" | jq -r '.state')
DURATION_MS=$(echo "$JOB_DATA" | jq -r '.duration')
START_TIME=$(echo "$JOB_DATA" | jq -r '.["start-time"]')
END_TIME=$(echo "$JOB_DATA" | jq -r '.["end-time"]')
JOB_NAME=$(echo "$JOB_DATA" | jq -r '.name')

# Calcular duración en segundos
if [ "$DURATION_MS" != "null" ] && [ "$DURATION_MS" != "-1" ]; then
  DURATION_SEC=$(echo "scale=2; $DURATION_MS / 1000" | bc)
else
  DURATION_SEC="N/A"
fi

echo "  Estado: $STATE"
echo "  Duración: $DURATION_SEC segundos"
echo ""

# Extraer métricas por vertex
echo "[2/5] Extrayendo métricas por operador..."

# Source metrics
SOURCE_RECORDS=$(echo "$JOB_DATA" | jq -r '.vertices[0].metrics["write-records"] // 0')
SOURCE_BYTES=$(echo "$JOB_DATA" | jq -r '.vertices[0].metrics["write-bytes"] // 0')
SOURCE_NAME=$(echo "$JOB_DATA" | jq -r '.vertices[0].name')
SOURCE_PARALLELISM=$(echo "$JOB_DATA" | jq -r '.vertices[0].parallelism')

# Filter/Transform metrics
FILTER_IN=$(echo "$JOB_DATA" | jq -r '.vertices[1].metrics["read-records"] // 0')
FILTER_OUT=$(echo "$JOB_DATA" | jq -r '.vertices[1].metrics["write-records"] // 0')
FILTER_NAME=$(echo "$JOB_DATA" | jq -r '.vertices[1].name')
FILTER_PARALLELISM=$(echo "$JOB_DATA" | jq -r '.vertices[1].parallelism')

# Window metrics
WINDOW_IN=$(echo "$JOB_DATA" | jq -r '.vertices[2].metrics["read-records"] // 0')
WINDOW_OUT=$(echo "$JOB_DATA" | jq -r '.vertices[2].metrics["write-records"] // 0')
WINDOW_NAME=$(echo "$JOB_DATA" | jq -r '.vertices[2].name')
WINDOW_PARALLELISM=$(echo "$JOB_DATA" | jq -r '.vertices[2].parallelism')

# Sink metrics
SINK_IN=$(echo "$JOB_DATA" | jq -r '.vertices[3].metrics["read-records"] // 0')
SINK_NAME=$(echo "$JOB_DATA" | jq -r '.vertices[3].name')
SINK_PARALLELISM=$(echo "$JOB_DATA" | jq -r '.vertices[3].parallelism')

# Calcular throughput
if [ "$DURATION_MS" != "null" ] && [ "$DURATION_MS" != "-1" ] && [ "$DURATION_MS" != "0" ]; then
  THROUGHPUT=$(echo "scale=0; $SOURCE_RECORDS * 1000 / $DURATION_MS" | bc)
else
  THROUGHPUT="N/A"
fi

# Calcular eficiencia del filtro
if [ "$SOURCE_RECORDS" != "0" ]; then
  FILTER_EFFICIENCY=$(echo "scale=1; $FILTER_OUT * 100 / $SOURCE_RECORDS" | bc)
else
  FILTER_EFFICIENCY="N/A"
fi

echo ""
echo "[3/5] Obteniendo distribución de TaskManagers..."

# Distribución de pods
kubectl get pods -n flink -l component=taskmanager -o wide > "$RESULTS_DIR/taskmanager-placement.txt" 2>/dev/null

# Contar pods por nodo
POD_DISTRIBUTION=$(kubectl get pods -n flink -l component=taskmanager -o wide 2>/dev/null | \
  awk 'NR>1 {print $7}' | sort | uniq -c | awk '{print "    "$2": "$1" TaskManagers"}')

TOTAL_TMS=$(kubectl get pods -n flink -l component=taskmanager --no-headers 2>/dev/null | wc -l)
NODES_USED=$(kubectl get pods -n flink -l component=taskmanager -o wide 2>/dev/null | \
  awk 'NR>1 {print $7}' | sort -u | wc -l)

echo ""
echo "[4/5] Obteniendo métricas de recursos..."

# Métricas de nodos (si están disponibles)
kubectl top nodes > "$RESULTS_DIR/node-metrics.txt" 2>&1
kubectl top pods -n flink > "$RESULTS_DIR/pod-metrics.txt" 2>&1

echo ""
echo "[5/5] Obteniendo logs del scheduler..."

# Logs del scheduler
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=500 > "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null

# Contar decisiones de scheduling
SCHEDULING_DECISIONS=$(grep -c "\[SCHEDULING\]" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")
STRATEGY_SWITCHES=$(grep -c "STRATEGY SWITCH\|Strategy changed" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")

# Extraer backpressure
echo "" > "$RESULTS_DIR/backpressure-summary.txt"
for i in 0 1 2 3; do
  VERTEX_ID=$(echo "$JOB_DATA" | jq -r ".vertices[$i].id // empty")
  VERTEX_NAME=$(echo "$JOB_DATA" | jq -r ".vertices[$i].name // empty")
  BP_TIME=$(echo "$JOB_DATA" | jq -r ".vertices[$i].metrics[\"accumulated-backpressured-time\"] // 0")
  IDLE_TIME=$(echo "$JOB_DATA" | jq -r ".vertices[$i].metrics[\"accumulated-idle-time\"] // 0")
  
  if [ -n "$VERTEX_NAME" ]; then
    echo "$VERTEX_NAME:" >> "$RESULTS_DIR/backpressure-summary.txt"
    echo "  Backpressure time: ${BP_TIME}ms" >> "$RESULTS_DIR/backpressure-summary.txt"
    echo "  Idle time: ${IDLE_TIME}ms" >> "$RESULTS_DIR/backpressure-summary.txt"
  fi
done

#############################################
# GENERAR RESUMEN
#############################################

cat > "$RESULTS_DIR/METRICS-SUMMARY.txt" << EOF
==========================================
  MÉTRICAS COMPLETAS - $STRATEGY
==========================================
Fecha: $(date)
Job ID: $JOB_ID
Job Name: $JOB_NAME

CONFIGURACIÓN:
- Workload: Nexmark Benchmark  
- Estrategia: $STRATEGY
- TaskManagers: $TOTAL_TMS
- Nodos utilizados: $NODES_USED

------------------------------------------
1. ESTADO DEL JOB
------------------------------------------
   Estado: $STATE
   Duración: $DURATION_MS ms ($DURATION_SEC s)
   Inicio: $START_TIME
   Fin: $END_TIME

------------------------------------------
2. THROUGHPUT
------------------------------------------
   Eventos generados: $SOURCE_RECORDS
   Throughput: $THROUGHPUT eventos/segundo
   Bytes procesados: $SOURCE_BYTES bytes

------------------------------------------
3. MÉTRICAS POR OPERADOR
------------------------------------------

   $SOURCE_NAME
   ├── Parallelism: $SOURCE_PARALLELISM
   ├── Records out: $SOURCE_RECORDS
   └── Bytes out: $SOURCE_BYTES

   $FILTER_NAME
   ├── Parallelism: $FILTER_PARALLELISM
   ├── Records in: $FILTER_IN
   ├── Records out: $FILTER_OUT
   └── Eficiencia: ${FILTER_EFFICIENCY}%

   $WINDOW_NAME
   ├── Parallelism: $WINDOW_PARALLELISM
   ├── Records in: $WINDOW_IN
   └── Records out: $WINDOW_OUT

   $SINK_NAME
   ├── Parallelism: $SINK_PARALLELISM
   └── Records in: $SINK_IN

------------------------------------------
4. DISTRIBUCIÓN DE PODS
------------------------------------------
$POD_DISTRIBUTION

------------------------------------------
5. SCHEDULING
------------------------------------------
   Decisiones de scheduling: $SCHEDULING_DECISIONS
   Cambios de estrategia: $STRATEGY_SWITCHES

------------------------------------------
6. BACKPRESSURE
------------------------------------------
$(cat "$RESULTS_DIR/backpressure-summary.txt")

==========================================
Archivos generados en $RESULTS_DIR/:
  - job-details.json
  - taskmanager-placement.txt
  - node-metrics.txt
  - pod-metrics.txt
  - scheduler-logs.txt
  - backpressure-summary.txt
  - METRICS-SUMMARY.txt
==========================================
EOF

# Mostrar resumen
echo ""
echo ""
cat "$RESULTS_DIR/METRICS-SUMMARY.txt"

# Generar CSV para análisis posterior
cat > "$RESULTS_DIR/metrics.csv" << EOF
strategy,job_id,state,duration_ms,duration_sec,source_records,throughput,filter_in,filter_out,filter_efficiency,window_in,window_out,sink_in,total_tms,nodes_used,scheduling_decisions,strategy_switches
$STRATEGY,$JOB_ID,$STATE,$DURATION_MS,$DURATION_SEC,$SOURCE_RECORDS,$THROUGHPUT,$FILTER_IN,$FILTER_OUT,$FILTER_EFFICIENCY,$WINDOW_IN,$WINDOW_OUT,$SINK_IN,$TOTAL_TMS,$NODES_USED,$SCHEDULING_DECISIONS,$STRATEGY_SWITCHES
EOF

echo ""
echo "CSV generado: $RESULTS_DIR/metrics.csv"