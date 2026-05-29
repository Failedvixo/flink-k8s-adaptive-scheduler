#!/bin/bash
#
# auto-scaler.sh — Monitor de elasticidad via Flink Adaptive Scheduler
#
# Usa PUT /jobs/{id}/resource-requirements para ajustar el RANGO de
# paralelismo del vertex CPU-Load. Flink decide el paralelismo real
# dentro de [lowerBound, upperBound] según los slots disponibles.
#
# Cuando busy% sube → subimos upperBound → Flink puede usar más subtasks
# Cuando busy% baja → bajamos upperBound → Flink libera subtasks
#
# Requiere: jobmanager.scheduler: adaptive

set -u

# ============================================================
# CONFIGURACIÓN (desde env o defaults)
# ============================================================

RATE="${RATE:-100000}"
DURATION="${DURATION:-180}"
PARALLELISM="${PARALLELISM:-8}"
WINDOW="${WINDOW:-10}"
CPU_LOAD="${CPU_LOAD:-2500}"
ARRIVAL_DIST="${ARRIVAL_DIST:-CONSTANT}"
INITIAL_CPU_PAR="${INITIAL_CPU_PAR:-4}"
MAX_EVENT_AGE="${MAX_EVENT_AGE:-2000}"

SCALE_UP_THRESH="${SCALE_UP_THRESH:-70}"
SCALE_DOWN_THRESH="${SCALE_DOWN_THRESH:-20}"
SCALE_STEP="${SCALE_STEP_SIZE:-2}"
MIN_PAR="${MIN_CPU_PARALLELISM:-2}"
MAX_PAR="${MAX_CPU_PARALLELISM:-10}"
POLL_INT="${POLL_INTERVAL_SEC:-10}"
COOLDOWN="${COOLDOWN_SEC:-30}"
# Substring used to identify the "hot" vertex whose parallelism we rescale.
# Default = ConfigurableGraphJob's CPU-load operator. Override per query, e.g.
#   HEAVY_VERTEX_PATTERN="hot-items-count"  (Q5)
#   HEAVY_VERTEX_PATTERN="new-users-join"   (Q8)
HEAVY_VERTEX_PATTERN="${HEAVY_VERTEX_PATTERN:-CPU Load Simulator}"

# Si INITIAL_CPU_PAR=0 (viene de CPU_LOAD_PAR=0 = "usar global"), usar MIN_PAR
if [ "${INITIAL_CPU_PAR}" -le 0 ] 2>/dev/null || [ "${INITIAL_CPU_PAR}" -lt "${MIN_PAR}" ] 2>/dev/null; then
  INITIAL_CPU_PAR=$MIN_PAR
fi

RESULTS_DIR="${RESULTS_DIR:-.}"
LOG_FILE="$RESULTS_DIR/autoscaler.log"
EVENTS_LOG="$RESULTS_DIR/scale-events.log"

# ============================================================
# ESTADO
# ============================================================

CURRENT_UPPER=$INITIAL_CPU_PAR
JOB_ID=""
CPU_LOAD_VERTEX_ID=""
LAST_SCALE_TIME=0
START_TIME=$(date +%s)
PREV_ACCUM_MS="-1"   # -1 = sin muestra previa
PREV_SAMPLE_TS=0

> "$LOG_FILE"
> "$EVENTS_LOG"

# ============================================================
# FUNCIONES
# ============================================================

