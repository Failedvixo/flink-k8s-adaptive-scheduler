#!/bin/bash
# ============================================
# One evaluation session per query: every condition and arm of the final design, unattended
# ============================================
#
# WHY (2026-10-21). The design agreed with the professor — five CAPSys queries, constant and
# sinusoidal load, with and without rescaling, 5 repetitions — in TWO PHASES:
#
#   phase 1, SELECTION (every query):
#   G  constant, 6 + 10 min, parallelism 2: the v4 trained on EACH of the five queries (V4_<Qn>,
#      this query's own included) + the untrained one (V4_PRIOR)                         x5
#   -> scripts/select_v4.py picks ONE table for all queries: lowest mean rank over 5 queries x
#      {delivery, p50 latency, sources' backpressure}; the rule is fixed before any result.
#
#   phase 2, EVALUATION (every query, with the selected table, on fresh runs):
#   A  constant, 6 + 10 min           STOCK V4 STOCK_BANDIT BANDIT                         x5
#   V  sine of 20 min, 6 min + one whole period, no rescaling   STOCK V4 BANDIT           x5
#   R  same sine, WITH the common proactive autoscaler (1-3 subtasks of the heavy operator),
#      placement coordinated for V4 and the bandit       STOCK V4 BANDIT V4_PRIOR_BANDIT  x5
#
# Rates: where the best placement at parallelism 2 keeps up and a bad one does not
# (scripts/run-plan-calibrations.sh). The 20-min period: detecting, rescaling and settling take
# ~3-5 min, so a 10-min sine left no room for rescaling to pay (simulator: every arm worse).
#
# RESUMABLE: a finished block leaves a marker in results/sessions/<query>/ and is skipped on a
# relaunch. report_session.py writes the query's report.md at the end.
#
# Usage:
#   BLOCKS=G scripts/run-session.sh q5        # phase 1, for each query
#   scripts/select_v4.py                      # the selection, once all G blocks are done
#   scripts/run-session.sh q5                 # phase 2 (A V R), for each query
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
Q="${1:?uso: scripts/run-session.sh <q3|q5|q6|q8|q11>}"
BLOCKS="${BLOCKS:-A V R}"
SELECTED="$ROOT_DIR/results/sessions/selected-v4.txt"
ALL="q3 q5 q6 q8 q11"
PERIOD="${PERIOD:-1200}"

# rate, JOB_WINDOW, the heavy operator (what the autoscaler scales), sine low and high (the peak
# goes past the best placement's capacity at parallelism 2; Q6 collapses for every placement
# above ~110000, so its peak stays short of that)
case "$Q" in
    q3)  RATE=30000; WIN=10;     HEAVY=q3-state-join;   LO=15000; HI=37500 ;;
    q5)  RATE=4000;  WIN="60:1"; HEAVY=hot-items-count; LO=2000;  HI=5000 ;;
    q6)  RATE=95000; WIN=10;     HEAVY=q6-winning-bids; LO=47500; HI=105000 ;;
    q8)  RATE=20000; WIN=10;     HEAVY=new-users-join;  LO=10000; HI=25000 ;;
    q11) RATE=95000; WIN=10;     HEAVY=q11-sessions;    LO=47500; HI=118750 ;;
    *) echo "consulta desconocida: $Q" >&2; exit 2 ;;
esac
OUT="$ROOT_DIR/results/sessions/$Q"
mkdir -p "$OUT"
LOG="$OUT/session.log"

table_for() { ls "$ROOT_DIR"/results/characterizer/"$1"-v4-cpu2105-r*.json 2>/dev/null | head -1 \
    | sed "s|^$ROOT_DIR/||"; }

tables_arms() {        # V4_<Qn> for every query with a trained table (this one's own included)
    local a=""
    for o in $ALL; do
        if [ -n "$(table_for "$o")" ]; then
            a="$a V4_$(echo "$o" | tr 'q' 'Q')"
        else
            echo "  ! sin tabla entrenada de $o: V4_$o queda fuera del bloque G" | tee -a "$LOG" >&2
        fi
    done
    echo "$a"
}

own=$(table_for "$Q")
[ -n "$own" ] || { echo "ERROR: falta la tabla entrenada de $Q" >&2; exit 2; }

echo "==========================================" | tee -a "$LOG"
echo "  Sesión $Q   tasa $RATE   sinusoide $LO-$HI (periodo $((PERIOD / 60)) min)   bloques: $BLOCKS" | tee -a "$LOG"
echo "  tabla propia: $own   operador escalado en R: $HEAVY" | tee -a "$LOG"
echo "  inicio $(date '+%d-%m %H:%M')" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"

run_block() {          # name arms runs dist duration measure_last [extra env assignments...]
    local name="$1" arms="$2" runs="$3" dist="$4" dur="$5" last="$6"
    shift 6
    if [ -f "$OUT/block-$name.done" ]; then
        echo "## bloque $name ya terminado ($(cat "$OUT/block-$name.done")) — se omite" | tee -a "$LOG"
        return
    fi
    echo "## bloque $name: $arms  x$runs  $dist  ${dur}s (medidos ${last}s)  $*  ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
    env "$@" ARMS="$arms" RUNS="$runs" QUERY="$Q" RATE="$RATE" DIST="$dist" DURATION="$dur" \
        JOB_WINDOW="$WIN" MEASURE_LAST="$last" \
        "$SCRIPT_DIR/run-monitor-campaign.sh" 2>&1 | tee "$OUT/block-$name.log"
    local camp
    camp=$(grep -o "results/monitor-campaign/[0-9-]*" "$OUT/block-$name.log" | tail -1)
    if [ -n "$camp" ] && [ -f "$ROOT_DIR/$camp/summary.txt" ]; then
        echo "$camp" > "$OUT/block-$name.done"
        echo "## bloque $name terminado -> $camp ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
    else
        echo "## bloque $name SIN resumen: no se marca como terminado" | tee -a "$LOG"
    fi
}

for b in $BLOCKS; do
    if [ "$b" != G ]; then
        [ -f "$SELECTED" ] || { echo "ERROR: falta $SELECTED — corre G en todas las queries y scripts/select_v4.py" | tee -a "$LOG" >&2; exit 2; }
        read -r SEL SEL_ARM SEL_SCORE < "$SELECTED"
        echo "  v4 seleccionado: $SEL  ($SEL_ARM, peor posición entre queries $SEL_SCORE)" | tee -a "$LOG"
    fi
    case "$b" in
        G) run_block G "$(tables_arms) V4_PRIOR" 5 CONSTANT 960 600 ;;
        A) run_block A "STOCK V4 STOCK_BANDIT BANDIT" 5 CONSTANT 960 600 QTABLE="$SEL" ;;
        V) run_block V "STOCK V4 BANDIT" 5 "SINE:$PERIOD:$LO,$HI" $((360 + PERIOD)) "$PERIOD" QTABLE="$SEL" ;;
        R) run_block R "STOCK V4 BANDIT V4_PRIOR_BANDIT" 5 "SINE:$PERIOD:$LO,$HI" $((360 + PERIOD)) "$PERIOD" \
               QTABLE="$SEL" MONITOR_ARGS="--autoscale --scale-vertex $HEAVY --kmin 1 --kmax 3 --forecast 180" ;;
        *) echo "bloque desconocido: $b" | tee -a "$LOG" ;;
    esac
done
echo "fin de la sesión $Q ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
echo "" | tee -a "$LOG"
# The query's report: every finished block, the declared comparisons -> results/sessions/<q>/report.md
python3 "$SCRIPT_DIR/report_session.py" "$Q" 2>&1 | tee -a "$LOG"
