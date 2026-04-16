#!/bin/bash
# Biblioteca común para experimentos de scheduling.
# Genera METRICS-SUMMARY.txt completo con throughput honesto (eventos procesados
# al final del grafo, no solo emitidos por el source).

run_strategy_experiment() {
  local STRATEGY="$1"
  local ADAPTIVE_MODE="$2"
  local WORKLOAD="${3:-high-load}"
  local RATE="${4:-200000}"
  local DURATION="${5:-180}"
  local PARALLELISM="${6:-8}"
  local WINDOW="${7:-10}"
  local CPU_LOAD="${8:-0}"
  local ARRIVAL_DIST="${9:-CONSTANT}"

  local RESULTS_DIR="results/$WORKLOAD/$STRATEGY"
  mkdir -p "$RESULTS_DIR"

  echo "=========================================="
  echo "  EXPERIMENTO: $STRATEGY ($WORKLOAD)"
  echo "  Rate=$RATE ev/s  Duration=${DURATION}s  Parallelism=$PARALLELISM"
  echo "  CPULoad=$CPU_LOAD iter  ArrivalDist=$ARRIVAL_DIST"
  echo "=========================================="

  local CONTAINER_NAME
  CONTAINER_NAME=$(kubectl get deployment adaptive-scheduler -n kube-system \
    -o jsonpath='{.spec.template.spec.containers[0].name}')
  echo "Container: $CONTAINER_NAME"

  local TAG_SUFFIX
  TAG_SUFFIX=$(echo "$STRATEGY" | tr '[:upper:]' '[:lower:]')
  local TAG="${TAG_SUFFIX}-$(date +%s)"

  echo ""
  echo "[1/8] Compilando scheduler..."
  cd scheduler || return 1
  rm -rf target
  mvn clean package -DskipTests
  if [ $? -ne 0 ]; then
    echo "ERROR: Compilación falló"
    cd ..
    return 1
  fi

  local _check_dir
  _check_dir=$(mktemp -d)
  ( cd "$_check_dir" && \
    jar -xf "$OLDPWD/target/flink-adaptive-scheduler-1.0.0.jar" \
      com/thesis/scheduler/AdaptiveScheduler.class 2>/dev/null )
  if [ -f "$_check_dir/com/thesis/scheduler/AdaptiveScheduler.class" ]; then
    if ! strings "$_check_dir/com/thesis/scheduler/AdaptiveScheduler.class" \
         | grep -q "FIXED STRATEGY"; then
      echo "ERROR: AdaptiveScheduler.class no contiene soporte FIXED_STRATEGY"
      rm -rf "$_check_dir"
      cd ..
      return 1
    fi
    echo "  ✓ JAR OK"
  fi
  rm -rf "$_check_dir"

  echo ""
  echo "[2/8] Construyendo imagen Docker..."
  docker build -t "adaptive-scheduler:$TAG" . --no-cache
  if [ $? -ne 0 ]; then
    echo "ERROR: docker build falló"
    cd ..
    return 1
  fi

  echo ""
  echo "[3/8] Cargando imagen en Minikube..."
  minikube image load "adaptive-scheduler:$TAG" --daemon
  if ! minikube image ls | grep -q "adaptive-scheduler:$TAG"; then
    echo "ERROR: la imagen no aparece en minikube"
    cd ..
    return 1
  fi

  cd ..

  echo ""
  echo "[4/8] Desplegando scheduler..."
  kubectl set image deployment/adaptive-scheduler -n kube-system \
    "${CONTAINER_NAME}=adaptive-scheduler:${TAG}"
  if [ "$ADAPTIVE_MODE" = "true" ]; then
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" FIXED_STRATEGY-
  else
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" "FIXED_STRATEGY=${STRATEGY}"
  fi
  kubectl rollout restart deployment/adaptive-scheduler -n kube-system
  kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=120s
  sleep 10

  local POD_NAME
  POD_NAME=$(kubectl get pod -n kube-system -l app=adaptive-scheduler \
    -o jsonpath='{.items[0].metadata.name}')

  local expected_banner
  if [ "$ADAPTIVE_MODE" = "true" ]; then
    expected_banner="Mode: ADAPTIVE"
  else
    expected_banner="Mode: FIXED STRATEGY ($STRATEGY)"
  fi

  local found="false"
  for i in {1..30}; do
    if kubectl logs -n kube-system "$POD_NAME" 2>/dev/null \
         | grep -qF "$expected_banner"; then
      echo "  ✓ $expected_banner"
      found="true"
      break
    fi
    sleep 2
  done
  if [ "$found" != "true" ]; then
    echo "ERROR: scheduler no arrancó"
    kubectl logs -n kube-system "$POD_NAME" --tail=50
    return 1
  fi

  echo ""
  echo "[5/8] Preparando cluster..."
  kubectl exec -n flink deployment/flink-jobmanager -- flink list 2>/dev/null | \
    grep -oP '[0-9a-f]{32}' | \
    xargs -I {} kubectl exec -n flink deployment/flink-jobmanager -- flink cancel {} 2>/dev/null

  kubectl scale deployment flink-taskmanager -n flink --replicas=0
  kubectl wait --for=delete pod -l component=taskmanager -n flink --timeout=60s 2>/dev/null || true
  sleep 5

  kubectl scale deployment flink-taskmanager -n flink --replicas=5
  kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s

  echo "  Esperando slots en JobManager..."
  local EXPECTED_SLOTS=10
  for i in {1..60}; do
    local AVAILABLE_SLOTS
    AVAILABLE_SLOTS=$(kubectl exec -n flink deployment/flink-jobmanager -- \
      curl -s http://localhost:8081/overview 2>/dev/null | jq -r '.["slots-available"] // 0')
    if [ "$AVAILABLE_SLOTS" -ge "$EXPECTED_SLOTS" ]; then
      echo "  ✓ $AVAILABLE_SLOTS slots disponibles"
      break
    fi
    sleep 2
  done
  sleep 5

  echo ""
  echo "[6/8] Ejecutando job..."
  local JOB_OUTPUT
  JOB_OUTPUT=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    flink run -d /tmp/nexmark.jar "$RATE" "$DURATION" "$PARALLELISM" "$WINDOW" "$CPU_LOAD" "$ARRIVAL_DIST" 2>&1)
  local JOB_ID
  JOB_ID=$(echo "$JOB_OUTPUT" | grep -oP 'JobID \K[0-9a-f]{32}')

  if [ -z "$JOB_ID" ]; then
    echo "ERROR: No se pudo obtener Job ID"
    echo "$JOB_OUTPUT"
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" FIXED_STRATEGY- 2>/dev/null || true
    return 1
  fi
  echo "Job ID: $JOB_ID"
  sleep 30

  local JOB_STATUS
  JOB_STATUS=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$JOB_ID" | jq -r '.state' 2>/dev/null)
  echo "Job Status: $JOB_STATUS"

  echo ""
  echo "[7/8] Monitoreando job..."
  local num_samples=$(( DURATION / 30 ))
  [ "$num_samples" -lt 1 ] && num_samples=1
  for (( i=1; i<=num_samples; i++ )); do
    echo ""
    echo "=== Muestra $i/$num_samples ==="
    kubectl top nodes
    kubectl get pods -n flink -l component=taskmanager -o wide | awk 'NR>1 {print $1, $7}'
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
  kubectl logs -n kube-system "$POD_NAME" --tail=5000 > "$RESULTS_DIR/scheduler-logs.txt"
  kubectl get pods -n flink -l component=taskmanager -o wide > "$RESULTS_DIR/taskmanager-placement.txt"
  kubectl top nodes > "$RESULTS_DIR/node-metrics.txt" 2>&1
  kubectl top pods -n flink > "$RESULTS_DIR/pod-metrics.txt" 2>&1

  kubectl logs -n flink -l component=taskmanager --tail=5000 2>/dev/null \
    > "$RESULTS_DIR/taskmanager-full.log" || true
  grep "Source-" "$RESULTS_DIR/taskmanager-full.log" > "$RESULTS_DIR/source-stats.txt" 2>/dev/null || true
  grep "Latency-PROC" "$RESULTS_DIR/taskmanager-full.log" > "$RESULTS_DIR/latency-processing.txt" 2>/dev/null || true
  grep "Latency-TOTAL" "$RESULTS_DIR/taskmanager-full.log" > "$RESULTS_DIR/latency-total.txt" 2>/dev/null || true
  grep "Sink-" "$RESULTS_DIR/taskmanager-full.log" > "$RESULTS_DIR/sink-stats.txt" 2>/dev/null || true

  # ====== Extracción de métricas ======
  local DURATION_MS STATE START_TIME END_TIME JOB_NAME
  DURATION_MS=$(jq -r '.duration' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "0")
  STATE=$(jq -r '.state' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")
  START_TIME=$(jq -r '.["start-time"]' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "0")
  END_TIME=$(jq -r '.["end-time"]' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "0")
  JOB_NAME=$(jq -r '.name' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")

  local DURATION_SEC="N/A"
  if [ "$DURATION_MS" != "0" ] && [ "$DURATION_MS" != "null" ]; then
    DURATION_SEC=$(echo "scale=2; $DURATION_MS / 1000" | bc)
  fi

  # ========================================
  # THROUGHPUT POR ETAPA DEL GRAFO
  # ========================================
  # Identificamos los vertices por nombre (contiene substring característico)
  # para ser robustos ante el orden exacto del plan.

  local NUM_VERTICES
  NUM_VERTICES=$(jq -r '.vertices | length' "$RESULTS_DIR/job-details.json")

  # Inicializar todos en N/A
  local SOURCE_OUT=0 FILTER_OUT=0 CPU_LOAD_OUT=0 LATENCY_OUT=0 TRANSFORM_OUT=0 WINDOW_OUT=0 SINK_IN=0
  local SOURCE_NAME="" FILTER_NAME="" CPU_LOAD_NAME="" LATENCY_NAME="" TRANSFORM_NAME="" WINDOW_NAME="" SINK_NAME=""
  local SOURCE_PAR=0 FILTER_PAR=0 CPU_LOAD_PAR=0 LATENCY_PAR=0 TRANSFORM_PAR=0 WINDOW_PAR=0 SINK_PAR=0

  for ((v=0; v<NUM_VERTICES; v++)); do
    local vname vout vin vpar
    vname=$(jq -r ".vertices[$v].name" "$RESULTS_DIR/job-details.json")
    vout=$(jq -r ".vertices[$v].metrics[\"write-records\"] // 0" "$RESULTS_DIR/job-details.json")
    vin=$(jq -r ".vertices[$v].metrics[\"read-records\"] // 0" "$RESULTS_DIR/job-details.json")
    vpar=$(jq -r ".vertices[$v].parallelism" "$RESULTS_DIR/job-details.json")

    # Clasificar por nombre. El chaining de Flink puede unir operadores;
    # usamos la primera clasificación que haga match.
    if [[ "$vname" == *"Source: Bid Generator"* ]]; then
      SOURCE_NAME="$vname"; SOURCE_OUT="$vout"; SOURCE_PAR="$vpar"
    elif [[ "$vname" == *"CPU Load Simulator"* ]]; then
      CPU_LOAD_NAME="$vname"; CPU_LOAD_OUT="$vout"; CPU_LOAD_PAR="$vpar"
    elif [[ "$vname" == *"Latency Tracker"* ]]; then
      LATENCY_NAME="$vname"; LATENCY_OUT="$vout"; LATENCY_PAR="$vpar"
    elif [[ "$vname" == *"Filter"* ]]; then
      FILTER_NAME="$vname"; FILTER_OUT="$vout"; FILTER_PAR="$vpar"
    elif [[ "$vname" == *"Transform"* ]]; then
      TRANSFORM_NAME="$vname"; TRANSFORM_OUT="$vout"; TRANSFORM_PAR="$vpar"
    elif [[ "$vname" == *"Window"* ]]; then
      WINDOW_NAME="$vname"; WINDOW_OUT="$vout"; WINDOW_PAR="$vpar"
    elif [[ "$vname" == *"Sink"* ]]; then
      SINK_NAME="$vname"; SINK_IN="$vin"; SINK_PAR="$vpar"
    fi
  done

  # ---- Buscar "processed events" ----
  # Prioridad para el throughput "honesto":
  # 1. Latency Tracker (si existe chained): justo después del CPU-load, antes del window
  # 2. Transform: USD to EUR: también está después del CPU-load
  # 3. CPU Load Simulator: si no hay transform, este es el último procesamiento pesado
  # 4. Filter: fallback
  # 5. Source: último fallback

  local PROCESSED_EVENTS=0
  local PROCESSED_STAGE=""
  if [ "$LATENCY_OUT" -gt 0 ] 2>/dev/null; then
    PROCESSED_EVENTS=$LATENCY_OUT
    PROCESSED_STAGE="Latency Tracker (post CPU-load)"
  elif [ "$TRANSFORM_OUT" -gt 0 ] 2>/dev/null; then
    PROCESSED_EVENTS=$TRANSFORM_OUT
    PROCESSED_STAGE="Transform (post CPU-load)"
  elif [ "$CPU_LOAD_OUT" -gt 0 ] 2>/dev/null; then
    PROCESSED_EVENTS=$CPU_LOAD_OUT
    PROCESSED_STAGE="CPU Load Simulator"
  elif [ "$FILTER_OUT" -gt 0 ] 2>/dev/null; then
    PROCESSED_EVENTS=$FILTER_OUT
    PROCESSED_STAGE="Filter"
  else
    PROCESSED_EVENTS=$SOURCE_OUT
    PROCESSED_STAGE="Source (fallback)"
  fi

  # Throughput honesto = eventos procesados / duración
  local THROUGHPUT_PROCESSED="N/A"
  if [ "$DURATION_MS" != "0" ] && [ "$DURATION_MS" != "null" ] && [ "$PROCESSED_EVENTS" -gt 0 ]; then
    THROUGHPUT_PROCESSED=$(echo "scale=0; $PROCESSED_EVENTS * 1000 / $DURATION_MS" | bc)
  fi

  # Throughput del source (compatibilidad)
  local THROUGHPUT_SOURCE="N/A"
  if [ "$DURATION_MS" != "0" ] && [ "$DURATION_MS" != "null" ] && [ "$SOURCE_OUT" -gt 0 ]; then
    THROUGHPUT_SOURCE=$(echo "scale=0; $SOURCE_OUT * 1000 / $DURATION_MS" | bc)
  fi

  # Backpressure por vertex
  local BACKPRESSURE_INFO=""
  for ((v=0; v<NUM_VERTICES; v++)); do
    local VNAME VBP VIDLE VPAR VIN VOUT
    VNAME=$(jq -r ".vertices[$v].name" "$RESULTS_DIR/job-details.json")
    VBP=$(jq -r ".vertices[$v].metrics[\"accumulated-backpressured-time\"] // 0" "$RESULTS_DIR/job-details.json")
    VIDLE=$(jq -r ".vertices[$v].metrics[\"accumulated-idle-time\"] // 0" "$RESULTS_DIR/job-details.json")
    VPAR=$(jq -r ".vertices[$v].parallelism" "$RESULTS_DIR/job-details.json")
    VIN=$(jq -r ".vertices[$v].metrics[\"read-records\"] // 0" "$RESULTS_DIR/job-details.json")
    VOUT=$(jq -r ".vertices[$v].metrics[\"write-records\"] // 0" "$RESULTS_DIR/job-details.json")
    BACKPRESSURE_INFO+="
   ${VNAME} (par=${VPAR}):
     Records in:        ${VIN}
     Records out:       ${VOUT}
     Backpressure time: ${VBP}ms
     Idle time:         ${VIDLE}ms"
  done

  # Distribución de pods
  local POD_DISTRIBUTION TOTAL_TMS NODES_USED
  POD_DISTRIBUTION=$(awk 'NR>1 {print $7}' "$RESULTS_DIR/taskmanager-placement.txt" | sort | uniq -c \
    | awk '{printf "    %s: %d TaskManagers\n", $2, $1}')
  TOTAL_TMS=$(kubectl get pods -n flink -l component=taskmanager --no-headers 2>/dev/null | wc -l)
  NODES_USED=$(awk 'NR>1 {print $7}' "$RESULTS_DIR/taskmanager-placement.txt" | sort -u | wc -l)

  # Scheduling stats
  local SCHEDULING_DECISIONS STRATEGY_SWITCHES
  SCHEDULING_DECISIONS=$(grep -c "\[SCHEDULING\]" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")
  STRATEGY_SWITCHES=$(grep -c "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")

  # Source stats: generado, emitido, dropped
  local TOTAL_GENERATED=0 TOTAL_DROPPED=0 TOTAL_EMITTED=0 DROP_PCT="N/A"
  if [ -f "$RESULTS_DIR/source-stats.txt" ] && [ -s "$RESULTS_DIR/source-stats.txt" ]; then
    while IFS= read -r line; do
      local g d e
      g=$(echo "$line" | grep -oP 'generated=\K[0-9,]+' | tr -d ',')
      d=$(echo "$line" | grep -oP 'dropped=\K[0-9,]+' | tr -d ',')
      e=$(echo "$line" | grep -oP 'emitted=\K[0-9,]+' | tr -d ',')
      [ -n "$g" ] && TOTAL_GENERATED=$(( TOTAL_GENERATED + g ))
      [ -n "$d" ] && TOTAL_DROPPED=$(( TOTAL_DROPPED + d ))
      [ -n "$e" ] && TOTAL_EMITTED=$(( TOTAL_EMITTED + e ))
    done < <(grep "FINALIZADO" "$RESULTS_DIR/source-stats.txt")
    if [ "$TOTAL_GENERATED" -gt 0 ]; then
      DROP_PCT=$(echo "scale=2; $TOTAL_DROPPED * 100 / $TOTAL_GENERATED" | bc)
    fi
  fi

  # Throughput generado y emitido (desde source logs, no desde Flink)
  local THROUGHPUT_GENERATED="N/A" THROUGHPUT_EMITTED="N/A"
  if [ "$DURATION_MS" != "0" ] && [ "$DURATION_MS" != "null" ] && [ "$TOTAL_GENERATED" -gt 0 ]; then
    THROUGHPUT_GENERATED=$(echo "scale=0; $TOTAL_GENERATED * 1000 / $DURATION_MS" | bc)
    THROUGHPUT_EMITTED=$(echo "scale=0; $TOTAL_EMITTED * 1000 / $DURATION_MS" | bc)
  fi

  # Latencia: percentiles promediados entre subtasks
  local LAT_PROC_AVG_P50 LAT_PROC_AVG_P95 LAT_PROC_AVG_P99 LAT_PROC_AVG_AVG
  if [ -f "$RESULTS_DIR/latency-processing.txt" ] && grep -q "FINAL" "$RESULTS_DIR/latency-processing.txt"; then
    LAT_PROC_AVG_P50=$(grep "FINAL" "$RESULTS_DIR/latency-processing.txt" | grep -oP 'p50=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_PROC_AVG_P95=$(grep "FINAL" "$RESULTS_DIR/latency-processing.txt" | grep -oP 'p95=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_PROC_AVG_P99=$(grep "FINAL" "$RESULTS_DIR/latency-processing.txt" | grep -oP 'p99=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_PROC_AVG_AVG=$(grep "FINAL" "$RESULTS_DIR/latency-processing.txt" | grep -oP 'avg=\K[0-9.]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.1f", s/n; else print "N/A"}')
  else
    LAT_PROC_AVG_P50="N/A"; LAT_PROC_AVG_P95="N/A"; LAT_PROC_AVG_P99="N/A"; LAT_PROC_AVG_AVG="N/A"
  fi

  local LAT_TOTAL_AVG_P50 LAT_TOTAL_AVG_P95 LAT_TOTAL_AVG_P99 LAT_TOTAL_AVG_AVG
  if [ -f "$RESULTS_DIR/latency-total.txt" ] && grep -q "FINAL" "$RESULTS_DIR/latency-total.txt"; then
    LAT_TOTAL_AVG_P50=$(grep "FINAL" "$RESULTS_DIR/latency-total.txt" | grep -oP 'p50=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_TOTAL_AVG_P95=$(grep "FINAL" "$RESULTS_DIR/latency-total.txt" | grep -oP 'p95=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_TOTAL_AVG_P99=$(grep "FINAL" "$RESULTS_DIR/latency-total.txt" | grep -oP 'p99=\K[0-9]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.0f", s/n; else print "N/A"}')
    LAT_TOTAL_AVG_AVG=$(grep "FINAL" "$RESULTS_DIR/latency-total.txt" | grep -oP 'avg=\K[0-9.]+' | awk '{s+=$1;n++} END {if(n>0) printf "%.1f", s/n; else print "N/A"}')
  else
    LAT_TOTAL_AVG_P50="N/A"; LAT_TOTAL_AVG_P95="N/A"; LAT_TOTAL_AVG_P99="N/A"; LAT_TOTAL_AVG_AVG="N/A"
  fi

  # ====== Generar METRICS-SUMMARY.txt ======
  cat > "$RESULTS_DIR/METRICS-SUMMARY.txt" << SUMMARY
==========================================
  MÉTRICAS COMPLETAS - $STRATEGY
==========================================
Fecha: $(date)
Job ID: $JOB_ID
Job Name: $JOB_NAME

CONFIGURACIÓN:
- Workload:                $WORKLOAD
- Estrategia:              $STRATEGY
- Modo:                    $([ "$ADAPTIVE_MODE" = "true" ] && echo "ADAPTIVE" || echo "FIXED_STRATEGY=$STRATEGY")
- Eventos/seg (base):      $RATE
- Distribución llegada:    $ARRIVAL_DIST
- Duración configurada:    ${DURATION}s
- Paralelismo global:      $PARALLELISM
- Ventana:                 ${WINDOW}s
- CPU load por evento:     $CPU_LOAD iter
- TaskManagers:            $TOTAL_TMS
- Nodos utilizados:        $NODES_USED
- Imagen scheduler:        adaptive-scheduler:$TAG

------------------------------------------
1. ESTADO DEL JOB
------------------------------------------
   Estado:        $STATE
   Duración:      $DURATION_MS ms ($DURATION_SEC s)
   Inicio:        $START_TIME
   Fin:           $END_TIME

------------------------------------------
2. THROUGHPUT
------------------------------------------
   TRABAJO ÚTIL PROCESADO (métrica principal):
     Stage de medición: $PROCESSED_STAGE
     Eventos procesados: $PROCESSED_EVENTS
     Throughput:         $THROUGHPUT_PROCESSED ev/s

   THROUGHPUT POR ETAPA (records out / duration):
     Source emitted:     $(echo "scale=0; $SOURCE_OUT * 1000 / $DURATION_MS" | bc 2>/dev/null || echo "N/A") ev/s   ($SOURCE_OUT records)
     Filter out:         $([ "$FILTER_OUT" -gt 0 ] && echo "scale=0; $FILTER_OUT * 1000 / $DURATION_MS" | bc || echo "N/A") ev/s   ($FILTER_OUT records)
     CPU Load out:       $([ "$CPU_LOAD_OUT" -gt 0 ] && echo "scale=0; $CPU_LOAD_OUT * 1000 / $DURATION_MS" | bc || echo "chained") ev/s   ($CPU_LOAD_OUT records)
     Latency Tracker:    $([ "$LATENCY_OUT" -gt 0 ] && echo "scale=0; $LATENCY_OUT * 1000 / $DURATION_MS" | bc || echo "chained") ev/s   ($LATENCY_OUT records)
     Transform out:      $([ "$TRANSFORM_OUT" -gt 0 ] && echo "scale=0; $TRANSFORM_OUT * 1000 / $DURATION_MS" | bc || echo "chained") ev/s   ($TRANSFORM_OUT records)
     Window out:         $([ "$WINDOW_OUT" -gt 0 ] && echo "scale=0; $WINDOW_OUT * 1000 / $DURATION_MS" | bc || echo "N/A") ev/s   ($WINDOW_OUT records, aggregated)
     Sink in:            $([ "$SINK_IN" -gt 0 ] && echo "scale=0; $SINK_IN * 1000 / $DURATION_MS" | bc || echo "N/A") ev/s   ($SINK_IN records)

   Nota: "chained" indica que Flink fusionó el operador con otro vertex,
   por lo que sus records están reflejados en el siguiente stage.

------------------------------------------
3. SOURCE STATS (desde logs del TaskManager)
------------------------------------------
   Total generado:    $TOTAL_GENERATED
   Total emitido:     $TOTAL_EMITTED
   Total descartado:  $TOTAL_DROPPED
   Drop %:            ${DROP_PCT}%

   Throughput generado: $THROUGHPUT_GENERATED ev/s
   Throughput emitido:  $THROUGHPUT_EMITTED ev/s (entra al grafo)
   Throughput procesado: $THROUGHPUT_PROCESSED ev/s (sale del CPU-load) ← MÉTRICA PRINCIPAL

------------------------------------------
4. LATENCIA END-TO-END
------------------------------------------
   PROCESSING (event-time -> fin de CPU-load):
     p50: ${LAT_PROC_AVG_P50} ms
     p95: ${LAT_PROC_AVG_P95} ms
     p99: ${LAT_PROC_AVG_P99} ms
     avg: ${LAT_PROC_AVG_AVG} ms

   TOTAL (event-time -> sink, incluye delay del window):
     p50: ${LAT_TOTAL_AVG_P50} ms
     p95: ${LAT_TOTAL_AVG_P95} ms
     p99: ${LAT_TOTAL_AVG_P99} ms
     avg: ${LAT_TOTAL_AVG_AVG} ms

   Nota: la latencia TOTAL incluye necesariamente hasta ${WINDOW}s
   adicionales por el delay del Tumbling Window.

------------------------------------------
5. MÉTRICAS POR OPERADOR$BACKPRESSURE_INFO

------------------------------------------
6. DISTRIBUCIÓN DE PODS
------------------------------------------
$POD_DISTRIBUTION

------------------------------------------
7. SCHEDULING
------------------------------------------
   Decisiones de scheduling: $SCHEDULING_DECISIONS
   Cambios de estrategia:    $STRATEGY_SWITCHES

   Switches detectados:
$(grep "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" | tail -10 || echo "   Ninguno")

------------------------------------------
8. RECURSOS DEL CLUSTER (final)
------------------------------------------
$(cat "$RESULTS_DIR/node-metrics.txt")

==========================================
Archivos generados en $RESULTS_DIR/:
  - job-details.json         (raw JSON del job)
  - taskmanager-placement.txt (pods por nodo)
  - taskmanager-full.log     (logs completos del TM)
  - source-stats.txt         (gen/emit/drop por subtask)
  - latency-processing.txt   (latencia post CPU-load)
  - latency-total.txt        (latencia event-time -> sink)
  - scheduler-logs.txt       (decisiones del scheduler)
  - node-metrics.txt         (CPU/memoria por nodo)
  - METRICS-SUMMARY.txt      (este archivo)
  - metrics.csv              (fila lista para análisis comparativo)
==========================================
SUMMARY

  # CSV con throughput honesto como métrica principal
  cat > "$RESULTS_DIR/metrics.csv" << CSV
strategy,workload,distribution,rate,duration_sec,cpu_load,parallelism,state,throughput_processed,throughput_generated,throughput_emitted,processed_events,total_generated,total_dropped,drop_pct,nodes_used,total_tms,scheduling_decisions,strategy_switches,lat_proc_p50,lat_proc_p95,lat_proc_p99,lat_proc_avg,lat_total_p50,lat_total_p95,lat_total_p99,lat_total_avg
$STRATEGY,$WORKLOAD,$ARRIVAL_DIST,$RATE,$DURATION_SEC,$CPU_LOAD,$PARALLELISM,$STATE,$THROUGHPUT_PROCESSED,$THROUGHPUT_GENERATED,$THROUGHPUT_EMITTED,$PROCESSED_EVENTS,$TOTAL_GENERATED,$TOTAL_DROPPED,$DROP_PCT,$NODES_USED,$TOTAL_TMS,$SCHEDULING_DECISIONS,$STRATEGY_SWITCHES,$LAT_PROC_AVG_P50,$LAT_PROC_AVG_P95,$LAT_PROC_AVG_P99,$LAT_PROC_AVG_AVG,$LAT_TOTAL_AVG_P50,$LAT_TOTAL_AVG_P95,$LAT_TOTAL_AVG_P99,$LAT_TOTAL_AVG_AVG
CSV

  echo ""
  echo "=========================================="
  echo "  EXPERIMENTO $STRATEGY COMPLETADO"
  echo "=========================================="
  echo ""
  cat "$RESULTS_DIR/METRICS-SUMMARY.txt"
  echo ""
  echo "Resultados en: $RESULTS_DIR/"
  echo "CSV: $RESULTS_DIR/metrics.csv"

  if [ "$ADAPTIVE_MODE" != "true" ]; then
    echo ""
    echo "Limpiando FIXED_STRATEGY..."
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" FIXED_STRATEGY- 2>/dev/null || true
  fi

  return 0
}