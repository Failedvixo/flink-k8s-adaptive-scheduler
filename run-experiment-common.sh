#!/bin/bash
# Biblioteca común para los scripts de experimentos de scheduling.
# No se ejecuta directamente — se invoca desde experiment-<strategy>.sh
# llamando a la función run_strategy_experiment.

run_strategy_experiment() {
  local STRATEGY="$1"          # FCFS | BALANCED | LEAST_LOADED | BANDIT | SARSA | ADAPTIVE
  local ADAPTIVE_MODE="$2"     # "true" si es adaptive (no setea FIXED_STRATEGY)
  local WORKLOAD="${3:-high-load}"  # nombre del workload (high-load, heavy, etc.)
  local RATE="${4:-200000}"    # eventos por segundo
  local DURATION="${5:-180}"   # segundos
  local PARALLELISM="${6:-8}"  # paralelismo del job
  local WINDOW="${7:-10}"      # tamaño de ventana en segundos

  local RESULTS_DIR="results/$WORKLOAD/$STRATEGY"
  mkdir -p "$RESULTS_DIR"

  echo "=========================================="
  echo "  EXPERIMENTO: $STRATEGY ($WORKLOAD)"
  echo "  Rate=$RATE ev/s  Duration=${DURATION}s  Parallelism=$PARALLELISM"
  echo "=========================================="

  # Detectar nombre real del container en el deployment
  local CONTAINER_NAME
  CONTAINER_NAME=$(kubectl get deployment adaptive-scheduler -n kube-system \
    -o jsonpath='{.spec.template.spec.containers[0].name}')
  echo "Container detectado en deployment: $CONTAINER_NAME"

  # Tag único por corrida
  local TAG_SUFFIX
  TAG_SUFFIX=$(echo "$STRATEGY" | tr '[:upper:]' '[:lower:]')
  local TAG="${TAG_SUFFIX}-$(date +%s)"
  echo "Tag único de imagen: $TAG"

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

  # Verificar el .class dentro del uber-jar
  local _check_dir
  _check_dir=$(mktemp -d)
  ( cd "$_check_dir" && \
    jar -xf "$OLDPWD/target/flink-adaptive-scheduler-1.0.0.jar" \
      com/thesis/scheduler/AdaptiveScheduler.class 2>/dev/null )

  if [ -f "$_check_dir/com/thesis/scheduler/AdaptiveScheduler.class" ]; then
    if ! strings "$_check_dir/com/thesis/scheduler/AdaptiveScheduler.class" \
         | grep -q "FIXED STRATEGY"; then
      echo "ERROR: AdaptiveScheduler.class no contiene soporte FIXED_STRATEGY"
      echo "Revisa que no haya archivos AdaptiveScheduler.java.backup o .ORIGINAL"
      rm -rf "$_check_dir"
      cd ..
      return 1
    fi
    echo "  ✓ Verificación del JAR OK (contiene FIXED_STRATEGY)"
  else
    echo "  WARNING: no se pudo extraer AdaptiveScheduler.class, saltando verificación"
  fi
  rm -rf "$_check_dir"

  echo ""
  echo "[2/8] Construyendo imagen Docker (sin caché)..."
  docker build -t "adaptive-scheduler:$TAG" . --no-cache
  if [ $? -ne 0 ]; then
    echo "ERROR: docker build falló"
    cd ..
    return 1
  fi

  if ! docker images --format '{{.Repository}}:{{.Tag}}' \
       | grep -q "^adaptive-scheduler:$TAG$"; then
    echo "ERROR: la imagen adaptive-scheduler:$TAG no existe en el Docker daemon"
    cd ..
    return 1
  fi
  echo "  ✓ Imagen construida: adaptive-scheduler:$TAG"

  echo ""
  echo "[3/8] Cargando imagen en Minikube..."
  minikube image load "adaptive-scheduler:$TAG" --daemon

  if ! minikube image ls | grep -q "adaptive-scheduler:$TAG"; then
    echo "ERROR: la imagen no aparece en minikube image ls después del load"
    cd ..
    return 1
  fi
  echo "  ✓ Imagen cargada en Minikube"

  cd ..

  echo ""
  echo "[4/8] Desplegando scheduler (STRATEGY=$STRATEGY, adaptive=$ADAPTIVE_MODE)..."

  # Paso 1: cambiar la imagen (set image hace merge, preserva todo lo demás)
  kubectl set image deployment/adaptive-scheduler -n kube-system \
    "${CONTAINER_NAME}=adaptive-scheduler:${TAG}"

  # Paso 2: setear o borrar FIXED_STRATEGY (set env también hace merge correcto)
  if [ "$ADAPTIVE_MODE" = "true" ]; then
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" FIXED_STRATEGY-
  else
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" "FIXED_STRATEGY=${STRATEGY}"
  fi

  # Paso 3: forzar un rollout explícito para consolidar ambos cambios
  # en un solo arranque de pod
  kubectl rollout restart deployment/adaptive-scheduler -n kube-system
  kubectl rollout status deployment/adaptive-scheduler -n kube-system --timeout=120s
  sleep 10

  # Validación crítica: obtener el pod activo DESPUÉS del rollout
  local POD_NAME
  POD_NAME=$(kubectl get pod -n kube-system -l app=adaptive-scheduler \
    -o jsonpath='{.items[0].metadata.name}')
  echo ""
  echo "Pod activo: $POD_NAME"

  # Validación 1: FIXED_STRATEGY en el container
  if [ "$ADAPTIVE_MODE" = "true" ]; then
    local adaptive_env
    adaptive_env=$(kubectl exec -n kube-system "$POD_NAME" -c "${CONTAINER_NAME}" \
      -- printenv FIXED_STRATEGY 2>/dev/null || echo "")
    if [ -n "$adaptive_env" ]; then
      echo "ERROR: modo ADAPTIVE pero FIXED_STRATEGY=$adaptive_env (debería estar vacío)"
      return 1
    fi
    echo "  ✓ Modo ADAPTIVE (FIXED_STRATEGY no está seteado)"
  else
    local env_check
    env_check=$(kubectl exec -n kube-system "$POD_NAME" -c "${CONTAINER_NAME}" \
      -- printenv FIXED_STRATEGY 2>/dev/null || echo "")
    if [[ "$env_check" != "$STRATEGY" ]]; then
      echo "ERROR: FIXED_STRATEGY=$env_check (esperado: $STRATEGY)"
      kubectl logs -n kube-system "$POD_NAME" --tail=30
      return 1
    fi
    echo "  ✓ FIXED_STRATEGY=$env_check"
  fi

  # Validación 2: el ConfigMap env sigue presente
  local cpu_low
  cpu_low=$(kubectl exec -n kube-system "$POD_NAME" -c "${CONTAINER_NAME}" \
    -- printenv CPU_LOW_THRESHOLD 2>/dev/null || echo "")
  if [ -z "$cpu_low" ]; then
    echo "ERROR: CPU_LOW_THRESHOLD del ConfigMap no está presente"
    echo "  El set env/set image borró las variables del ConfigMap."
    echo "  Restaura con: kubectl apply -f kubernetes/scheduler-manifests.yaml"
    return 1
  fi
  echo "  ✓ ConfigMap env preservado (CPU_LOW_THRESHOLD=$cpu_low)"

  # Validación 3: banner en logs con el modo correcto
  echo ""
  echo "Esperando banner del scheduler..."
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
      echo "  ✓ Scheduler activo con: $expected_banner"
      found="true"
      break
    fi
    echo "  ... esperando ($i/30)"
    sleep 2
  done

  if [ "$found" != "true" ]; then
    echo "ERROR: el scheduler no arrancó con '$expected_banner'"
    kubectl logs -n kube-system "$POD_NAME" --tail=50
    return 1
  fi

  echo ""
  echo "[5/8] Preparando cluster..."

  # Cancelar jobs previos
  kubectl exec -n flink deployment/flink-jobmanager -- flink list 2>/dev/null | \
    grep -oP '[0-9a-f]{32}' | \
    xargs -I {} kubectl exec -n flink deployment/flink-jobmanager -- flink cancel {} 2>/dev/null

  # Borrar TaskManagers antiguos explícitamente
  kubectl scale deployment flink-taskmanager -n flink --replicas=0
  echo "  Esperando que los TaskManagers antiguos se eliminen..."
  kubectl wait --for=delete pod -l component=taskmanager -n flink --timeout=60s 2>/dev/null || true
  sleep 5

  kubectl scale deployment flink-taskmanager -n flink --replicas=5
  kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s
  sleep 10

  echo ""
  echo "Confirmando decisiones iniciales:"
  kubectl logs -n kube-system "$POD_NAME" --tail=150 \
    | grep -E "\[SCHEDULING\]|Strategy:" | head -20

  echo ""
  echo "[6/8] Ejecutando job Nexmark ($RATE evt/s, paralelismo $PARALLELISM, ${DURATION}s)..."

  local JOB_OUTPUT
  JOB_OUTPUT=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    flink run -d /tmp/nexmark.jar "$RATE" "$DURATION" "$PARALLELISM" "$WINDOW" 2>&1)

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
  echo "[7/8] Monitoreando job (muestras cada 30s durante ${DURATION}s)..."

  local num_samples=$(( DURATION / 30 ))
  [ "$num_samples" -lt 1 ] && num_samples=1

  for (( i=1; i<=num_samples; i++ )); do
    echo ""
    echo "=== Muestra $i/$num_samples (t+$((i*30))s) ==="
    kubectl top nodes
    echo ""
    kubectl get pods -n flink -l component=taskmanager -o wide | awk '{print $1, $7}'
    echo ""
    kubectl logs -n kube-system "$POD_NAME" --tail=5 \
      | grep -iE "strategy|scheduling" || true
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

  kubectl logs -n kube-system "$POD_NAME" --tail=5000 \
    > "$RESULTS_DIR/scheduler-logs.txt"
  kubectl get pods -n flink -l component=taskmanager -o wide \
    > "$RESULTS_DIR/taskmanager-placement.txt"
  kubectl top nodes > "$RESULTS_DIR/node-metrics.txt" 2>&1
  kubectl top pods -n flink > "$RESULTS_DIR/pod-metrics.txt" 2>&1

  local DURATION STATE SWITCHES DECISIONS
  DURATION=$(jq -r '.duration' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")
  STATE=$(jq -r '.state' "$RESULTS_DIR/job-details.json" 2>/dev/null || echo "N/A")
  SWITCHES=$(grep -c "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")
  DECISIONS=$(grep -c "\[SCHEDULING\]" "$RESULTS_DIR/scheduler-logs.txt" 2>/dev/null || echo "0")

  cat > "$RESULTS_DIR/SUMMARY.txt" << SUMMARY
========================================
EXPERIMENTO: $STRATEGY ($WORKLOAD)
========================================

Configuración:
- Workload: $WORKLOAD
- Eventos/seg: $RATE
- Duración: $DURATION segundos
- Paralelismo: $PARALLELISM
- Ventana: ${WINDOW}s
- TaskManagers: 5
- Imagen: adaptive-scheduler:$TAG
- Modo: $([ "$ADAPTIVE_MODE" = "true" ] && echo "ADAPTIVE" || echo "FIXED_STRATEGY=$STRATEGY")

Resultados:
- Job ID: $JOB_ID
- Estado final: $STATE
- Duración: $DURATION ms

Distribución de TaskManagers:
$(awk 'NR>1 {print $7}' "$RESULTS_DIR/taskmanager-placement.txt" | sort | uniq -c)

Scheduling decisions: $DECISIONS
Strategy switches:    $SWITCHES

Switches detectados:
$(grep "STRATEGY SWITCH" "$RESULTS_DIR/scheduler-logs.txt" | tail -10 || echo "Ninguno")

========================================
SUMMARY

  echo ""
  echo "=========================================="
  echo "  EXPERIMENTO $STRATEGY COMPLETADO"
  echo "=========================================="
  echo ""
  cat "$RESULTS_DIR/SUMMARY.txt"
  echo ""
  echo "Resultados detallados en: $RESULTS_DIR/"

  # Limpieza: quitar variable de entorno al final
  if [ "$ADAPTIVE_MODE" != "true" ]; then
    echo ""
    echo "Limpiando FIXED_STRATEGY del deployment..."
    kubectl set env deployment/adaptive-scheduler -n kube-system \
      --containers="${CONTAINER_NAME}" FIXED_STRATEGY- 2>/dev/null || true
  fi

  return 0
}