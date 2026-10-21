#!/bin/bash
# ============================================
# One evaluation session per query: every condition and arm of the final design, unattended
# ============================================
#
# WHY (2026-10-21). The design agreed with the professor — five CAPSys queries, constant and
# sinusoidal load, with and without rescaling, 5 repetitions — runs as one session per query so
# the cluster state, the calibration and the trained tables are shared by every arm of that query.
# Blocks (each one run-monitor-campaign.sh, orders rotated, its own analysis):
#
#   A  constant, no rescaling, 6 + 10 min           x5
#        STOCK V4 STOCK_BANDIT BANDIT + V4_<every other query> + V4_PRIOR
#        (generalisation lives here: same condition, same rotation as the rest)
#   -- after A: the trained table with the best delivery over A's last 10 min (this query's own or
#      another's) is SELECTED; A is the validation set, V and R the test, on a different load
#   V  sine of 20 min, no rescaling, 6 min + one whole period      STOCK V4 BANDIT              x5
#   R  same sine, WITH the common proactive autoscaler (1-3 subtasks of the heavy operator),
#      placement coordinated for V4 and the bandit                 STOCK V4 BANDIT V4_PRIOR_BANDIT  x5
#
# V4 / BANDIT in V and R use the selected table; V4_PRIOR is an empty table (v4's prior alone).
# Rates: where the best placement at parallelism 2 keeps up and a bad one does not
# (scripts/run-plan-calibrations.sh). The 20-min period: detecting, rescaling and settling take
# ~3-5 min, so a 10-min sine left no room for rescaling to pay (simulator: every arm worse).
#
# RESUMABLE: a finished block leaves a marker in results/sessions/<query>/ and is skipped on a
# relaunch; the selection is a file there too. At the end, report_session.py writes report.md.
#
# Usage:
#   scripts/run-session.sh q5
#   BLOCKS="V R" scripts/run-session.sh q8        # only some blocks (A must have run for V/R)
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
Q="${1:?uso: scripts/run-session.sh <q3|q5|q6|q8|q11>}"
BLOCKS="${BLOCKS:-A V R}"
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

cross_arms() {
    local a=""
    for o in $ALL; do
        [ "$o" = "$Q" ] && continue
        if [ -n "$(table_for "$o")" ]; then
            a="$a V4_$(echo "$o" | tr 'q' 'Q')"
        else
            echo "  ! sin tabla entrenada de $o: V4_$o queda fuera del bloque A" | tee -a "$LOG" >&2
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

# The trained table with the best delivery over block A's last 10 min: this query's own (arm V4)
# or another query's (V4_<Qn>). V4_PRIOR is the control, never a candidate. Ties keep the own.
select_table() {
    [ -f "$OUT/selected-table.txt" ] && { cat "$OUT/selected-table.txt"; return; }
    [ -f "$OUT/block-A.done" ] || { echo "$own own (sin bloque A: tabla propia por defecto)"; return; }
    python3 - "$ROOT_DIR" "$(cat "$OUT/block-A.done")" "$Q" <<'EOF' | tee "$OUT/selected-table.txt"
import glob, statistics as st, sys
from pathlib import Path
root, camp, q = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, f"{root}/scripts")
import report_session as rs
runs = rs.load_campaign(Path(root) / camp)
def table(arm):
    qq = q if arm == "V4" else arm[3:].lower()
    hits = sorted(glob.glob(f"{root}/results/characterizer/{qq}-v4-cpu2105-r*.json"))
    return hits[0][len(root) + 1:] if hits else None
scores = {}
for arm in {r["arm"] for r in runs}:
    if arm == "V4" or arm.startswith("V4_Q"):
        w = [r["w"] for r in runs if r["arm"] == arm and r["w"] is not None]
        if w:
            scores[arm] = st.mean(w)
best = max(scores, key=lambda a: (round(scores[a], 3), a == "V4"))
print(f"{table(best)} {best} {scores[best]:.4f}")
EOF
}

for b in $BLOCKS; do
    case "$b" in
        A) run_block A "STOCK V4 STOCK_BANDIT BANDIT$(cross_arms) V4_PRIOR" 5 CONSTANT 960 600 ;;
        V|R)
            read -r SEL SEL_ARM SEL_SCORE <<< "$(select_table)"
            echo "  tabla de v4 para V y R: $SEL  (brazo $SEL_ARM en A, entrega $SEL_SCORE)" | tee -a "$LOG"
            if [ "$b" = V ]; then
                run_block V "STOCK V4 BANDIT" 5 "SINE:$PERIOD:$LO,$HI" $((360 + PERIOD)) "$PERIOD" \
                    QTABLE="$SEL"
            else
                run_block R "STOCK V4 BANDIT V4_PRIOR_BANDIT" 5 "SINE:$PERIOD:$LO,$HI" $((360 + PERIOD)) "$PERIOD" \
                    QTABLE="$SEL" \
                    MONITOR_ARGS="--autoscale --scale-vertex $HEAVY --kmin 1 --kmax 3 --forecast 180"
            fi ;;
        *) echo "bloque desconocido: $b" | tee -a "$LOG" ;;
    esac
done
echo "fin de la sesión $Q ($(date '+%d-%m %H:%M'))" | tee -a "$LOG"
echo "" | tee -a "$LOG"
# The query's report: every finished block, the declared comparisons -> results/sessions/<q>/report.md
python3 "$SCRIPT_DIR/report_session.py" "$Q" 2>&1 | tee -a "$LOG"
