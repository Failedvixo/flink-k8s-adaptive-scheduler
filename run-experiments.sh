#!/bin/bash
#
# Orquestador de experimentos completos.
#
# Matriz: 6 estrategias × 3 distribuciones = 18 experimentos.
# Configuración base: 200k ev/s, par=8, cpuLoad=1000, 5 TMs, 180s.
#
# Cada experimento genera su propio METRICS-SUMMARY.txt + metrics.csv en
# results/<workload>/<strategy>/
#
# Al final:
#   - Consolida todos los metrics.csv en results/all-metrics.csv
#   - Genera un COMPARISON.md por distribución
#   - Genera un COMPARISON-GLOBAL.md con matriz cruzada

set -u

source "$(dirname "$0")/run-experiment-common.sh"

# ============================================================
# CONFIGURACIÓN GLOBAL
# ============================================================

RATE=200000           # eventos/segundo base
DURATION=180          # segundos por experimento
PARALLELISM=8
WINDOW=10             # segundos de ventana tumbling
CPU_LOAD=1000         # iteraciones de trabajo sintético por evento

# Estrategias. Formato: "STRATEGY:ADAPTIVE_MODE"
# ADAPTIVE_MODE=true solo para ADAPTIVE. Las demás usan FIXED_STRATEGY.
STRATEGIES=(
  "FCFS:false"
  "BALANCED:false"
  "LEAST_LOADED:false"
  "BANDIT:false"
  "SARSA:false"
  "ADAPTIVE:true"
)

# Distribuciones de llegada.
DISTRIBUTIONS=(
  "constant"      # CONSTANT — tasa uniforme (baseline)
  "step"          # STEP     — baja/alta/baja en fases 33/33/33
  "sine"          # SINE     — oscilación sinusoidal
)

PAUSE_BETWEEN=30      # pausa entre experimentos (cooldown)

# ============================================================
# AUXILIARES
# ============================================================

workload_to_dist() {
  case "$1" in
    "constant") echo "CONSTANT" ;;
    "step")     echo "STEP" ;;
    "sine")     echo "SINE" ;;
    *)          echo "CONSTANT" ;;
  esac
}