log() {
  local msg="[$(date '+%H:%M:%S')] $1"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

get_running_job() {
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s http://localhost:8081/jobs/overview 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
running = [j for j in data.get('jobs', []) if j['state'] == 'RUNNING']
if running:
    print(running[0]['jid'])
" 2>/dev/null
}

get_cpuload_vertex_id() {
  local jid="$1"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
for v in data.get('vertices', []):
    if '${HEAVY_VERTEX_PATTERN}' in v.get('name', ''):
        print(v['id'])
        break
" 2>/dev/null
}

get_cpuload_busy_pct() {
  local jid="$1"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
dur = max(data.get('duration', 1), 1)
for v in data.get('vertices', []):
    if '${HEAVY_VERTEX_PATTERN}' in v.get('name', ''):
        par = v.get('parallelism', 1)
        busy = v['metrics'].get('accumulated-busy-time', 0)
        if busy == 'NaN' or busy is None: busy = 0
        else: busy = float(busy)
        pct = (busy / (dur * par)) * 100.0 if dur > 0 and par > 0 else 0
        print(f'{pct:.1f}')
        break
" 2>/dev/null
}

get_cpuload_accum_ms() {
  local jid="$1"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
for v in data.get('vertices', []):
    if '${HEAVY_VERTEX_PATTERN}' in v.get('name', ''):
        par = v.get('parallelism', 1)
        busy = v['metrics'].get('accumulated-busy-time', 0)
        if busy == 'NaN' or busy is None: busy = 0
        else: busy = float(busy)
        print(f'{busy:.3f} {par}')
        break
" 2>/dev/null
}

get_cpuload_current_par() {
  local jid="$1"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
for v in data.get('vertices', []):
    if '${HEAVY_VERTEX_PATTERN}' in v.get('name', ''):
        print(v.get('parallelism', 0))
        break
" 2>/dev/null
}

get_job_state() {
  local jid="$1"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null
}

# ============================================================
# RESCALE: ajusta [lowerBound(fijo), upperBound(dinámico)]
# ============================================================

update_resource_requirements() {
  local jid="$1"
  local vertex_id="$2"
  local new_upper="$3"
  local direction="$4"

  local old_upper=$CURRENT_UPPER

  log ""
  log "=========================================="
  log "  RESCALE $direction: upperBound $old_upper → $new_upper"
  log "  Range: [${MIN_PAR}, ${new_upper}]"
  log "  Vertex: $vertex_id"
  log "=========================================="

  # Flink requiere incluir TODOS los vertices en el payload.
  # Para los demás vertices se fija lowerBound=upperBound=parallelism_actual.
  local job_json
  job_json=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$jid" 2>/dev/null)

  local payload
  payload=$(echo "$job_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
reqs = {}
for v in data.get('vertices', []):
    vid = v['id']
    par = max(v.get('parallelism', 1), 1)
    if vid == '${vertex_id}':
        reqs[vid] = {'parallelism': {'lowerBound': ${MIN_PAR}, 'upperBound': ${new_upper}}}
    else:
        reqs[vid] = {'parallelism': {'lowerBound': par, 'upperBound': par}}
print(json.dumps(reqs))
" 2>/dev/null)

  log "PUT /jobs/$jid/resource-requirements"
  log "Payload: $payload"

  local response
  response=$(kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s -w "\n%{http_code}" -X PUT \
    "http://localhost:8081/jobs/$jid/resource-requirements" \
    -H "Content-Type: application/json" \
    -d "$payload" 2>/dev/null)

  local http_code
  http_code=$(echo "$response" | tail -1)
  local body
  body=$(echo "$response" | head -n -1)

  if [ "$http_code" = "200" ]; then
    log "✓ Resource requirements accepted (HTTP 200)"
    CURRENT_UPPER=$new_upper
    LAST_SCALE_TIME=$(date +%s)
    local elapsed=$(( LAST_SCALE_TIME - START_TIME ))
    echo "$(date '+%H:%M:%S') t=${elapsed}s $direction range=[${MIN_PAR},${new_upper}]" >> "$EVENTS_LOG"

    sleep 15
    local actual_par
    actual_par=$(get_cpuload_current_par "$jid")
    if [ -n "$actual_par" ]; then
      log "Flink decided parallelism: $actual_par within [${MIN_PAR}, ${new_upper}]"
    fi
    return 0
  else
    log "ERROR: Rescale failed (HTTP $http_code)"
    log "Body: $body"
    return 1
  fi
}

# ============================================================
# MAIN
# ============================================================

log "=========================================="
log "  AUTO-SCALER (Adaptive Scheduler REST API)"
log "=========================================="
log "Config:"
log "  Rate:              $RATE ev/s"
log "  Duration:          ${DURATION}s"
log "  Global par:        $PARALLELISM"
log "  Initial upperBound:$INITIAL_CPU_PAR"
log "  lowerBound (fixed):$MIN_PAR"
log "  CPU load:          $CPU_LOAD iter/event"
log "  Max event age:     ${MAX_EVENT_AGE}ms"
log "  Arrival dist:      $ARRIVAL_DIST"
log "  Heavy vertex:      pattern='${HEAVY_VERTEX_PATTERN}'"
log "  Scale up at:       >${SCALE_UP_THRESH}% busy → raise upperBound"
log "  Scale down at:     <${SCALE_DOWN_THRESH}% busy → lower upperBound"
log "  Scale step:        ±$SCALE_STEP"
log "  Range limits:      [$MIN_PAR, $MAX_PAR]"
log "  Poll interval:     ${POLL_INT}s"
log "  Cooldown:          ${COOLDOWN}s"
log "=========================================="
log ""

# Submit initial job
log "Submitting job: cpuLoadParallelism=$INITIAL_CPU_PAR"
_CLS_ARG=""
if [ -n "${JOB_CLASS:-}" ]; then _CLS_ARG="-c ${JOB_CLASS}"; fi
# shellcheck disable=SC2086
JOB_OUTPUT=$(kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d $_CLS_ARG /tmp/nexmark.jar \
  "$RATE" "$DURATION" "$PARALLELISM" "$WINDOW" "$CPU_LOAD" "$ARRIVAL_DIST" "$INITIAL_CPU_PAR" "$MAX_EVENT_AGE" ${EXTRA_JOB_ARGS:-} 2>&1)

JOB_ID=$(echo "$JOB_OUTPUT" | grep -oP 'JobID \K[0-9a-f]{32}')
if [ -z "$JOB_ID" ]; then
  log "FATAL: Could not get Job ID"
  log "$JOB_OUTPUT"
  exit 1
fi
log "Job ID: $JOB_ID"
echo "$JOB_ID" > "$RESULTS_DIR/job-id.txt"

log "Waiting for RUNNING..."
for i in {1..30}; do
  sleep 2
  STATE=$(get_job_state "$JOB_ID")
  if [ "$STATE" = "RUNNING" ]; then
    log "Job RUNNING"
    break
  fi
done

# Flink can flip STATE=RUNNING before /jobs/{id} exposes every vertex,
# so poll until the heavy vertex actually appears (up to ~60s).
CPU_LOAD_VERTEX_ID=""
for i in {1..30}; do
  CPU_LOAD_VERTEX_ID=$(get_cpuload_vertex_id "$JOB_ID")
  [ -n "$CPU_LOAD_VERTEX_ID" ] && break
  sleep 2
done
if [ -z "$CPU_LOAD_VERTEX_ID" ]; then
  log "FATAL: heavy vertex (pattern='${HEAVY_VERTEX_PATTERN}') not found after 60s"
  kubectl exec -n flink deployment/flink-jobmanager -- \
    curl -s "http://localhost:8081/jobs/$JOB_ID" 2>/dev/null | python3 -c "
import sys, json
for v in json.load(sys.stdin).get('vertices', []):
    print(f'  {v[\"id\"]}: {v[\"name\"]} (par={v[\"parallelism\"]})')
" 2>/dev/null
  exit 1
fi
log "CPU Load vertex ID: $CPU_LOAD_VERTEX_ID"

log "Setting initial range: [${MIN_PAR}, ${INITIAL_CPU_PAR}]"
update_resource_requirements "$JOB_ID" "$CPU_LOAD_VERTEX_ID" "$INITIAL_CPU_PAR" "INIT"

log "Waiting 20s for metrics..."
sleep 20

# ============================================================
# MONITORING LOOP
# ============================================================

while true; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TIME ))

  if [ $ELAPSED -ge $(( DURATION + 60 )) ]; then
    log "Duration exceeded. Stopping."
    break
  fi

  RUNNING_JOB=$(get_running_job)
  if [ -z "$RUNNING_JOB" ]; then
    STATE=$(get_job_state "$JOB_ID" 2>/dev/null)
    if [ "$STATE" = "FINISHED" ] || [ "$STATE" = "FAILED" ] || [ "$STATE" = "CANCELED" ]; then
      log "Job $STATE. Stopping."
      break
    fi
    log "Job not RUNNING (state=$STATE). Waiting..."
    sleep "$POLL_INT"
    continue
  fi

  if [ "$RUNNING_JOB" != "$JOB_ID" ]; then
    log "Job ID changed: $JOB_ID → $RUNNING_JOB"
    JOB_ID="$RUNNING_JOB"
    CPU_LOAD_VERTEX_ID=$(get_cpuload_vertex_id "$JOB_ID")
    log "New vertex ID: $CPU_LOAD_VERTEX_ID"
  fi

  RAW=$(get_cpuload_accum_ms "$JOB_ID")
  if [ -z "$RAW" ]; then
    sleep "$POLL_INT"
    continue
  fi
  ACCUM_MS=$(echo "$RAW" | awk '{print $1}')
  ACTUAL_PAR=$(echo "$RAW" | awk '{print $2}')

  # Busy% instantáneo: delta accumulated_ms entre polls (no promedio histórico)
  if [ "$PREV_ACCUM_MS" != "-1" ] && [ "$PREV_SAMPLE_TS" -gt 0 ]; then
    DELTA_BUSY=$(echo "$ACCUM_MS - $PREV_ACCUM_MS" | bc 2>/dev/null)
    # Clamp a 0 si Flink reseteó métricas tras un rescale
    DELTA_BUSY=$(echo "if ($DELTA_BUSY < 0) 0 else $DELTA_BUSY" | bc 2>/dev/null)
    DELTA_WINDOW=$(echo "($NOW - $PREV_SAMPLE_TS) * 1000 * $ACTUAL_PAR" | bc 2>/dev/null)
    if [ -n "$DELTA_WINDOW" ] && [ "$DELTA_WINDOW" -gt 0 ] 2>/dev/null; then
      BUSY=$(echo "scale=1; $DELTA_BUSY * 100 / $DELTA_WINDOW" | bc 2>/dev/null)
      [ -z "$BUSY" ] && BUSY="0.0"
    else
      BUSY="0.0"
    fi
  else
    BUSY="0.0"
  fi
  PREV_ACCUM_MS="$ACCUM_MS"
  PREV_SAMPLE_TS=$NOW

  log "busy_inst=${BUSY}% actualPar=${ACTUAL_PAR} upperBound=${CURRENT_UPPER} range=[${MIN_PAR},${CURRENT_UPPER}] elapsed=${ELAPSED}s"
  kubectl top nodes 2>/dev/null | while IFS= read -r line; do log "  $line"; done

  TIME_SINCE_SCALE=$(( NOW - LAST_SCALE_TIME ))
  if [ $TIME_SINCE_SCALE -lt $COOLDOWN ]; then
    log "  (cooldown: ${TIME_SINCE_SCALE}/${COOLDOWN}s)"
    sleep "$POLL_INT"
    continue
  fi

  BUSY_INT=$(echo "$BUSY" | cut -d'.' -f1)

  if [ "$BUSY_INT" -gt "$SCALE_UP_THRESH" ] && [ "$CURRENT_UPPER" -lt "$MAX_PAR" ]; then
    NEW_UPPER=$(( CURRENT_UPPER + SCALE_STEP ))
    [ $NEW_UPPER -gt $MAX_PAR ] && NEW_UPPER=$MAX_PAR
    update_resource_requirements "$JOB_ID" "$CPU_LOAD_VERTEX_ID" $NEW_UPPER "UP"

  elif [ "$BUSY_INT" -lt "$SCALE_DOWN_THRESH" ] && [ "$CURRENT_UPPER" -gt "$MIN_PAR" ]; then
    NEW_UPPER=$(( CURRENT_UPPER - SCALE_STEP ))
    [ $NEW_UPPER -lt $MIN_PAR ] && NEW_UPPER=$MIN_PAR
    update_resource_requirements "$JOB_ID" "$CPU_LOAD_VERTEX_ID" $NEW_UPPER "DOWN"
  fi

  sleep "$POLL_INT"
done

# ============================================================
# SUMMARY
# ============================================================

NUM_EVENTS=$(wc -l < "$EVENTS_LOG" 2>/dev/null || echo "0")
FINAL_PAR=$(get_cpuload_current_par "$JOB_ID" 2>/dev/null || echo "?")
log ""
log "=========================================="
log "  AUTO-SCALER FINISHED"
log "=========================================="
log "Scale events:       $NUM_EVENTS"
log "Upper bound final:  $CURRENT_UPPER"
log "Parallelism final:  $FINAL_PAR (decided by Flink)"
log "Lower bound (fixed):$MIN_PAR"
if [ -s "$EVENTS_LOG" ]; then
  log ""
  log "History:"
  while IFS= read -r line; do
    log "  $line"
  done < "$EVENTS_LOG"
fi
log "=========================================="