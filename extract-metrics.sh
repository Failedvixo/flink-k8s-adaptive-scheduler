#!/bin/bash

STRATEGY=$1

if [ -z "$STRATEGY" ]; then
  echo "Uso: ./extract-metrics.sh <FCFS|BALANCED|LEAST_LOADED|ADAPTIVE>"
  exit 1
fi

RESULTS_DIR="results/high-load/$STRATEGY"

if [ ! -f "$RESULTS_DIR/job-details.json" ]; then
  echo "ERROR: No existe $RESULTS_DIR/job-details.json"
  exit 1
fi

echo "=========================================="
echo "  MÉTRICAS DETALLADAS: $STRATEGY"
echo "=========================================="

# Duración total
DURATION_MS=$(jq -r '.duration' "$RESULTS_DIR/job-details.json")
DURATION_SEC=$(echo "scale=2; $DURATION_MS / 1000" | bc)

echo ""
echo "=== DURACIÓN ==="
echo "Total: $DURATION_MS ms ($DURATION_SEC segundos)"

# Extraer métricas de cada vertex (operador)
echo ""
echo "=== THROUGHPUT POR OPERADOR ==="

jq -r '.vertices[] | 
  "\(.name):" + 
  "\n  Records Received: \(."metrics"."read-records" // 0)" +
  "\n  Records Sent: \(."metrics"."write-records" // 0)" +
  "\n  Bytes Received: \(."metrics"."read-bytes" // 0)" +
  "\n  Bytes Sent: \(."metrics"."write-bytes" // 0)" +
  "\n  Parallelism: \(.parallelism)" +
  "\n"' "$RESULTS_DIR/job-details.json"

# Calcular throughput general
echo ""
echo "=== THROUGHPUT GENERAL ==="

# Records del source (primer vertex)
TOTAL_RECORDS=$(jq -r '.vertices[0]."metrics"."write-records" // 0' "$RESULTS_DIR/job-details.json")

if [ "$TOTAL_RECORDS" != "0" ] && [ "$TOTAL_RECORDS" != "null" ]; then
  THROUGHPUT=$(echo "scale=0; $TOTAL_RECORDS * 1000 / $DURATION_MS" | bc)
  echo "Total de eventos procesados: $TOTAL_RECORDS"
  echo "Throughput promedio: $THROUGHPUT eventos/segundo"
else
  echo "No se pudo calcular (datos no disponibles)"
fi

# Latencia end-to-end (aproximada por timestamps)
echo ""
echo "=== LATENCIA END-TO-END ==="
echo "Duración del job: $DURATION_SEC segundos"
echo "Latencia promedio por evento: $(echo "scale=3; $DURATION_MS / $TOTAL_RECORDS" | bc) ms"

# Backpressure (si está disponible)
echo ""
echo "=== BACKPRESSURE ==="
jq -r '.vertices[] | 
  "\(.name): \(.metrics.backpressure // "N/A")"' "$RESULTS_DIR/job-details.json"

# Generar resumen de métricas
cat > "$RESULTS_DIR/METRICS.txt" << METRICS
========================================
MÉTRICAS COMPLETAS - $STRATEGY
========================================

CONFIGURACIÓN:
- Workload: Nexmark Benchmark
- Tasa objetivo: 200,000 eventos/segundo
- Duración configurada: 180 segundos
- Paralelismo: 8
- TaskManagers: 5

RESULTADOS:

1. DURACIÓN:
   - Total: $DURATION_MS ms
   - Segundos: $DURATION_SEC s

2. THROUGHPUT:
   - Eventos procesados: $TOTAL_RECORDS
   - Throughput: $THROUGHPUT eventos/segundo
   - Tasa objetivo: 200,000 evt/s
   - Eficiencia: $(echo "scale=1; $THROUGHPUT * 100 / 200000" | bc)%

3. LATENCIA:
   - End-to-end: $DURATION_SEC segundos
   - Por evento: $(echo "scale=3; $DURATION_MS / $TOTAL_RECORDS" | bc) ms

4. DISTRIBUCIÓN:
$(cat "$RESULTS_DIR/taskmanager-placement.txt" | awk 'NR>1 {print $7}' | sort | uniq -c)

5. OBSERVACIONES:
$(if [ $(cat "$RESULTS_DIR/taskmanager-placement.txt" | awk 'NR>1 {print $7}' | sort -u | wc -l) -eq 1 ]; then
  echo "   ⚠️  TODOS los TaskManagers en el mismo nodo (desbalanceado)"
elif [ $(cat "$RESULTS_DIR/taskmanager-placement.txt" | awk 'NR>1 {print $7}' | sort -u | wc -l) -eq 3 ]; then
  echo "   ✅ TaskManagers distribuidos en múltiples nodos"
else
  echo "   ⚠️  Distribución parcial entre nodos"
fi)

========================================
METRICS

cat "$RESULTS_DIR/METRICS.txt"