TOTAL=$(( ${#STRATEGIES[@]} * ${#DISTRIBUTIONS[@]} ))
COUNT=0
FAILED=()
SUCCEEDED=()

# ============================================================
# BANNER
# ============================================================

mkdir -p results

echo "=========================================="
echo "  RUN-EXPERIMENTS — MATRIZ COMPLETA"
echo "=========================================="
echo "Configuración base:"
echo "  Rate:         $RATE ev/s"
echo "  Duration:     ${DURATION}s"
echo "  Parallelism:  $PARALLELISM"
echo "  Window:       ${WINDOW}s"
echo "  CPU load:     $CPU_LOAD iter/event"
echo "  TaskManagers: 5"
echo ""
echo "Estrategias (${#STRATEGIES[@]}):"
for s in "${STRATEGIES[@]}"; do
  name="${s%%:*}"; mode="${s##*:}"
  echo "  - $name $([ "$mode" = "true" ] && echo "(adaptive mode)")"
done
echo ""
echo "Distribuciones (${#DISTRIBUTIONS[@]}):"
for d in "${DISTRIBUTIONS[@]}"; do
  echo "  - $d ($(workload_to_dist "$d"))"
done
echo ""
echo "Total de experimentos: $TOTAL"
EST_MIN=$(( TOTAL * (DURATION + 240 + PAUSE_BETWEEN) / 60 ))
echo "Tiempo estimado: ~${EST_MIN} min ($(( EST_MIN / 60 ))h $(( EST_MIN % 60 ))min)"
echo "=========================================="
echo ""
echo "Comenzando en 10 segundos... (Ctrl+C para abortar)"
sleep 10

START_TIME=$(date +%s)

# ============================================================
# LOOP PRINCIPAL
# ============================================================

for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
  DIST=$(workload_to_dist "$WORKLOAD")

  echo ""
  echo "############################################"
  echo "# DISTRIBUCIÓN: $WORKLOAD (dist=$DIST)"
  echo "############################################"

  for entry in "${STRATEGIES[@]}"; do
    STRATEGY="${entry%%:*}"
    ADAPTIVE_MODE="${entry##*:}"

    COUNT=$(( COUNT + 1 ))
    EXP_ID="[$COUNT/$TOTAL] $STRATEGY @ $WORKLOAD"

    # ETA basado en promedio de experimentos previos
    NOW=$(date +%s)
    ELAPSED_SO_FAR=$(( NOW - START_TIME ))
    if [ $COUNT -gt 1 ]; then
      AVG_PER_EXP=$(( ELAPSED_SO_FAR / (COUNT - 1) ))
      ETA=$(( AVG_PER_EXP * (TOTAL - COUNT + 1) ))
      ETA_MIN=$(( ETA / 60 ))
      ETA_STR="(ETA ~${ETA_MIN} min restantes)"
    else
      ETA_STR=""
    fi

    echo ""
    echo "============================================"
    echo "  $EXP_ID"
    echo "  Iniciando: $(date '+%Y-%m-%d %H:%M:%S')  $ETA_STR"
    echo "============================================"

    if run_strategy_experiment \
         "$STRATEGY" \
         "$ADAPTIVE_MODE" \
         "$WORKLOAD" \
         "$RATE" \
         "$DURATION" \
         "$PARALLELISM" \
         "$WINDOW" \
         "$CPU_LOAD" \
         "$DIST"; then
      SUCCEEDED+=("$STRATEGY/$WORKLOAD")
      echo ""
      echo "  ✓ $EXP_ID OK"
    else
      FAILED+=("$STRATEGY/$WORKLOAD")
      echo ""
      echo "  ✗ $EXP_ID FALLÓ (continuando...)"
    fi

    if [ $COUNT -lt $TOTAL ]; then
      echo ""
      echo "Pausa de ${PAUSE_BETWEEN}s antes del siguiente experimento..."
      sleep "$PAUSE_BETWEEN"
    fi
  done
done

END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))
ELAPSED_MIN=$(( ELAPSED / 60 ))

echo ""
echo ""
echo "=========================================="
echo "  MATRIZ COMPLETA TERMINADA"
echo "=========================================="
echo "Tiempo total: ${ELAPSED_MIN} min ($(( ELAPSED_MIN / 60 ))h $(( ELAPSED_MIN % 60 ))min)"
echo "Exitosos:     ${#SUCCEEDED[@]}/$TOTAL"
echo "Fallidos:     ${#FAILED[@]}/$TOTAL"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo ""
  echo "Fallidos:"
  for f in "${FAILED[@]}"; do
    echo "  - $f"
  done
fi
echo ""

# ============================================================
# CONSOLIDAR METRICS.CSV
# ============================================================

ALL_CSV="results/all-metrics.csv"
echo "Consolidando resultados en $ALL_CSV ..."

FIRST_CSV=$(find results -name "metrics.csv" -type f 2>/dev/null | head -1)

if [ -n "$FIRST_CSV" ] && [ -f "$FIRST_CSV" ]; then
  head -1 "$FIRST_CSV" > "$ALL_CSV"
  find results -name "metrics.csv" -type f 2>/dev/null | sort | while read -r csv; do
    tail -n +2 "$csv" >> "$ALL_CSV"
  done
  TOTAL_LINES=$(( $(wc -l < "$ALL_CSV") - 1 ))
  echo "  ✓ $ALL_CSV generado ($TOTAL_LINES experimentos)"
else
  echo "  ⚠ No se encontraron metrics.csv para consolidar"
fi

# ============================================================
# COMPARISON.md POR DISTRIBUCIÓN
# ============================================================

for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
  COMP_FILE="results/$WORKLOAD/COMPARISON.md"
  [ -d "results/$WORKLOAD" ] || continue

  DIST=$(workload_to_dist "$WORKLOAD")

  cat > "$COMP_FILE" << MDHEADER
# Comparación de Estrategias — Distribución: $WORKLOAD ($DIST)

## Configuración
| Parámetro | Valor |
|-----------|-------|
| Rate base | $RATE ev/s |
| Duración | ${DURATION}s |
| Paralelismo | $PARALLELISM |
| Ventana | ${WINDOW}s |
| CPU load | $CPU_LOAD iter/evento |
| TaskManagers | 5 |

## Resultados

| Estrategia | Estado | Throughput procesado | Eventos procesados | Drop % | Nodos | Lat p50 | Lat p95 | Lat p99 |
|------------|--------|---------------------:|-------------------:|-------:|------:|--------:|--------:|--------:|
MDHEADER

  for entry in "${STRATEGIES[@]}"; do
    STRATEGY="${entry%%:*}"
    CSV="results/$WORKLOAD/$STRATEGY/metrics.csv"
    if [ -f "$CSV" ]; then
      # Columnas del CSV (index 1-based):
      #  1 strategy       2 workload       3 distribution    4 rate
      #  5 duration_sec   6 cpu_load       7 parallelism     8 state
      #  9 throughput_processed  10 throughput_generated    11 throughput_emitted
      # 12 processed_events      13 total_generated          14 total_dropped
      # 15 drop_pct     16 nodes_used      17 total_tms
      # 18 scheduling_decisions  19 strategy_switches
      # 20 lat_proc_p50  21 lat_proc_p95  22 lat_proc_p99  23 lat_proc_avg
      # 24 lat_total_p50 25 lat_total_p95 26 lat_total_p99 27 lat_total_avg
      ROW=$(tail -n 1 "$CSV")
      STATE=$(echo "$ROW" | cut -d',' -f8)
      TP=$(echo "$ROW" | cut -d',' -f9)
      PROC_EV=$(echo "$ROW" | cut -d',' -f12)
      DROP=$(echo "$ROW" | cut -d',' -f15)
      NODES=$(echo "$ROW" | cut -d',' -f16)
      P50=$(echo "$ROW" | cut -d',' -f20)
      P95=$(echo "$ROW" | cut -d',' -f21)
      P99=$(echo "$ROW" | cut -d',' -f22)
      echo "| $STRATEGY | $STATE | ${TP} ev/s | ${PROC_EV} | ${DROP}% | ${NODES} | ${P50} ms | ${P95} ms | ${P99} ms |" >> "$COMP_FILE"
    else
      echo "| $STRATEGY | FAILED | - | - | - | - | - | - | - |" >> "$COMP_FILE"
    fi
  done

  cat >> "$COMP_FILE" << MDFOOTER

## Notas

- **Throughput procesado**: eventos que salieron del Latency Tracker (post CPU-Load).
  Es el trabajo útil real — excluye eventos descartados en la cola del source.
- **Eventos procesados**: total absoluto de eventos que completaron el pipeline.
- **Drop %**: % de eventos generados que no entraron al grafo (cola saturada).
- **Latencia**: p50/p95/p99 del procesamiento (event-time → fin de CPU-load),
  promediada entre subtasks. No incluye delay del window tumbling.

Datos detallados: \`results/$WORKLOAD/<STRATEGY>/METRICS-SUMMARY.txt\`.

MDFOOTER

  echo "  ✓ $COMP_FILE generado"
done

# ============================================================
# COMPARISON-GLOBAL.md (matriz cruzada)
# ============================================================

GLOBAL_FILE="results/COMPARISON-GLOBAL.md"

# Construir headers dinámicos
DIST_HEADER=""
DIST_SEP=""
for d in "${DISTRIBUTIONS[@]}"; do
  DIST_HEADER+=" $d |"
  DIST_SEP+="---:|"
done

cat > "$GLOBAL_FILE" << GHEADER
# Comparación Global — Todas las Distribuciones

## Configuración común
| Parámetro | Valor |
|-----------|-------|
| Rate base | $RATE ev/s |
| Duración | ${DURATION}s |
| Paralelismo | $PARALLELISM |
| Ventana | ${WINDOW}s |
| CPU load | $CPU_LOAD iter/evento |
| TaskManagers | 5 |

## Matriz: Throughput procesado (ev/s)

| Estrategia |$DIST_HEADER
|------------|$DIST_SEP
GHEADER

for entry in "${STRATEGIES[@]}"; do
  STRATEGY="${entry%%:*}"
  ROW_STR="| $STRATEGY |"
  for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
    CSV="results/$WORKLOAD/$STRATEGY/metrics.csv"
    if [ -f "$CSV" ]; then
      TP=$(tail -n 1 "$CSV" | cut -d',' -f9)
      ROW_STR+=" ${TP} |"
    else
      ROW_STR+=" - |"
    fi
  done
  echo "$ROW_STR" >> "$GLOBAL_FILE"
done

cat >> "$GLOBAL_FILE" << GMID2

## Matriz: Drop % (eventos descartados por saturación)

| Estrategia |$DIST_HEADER
|------------|$DIST_SEP
GMID2

for entry in "${STRATEGIES[@]}"; do
  STRATEGY="${entry%%:*}"
  ROW_STR="| $STRATEGY |"
  for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
    CSV="results/$WORKLOAD/$STRATEGY/metrics.csv"
    if [ -f "$CSV" ]; then
      DROP=$(tail -n 1 "$CSV" | cut -d',' -f15)
      ROW_STR+=" ${DROP}% |"
    else
      ROW_STR+=" - |"
    fi
  done
  echo "$ROW_STR" >> "$GLOBAL_FILE"
done

cat >> "$GLOBAL_FILE" << GMID3

## Matriz: Latencia p95 (ms)

| Estrategia |$DIST_HEADER
|------------|$DIST_SEP
GMID3

for entry in "${STRATEGIES[@]}"; do
  STRATEGY="${entry%%:*}"
  ROW_STR="| $STRATEGY |"
  for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
    CSV="results/$WORKLOAD/$STRATEGY/metrics.csv"
    if [ -f "$CSV" ]; then
      P95=$(tail -n 1 "$CSV" | cut -d',' -f21)
      ROW_STR+=" ${P95} |"
    else
      ROW_STR+=" - |"
    fi
  done
  echo "$ROW_STR" >> "$GLOBAL_FILE"
done

cat >> "$GLOBAL_FILE" << GFOOTER

## Matriz: Latencia p99 (ms)

| Estrategia |$DIST_HEADER
|------------|$DIST_SEP
GFOOTER

for entry in "${STRATEGIES[@]}"; do
  STRATEGY="${entry%%:*}"
  ROW_STR="| $STRATEGY |"
  for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
    CSV="results/$WORKLOAD/$STRATEGY/metrics.csv"
    if [ -f "$CSV" ]; then
      P99=$(tail -n 1 "$CSV" | cut -d',' -f22)
      ROW_STR+=" ${P99} |"
    else
      ROW_STR+=" - |"
    fi
  done
  echo "$ROW_STR" >> "$GLOBAL_FILE"
done

cat >> "$GLOBAL_FILE" << GLOBALEND

## Lectura recomendada

1. **Comparar dentro de cada distribución** (ver \`results/<dist>/COMPARISON.md\`)
   para ver qué estrategia gana en cada régimen.
2. **Comparar una misma estrategia entre distribuciones** (filas de arriba)
   para ver la robustez de cada estrategia ante variabilidad de carga.
3. **Relación drop% vs throughput procesado**: la estrategia ideal maximiza
   throughput procesado minimizando drop%. Si ambos son altos, el pipeline
   está saturado y la estrategia elige bien qué eventos descartar. Si
   throughput procesado es bajo y drop% es alto, la estrategia está
   distribuyendo mal y saturando un nodo.

Datos raw: \`results/all-metrics.csv\`.

GLOBALEND

echo "  ✓ $GLOBAL_FILE generado"

# ============================================================
# RESUMEN FINAL + PREVIEW
# ============================================================

echo ""
echo "=========================================="
echo "  ARCHIVOS GENERADOS"
echo "=========================================="
echo "  - results/all-metrics.csv"
echo "  - results/COMPARISON-GLOBAL.md"
for WORKLOAD in "${DISTRIBUTIONS[@]}"; do
  [ -f "results/$WORKLOAD/COMPARISON.md" ] && \
    echo "  - results/$WORKLOAD/COMPARISON.md"
done
echo ""
echo "Resultados detallados: results/<workload>/<strategy>/"
echo ""
echo "=========================================="
echo "  PREVIEW: COMPARISON-GLOBAL.md"
echo "=========================================="
cat "$GLOBAL_FILE"