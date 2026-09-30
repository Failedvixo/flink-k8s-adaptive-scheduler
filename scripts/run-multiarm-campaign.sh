#!/bin/bash
# ============================================
# N arms in ONE session, with the order rotated
# ============================================
#
# WHY (2026-09-17). The paired campaign compares exactly two arms, so three arms needed three
# campaigns — and campaigns live in different sessions. That is fatal to the comparison,
# because the cluster's ABSOLUTE throughput drifts between sessions: the identical LPT
# placement measured 31200 rec/s one night and 24414 another, a 22% swing driven by the host
# (WSL restarts, page cache, whatever else Windows is doing) and nothing to do with placement.
# Chaining results across sessions — "RL beats LPT, LPT beats STOCK, therefore RL beats
# STOCK" — is not defensible against a drift that size.
#
# What IS defensible is a comparison where every arm ran under the same conditions. Putting
# all the arms in one session gives every pairwise comparison at once, each one valid.
#
# ORDER IS ROTATED, not reversed: with three arms, reversing only swaps the ends. Each pass
# is a cyclic rotation (A B C, B C A, C A B), so over RUNS passes each arm occupies each
# position equally often — the same control the paired campaign gets from its two orders.
# RUNS must therefore be a multiple of the number of arms for the balance to be exact.
#
# The protocol stays CAPSys's: one job per arm per pass, 6-minute warm-up, 10 minutes of
# metrics, and the analysis counts JOBS (a job's repetitions share one placement).
#
# Usage:
#   ARMS="STOCK LPT RL" scripts/run-multiarm-campaign.sh              # 6 passes, ~5.5 h
#   RUNS=9 ARMS="STOCK LPT RL" scripts/run-multiarm-campaign.sh       # 9 jobs per arm
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ARMS="${ARMS:-STOCK LPT RL}"
RUNS="${RUNS:-6}"
export QUERY="${QUERY:-q8}"
# 40000 comes from the calibration of 2026-09-21 over the FIXED Q8 (the join emits again):
# the sink peaks at 24726 rec/s there and backpressure jumps 55 -> 384 ms/s, so the cluster is
# at its limit and a bad placement costs throughput. Below 32000 it absorbs everything linearly
# and the arms cannot be told apart; at 48000 it is past the cliff — output RETREATS to 20704,
# below what 32000 delivers. The old 38000 default predates the join fix and is not comparable.
export RATE="${RATE:-40000}"
# Two repetitions whenever RL is among the arms: see the note at the analysis call below.
case " $ARMS " in *" RL "*) export REPS="${REPS:-2}" ;; *) export REPS="${REPS:-1}" ;; esac
export SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
export SUBMIT_PAR="${SUBMIT_PAR:-3}"
export TARGET_PAR="${TARGET_PAR:-2}"
export MAX_EVENT_AGE="${MAX_EVENT_AGE:-5000}"
export SCHEDULE="${SCHEDULE:-3 2*}"
export WIDE_HOLD="${WIDE_HOLD:-120}"
export WARMUP="${WARMUP:-360}"
export WINDOW="${WINDOW:-600}"
export JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.ref.RefNexmarkJob}"
export TM_DEPLOYMENTS="${TM_DEPLOYMENTS:-flink-tm-fast:1 flink-tm-medium:1 flink-tm-slow:1}"
export DRAIN_DEPLOYMENTS="${DRAIN_DEPLOYMENTS:-flink-taskmanager:0}"
# CHARACTERISER pasa al driver, no se queda aquí (2026-09-21): sin esta línea la campaña
# corre el brazo RL sobre el plan que haya quedado en el nodo y nadie se entera.
export CHARACTERISER="${CHARACTERISER:-0}"
export AGENT_QTABLE="${AGENT_QTABLE:-}"
export AGENT_FREEZE="${AGENT_FREEZE:-}"
export PUBLISH_LOADS="${PUBLISH_LOADS:-0}"
export KEEP_FIRST=1
export ANALYSE_SLICES="${ANALYSE_SLICES:-$(( TARGET_PAR * ${PLAN_STAGES:-4} ))}"

set -- $ARMS
NARMS=$#
[ "$NARMS" -ge 2 ] || { echo "ERROR: hacen falta al menos dos brazos" >&2; exit 2; }
if [ $(( RUNS % NARMS )) -ne 0 ]; then
    echo "AVISO: RUNS=$RUNS no es múltiplo de $NARMS brazos; las posiciones quedarán" >&2
    echo "       desbalanceadas y el orden podría explicar parte del resultado." >&2
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT_DIR/results/multiarm-campaign/$STAMP"
mkdir -p "$OUT"

