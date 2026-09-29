#!/bin/bash
# ============================================
# N paired campaigns back to back, analysed as ONE sample of jobs
# ============================================
#
# WHY (2026-09-15). The arms repeat the SAME placement at every repetition inside a job:
# STOCK draws its placement once and state locality sends each rescale back to the same
# slots. So the independent sample is the JOB, not the episode, and the campaign that
# looked like p=0.0038 on 11 vs 14 episodes was 4 vs 4 jobs. STOCK's jobs alone ranged
# from 18534 to 30479 rec/s: the variance that matters is between draws, and only more
# jobs reduce it.
#
# The protocol follows CAPSys (EuroSys'25), stated so it can be cited rather than chosen:
#   * 10 runs per policy, each a fresh deployment — "repeated 10 times for each policy to
#     capture the randomness inherent in the baseline approaches" (artifact appendix);
#   * a 6-minute warm-up and 10 minutes of metrics (§6.2).
# Here that is RUNS paired campaigns of one repetition each: every campaign gives each arm
# two jobs, one in each position, so RUNS=5 yields 10 jobs per arm with the order balanced.
#
# SCHEDULE="3 2*" measures only the rescale to 8 slices — the decision every analysis so far
# has used — but goes through an UNMEASURED step at 12 first, held just long enough to take
# effect (WIDE_HOLD=120 instead of warm-up+window).
#
# THE UNMEASURED STEP IS NOT DECORATION (measured 2026-09-15). With SCHEDULE="2*" the job
# narrows 60 s after submission, and `tm-3-fast` had not offered its slots yet: the assigner
# saw a ten-slot pool, LPT and STOCK produced the IDENTICAL spread {medium=3, slow=5}, and
# that pass could not tell the arms apart at all. Pinning parallelism to 3 first makes the
# driver block until every vertex actually reaches it, which cannot happen until all twelve
# slots — and therefore all three machines — are in the pool. The old "3* 2" schedule hid
# this by spending sixteen minutes at 12 slices before narrowing.
#
# All RUNS campaigns run in one session, so no job is pooled with jobs from another day,
# another cluster restart or another jar.
#
# Usage:
#   ARMS="STOCK LPT" scripts/run-replicated-campaign.sh          # 5 campaigns, ~7 h
#   RUNS=2 ARMS="STOCK LPT" scripts/run-replicated-campaign.sh   # shorter

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RUNS="${RUNS:-5}"
export ARMS="${ARMS:-STOCK LPT}"
export QUERY="${QUERY:-q8}"
# 40000 comes from the calibration of 2026-09-21 over the FIXED Q8 (the join emits again):
# the sink peaks at 24726 rec/s there and backpressure jumps 55 -> 384 ms/s, so the cluster is
# at its limit and a bad placement costs throughput. Below 32000 it absorbs everything linearly
# and the arms cannot be told apart; at 48000 it is past the cliff — output RETREATS to 20704,
# below what 32000 delivers. The old 38000 default predates the join fix and is not comparable.
export RATE="${RATE:-40000}"
export REPS="${REPS:-1}"
export SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
export SUBMIT_PAR="${SUBMIT_PAR:-3}"
export TARGET_PAR="${TARGET_PAR:-2}"
export MAX_EVENT_AGE="${MAX_EVENT_AGE:-5000}"
export SCHEDULE="${SCHEDULE:-3 2*}"
# The wide step only has to take effect; it is not measured, so it does not need a window.
export WIDE_HOLD="${WIDE_HOLD:-120}"
export WARMUP="${WARMUP:-360}"
export WINDOW="${WINDOW:-600}"
# CHARACTERISER pasa al driver, no se queda aquí (2026-09-21): sin esta línea la campaña
# corre el brazo RL sobre el plan que haya quedado en el nodo y nadie se entera.
export CHARACTERISER="${CHARACTERISER:-0}"
export AGENT_QTABLE="${AGENT_QTABLE:-}"
export AGENT_FREEZE="${AGENT_FREEZE:-}"
export KEEP_FIRST=1
# Pin the analysed width to the measured step. The largest stratum is the right default in
# general, but a short-window run also measures the TRANSITION step, and there the fixed
# plans do not apply and both arms fall back to LPT — comparing two identical placements.
# Q8 under PER_STAGE has four stages, so the measured width is 4 x TARGET_PAR.
export ANALYSE_SLICES="${ANALYSE_SLICES:-$(( TARGET_PAR * ${PLAN_STAGES:-4} ))}"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT_DIR/results/replicated-campaign/$STAMP"
mkdir -p "$OUT"

echo "=========================================="
echo "  Campaña replicada — $RUNS campañas pareadas = $((RUNS * 2)) jobs por brazo"
echo "  $QUERY a $RATE rec/s, brazos: $ARMS"
echo "  calentamiento ${WARMUP}s + ventana ${WINDOW}s, esquema '$SCHEDULE', reps $REPS"
echo "  -> $OUT"
echo "=========================================="

DIRS=""
FAILED=""
for run in $(seq 1 "$RUNS"); do
    echo ""
    echo "################ campaña $run / $RUNS  ($(date +%H:%M))"
    before=$(ls -td "$ROOT_DIR"/results/paired-campaign/*/ 2>/dev/null | head -1)
    "$SCRIPT_DIR/run-paired-campaign.sh" 2>&1 | tee "$OUT/campaign-$run.log"
    pair=$(ls -td "$ROOT_DIR"/results/paired-campaign/*/ 2>/dev/null | head -1)
    # A campaign that aborts must not end the night: the others are still independent
    # jobs. It is recorded, and only the passes it actually completed are analysed.
    if [ -z "$pair" ] || [ "$pair" = "$before" ]; then
        FAILED="$FAILED $run"
        continue
    fi
    got=0
    for p in A B; do
        if [ -f "$pair/pass-$p.dir" ]; then
            DIRS="$DIRS $(cat "$pair/pass-$p.dir")"
            got=$((got + 1))
        fi
    done
    [ "$got" -eq 2 ] || FAILED="$FAILED $run(pasadas:$got)"
    echo "$pair" >> "$OUT/campaigns.txt"
done

echo ""
echo "=========================================="
echo "  ANÁLISIS CONJUNTO"
echo "=========================================="
[ -n "$FAILED" ] && echo "  ! campañas incompletas:$FAILED" | tee "$OUT/failed.txt"
if [ -z "$DIRS" ]; then
    echo "ERROR: ninguna pasada produjo datos" >&2
    exit 1
fi
echo "$DIRS" | tr ' ' '\n' | sed '/^$/d' > "$OUT/pass-dirs.txt"
python3 "$SCRIPT_DIR/analyse_paired_campaign.py" --keep-first ${ANALYSE_SLICES:+--slices $ANALYSE_SLICES} $DIRS | tee "$OUT/summary.txt"
echo ""
echo "resultados -> $OUT"
