#!/bin/bash
# ============================================
# One evaluation session per query: every condition and arm of the final design, unattended
# ============================================
#
# WHY (2026-10-21). The design agreed with the professor — five CAPSys queries, constant and
# sinusoidal load, with and without rescaling, 5 repetitions, 10 min of measurement — runs as one
# session per query so the cluster state, the calibration and the trained tables are shared by
# every arm of that query. Blocks (each one run-monitor-campaign.sh, orders rotated, its own
# analysis):
#
#   A  constant, no rescaling      STOCK V4 STOCK_BANDIT BANDIT                x5   6+10 min
#   G  constant, generalisation    V4_<every other query> V4_PRIOR            x3   6+10 min
#   V  sine, no rescaling          STOCK V4 STOCK_BANDIT BANDIT                x5   10+10 min
#   R  sine, with rescaling        (coordinated scaling — added when implemented)
#
# V4 is the table trained on THIS query; V4_<Qn> the one trained on Qn (results/characterizer/
# <qn>-v4-cpu2105-r<rate>.json); V4_PRIOR an empty table, v4's prior alone. Rates are where the
# best placement at parallelism 2 keeps up and a bad one does not (scripts/run-plan-calibrations.sh).
# The sine has a 10-min period: one period of warm-up, one measured (MEASURE_LAST=600).
#
# RESUMABLE: a block that finished leaves a marker in results/sessions/<query>/, and a relaunch
# skips it. Interrupting inside a block loses only that block's runs so far.
#
# Usage:
#   scripts/run-session.sh q5
#   BLOCKS="A V" scripts/run-session.sh q8        # only some blocks
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
Q="${1:?uso: scripts/run-session.sh <q3|q5|q6|q8|q11>}"
BLOCKS="${BLOCKS:-A G V}"
ALL="q3 q5 q6 q8 q11"

# rate, JOB_WINDOW, sine low and high (the peak goes past the best placement's capacity; Q6
# collapses for every placement above ~110000, so its peak stays short of that)
case "$Q" in
    q3)  RATE=30000; WIN=10;     LO=15000; HI=37500 ;;
    q5)  RATE=4000;  WIN="60:1"; LO=2000;  HI=5000 ;;
    q6)  RATE=95000; WIN=10;     LO=47500; HI=105000 ;;
    q8)  RATE=20000; WIN=10;     LO=10000; HI=25000 ;;
    q11) RATE=95000; WIN=10;     LO=47500; HI=118750 ;;
    *) echo "consulta desconocida: $Q" >&2; exit 2 ;;
esac
OUT="$ROOT_DIR/results/sessions/$Q"
mkdir -p "$OUT"
LOG="$OUT/session.log"

cross_arms() {
    local a=""
    for o in $ALL; do
        [ "$o" = "$Q" ] && continue
        if ls "$ROOT_DIR"/results/characterizer/"$o"-v4-cpu2105-r*.json >/dev/null 2>&1; then
            a="$a V4_$(echo "$o" | tr 'q' 'Q')"
        else
            echo "  ! sin tabla entrenada de $o: V4_$o queda fuera del bloque G" | tee -a "$LOG" >&2
        fi
    done
    echo "$a V4_PRIOR"
}

own=$(ls "$ROOT_DIR"/results/characterizer/"$Q"-v4-cpu2105-r*.json 2>/dev/null | head -1)
[ -n "$own" ] || { echo "ERROR: falta la tabla entrenada de $Q" >&2; exit 2; }

echo "==========================================" | tee -a "$LOG"
echo "  Sesión $Q   tasa $RATE   sinusoide $LO-$HI (10 min)   bloques: $BLOCKS" | tee -a "$LOG"
echo "  tabla propia: ${own#$ROOT_DIR/}" | tee -a "$LOG"
echo "  inicio $(date '+%d-%m %H:%M')" | tee -a "$LOG"
echo "==========================================" | tee -a "$LOG"

run_block() {          # name arms runs dist duration
    local name="$1" arms="$2" runs="$3" dist="$4" dur="$5"
    if [ -f "$OUT/block-$name.done" ]; then
        echo "## bloque $name ya terminado ($(cat "$OUT/block-$name.done")) — se omite" | tee -a "$LOG"
        return
    fi
    echo "## bloque $name: $arms  x$runs  $dist  ${dur}s  ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
    ARMS="$arms" RUNS="$runs" QUERY="$Q" RATE="$RATE" DIST="$dist" DURATION="$dur" \
        JOB_WINDOW="$WIN" MEASURE_LAST=600 \
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
    case "$b" in
        A) run_block A "STOCK V4 STOCK_BANDIT BANDIT" 5 CONSTANT 960 ;;
        G) run_block G "$(cross_arms)" 3 CONSTANT 960 ;;
        V) run_block V "STOCK V4 STOCK_BANDIT BANDIT" 5 "SINE:600:$LO,$HI" 1200 ;;
        R) echo "## bloque R (con reescalado): todavía no implementado" | tee -a "$LOG" ;;
        *) echo "bloque desconocido: $b" | tee -a "$LOG" ;;
    esac
done
echo "fin de la sesión $Q ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