echo "=========================================="
echo "  Campaña multi-brazo — $QUERY a $RATE rec/s"
echo "  brazos: $ARMS   pasadas: $RUNS  ->  $(( RUNS )) jobs por brazo"
echo "  calentamiento ${WARMUP}s + ventana ${WINDOW}s, esquema '$SCHEDULE'"
echo "  -> $OUT"
# THE ESTIMATE IS MEASURED, NOT GUESSED (2026-09-21). The campaign of that day was quoted
# at "~7 h" from the holds alone and took 8.7: six passes of three arms, 87 min per pass,
# 29 per job. The holds account for only 19 of those 29 minutes. The other 10 are the
# fixed cost of a cell that nothing shortens — submitting the job and waiting for RUNNING,
# the two rescales (lib_wait_rescale returned in 50-120 s all night), restarting the three
# TaskManagers, and wiping checkpoints and MinIO's multipart leftovers before the arm.
PER_ARM=$(( WARMUP + WINDOW + ${STEP_MARGIN:-90} + WIDE_HOLD + 570 ))
# Each extra repetition is another wide hold, two rescales (~150 s each) and a measured step.
PER_ARM=$(( PER_ARM + (REPS - 1) * (WARMUP + WINDOW + ${STEP_MARGIN:-90} + WIDE_HOLD + 300) ))
TOTAL=$(( PER_ARM * NARMS * RUNS ))
echo "  duración estimada: $(( PER_ARM / 60 )) min por brazo x $NARMS brazos x $RUNS pasadas"
echo "                     = $(( TOTAL / 3600 ))h $(( (TOTAL % 3600) / 60 ))m  (termina cerca de las $(date -d "+$TOTAL seconds" +%H:%M 2>/dev/null || echo '?'))"
echo "=========================================="

DIRS=""
FAILED=""
for pass in $(seq 1 "$RUNS"); do
    # Cyclic rotation: pass k starts at arm k, wrapping around.
    ORDER=""
    for offset in $(seq 0 $(( NARMS - 1 ))); do
        index=$(( (pass - 1 + offset) % NARMS + 1 ))
        ORDER="$ORDER $(eval echo \${$index})"
    done
    ORDER="${ORDER# }"
    echo ""
    echo "################ pasada $pass / $RUNS: $ORDER  ($(date +%H:%M))"
    before=$(ls -td "$ROOT_DIR"/results/placement-experiment/*/ 2>/dev/null | head -1)
    ARMS="$ORDER" "$SCRIPT_DIR/run-placement-experiment.sh" 2>&1 | tee "$OUT/pass-$pass.log"
    produced=$(ls -td "$ROOT_DIR"/results/placement-experiment/*/ 2>/dev/null | head -1)
    if [ -z "$produced" ] || [ "$produced" = "$before" ]; then
        FAILED="$FAILED $pass"
        continue
    fi
    DIRS="$DIRS $produced"
    echo "$produced" >> "$OUT/pass-dirs.txt"
done

echo ""
echo "=========================================="
echo "  ANÁLISIS CONJUNTO"
echo "=========================================="
[ -n "$FAILED" ] && echo "  ! pasadas sin datos:$FAILED" | tee "$OUT/failed.txt"
if [ -z "$DIRS" ]; then
    echo "ERROR: ninguna pasada produjo datos" >&2
    exit 1
fi
# THE FIRST EPISODE OF A JOB IS DROPPED WHEN THERE IS MORE THAN ONE (2026-09-30). With REPS=1
# the RL arm was never evaluated: the agent publishes its plan AFTER observing a width, so the
# job's one measured rescale ran whatever plan the PREVIOUS job left (or LPT, when there was
# none) — the 2026-09-30 oracle campaign measured the previous pass's plan three times out of
# four. REPS=2 lets the agent observe in repetition 1 and be measured in repetition 2; every
# arm gets the same two repetitions so their jobs are equally old when measured.
KEEP_FIRST_FLAG=""
[ "$REPS" = "1" ] && KEEP_FIRST_FLAG="--keep-first"
python3 "$SCRIPT_DIR/analyse_paired_campaign.py" $KEEP_FIRST_FLAG \
    ${ANALYSE_SLICES:+--slices $ANALYSE_SLICES} $DIRS | tee "$OUT/summary.txt"
echo ""
echo "resultados -> $OUT"
