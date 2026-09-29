#!/bin/bash
# ============================================
# The same campaign twice, with the arm order reversed
# ============================================
#
# WHY BOTH PASSES (and why this is not optional). Position, not placement, has
# explained a result twice in this project. The headline campaign's STOCK got WORSE
# when it ran last. And the capacity experiment of 2026-09-03 measured BOTH arms
# doing worse in the second slot of every repetition — LPT 0.988 -> 0.861, STOCK
# 1.000 -> 0.957 — so a single-order campaign hands whichever arm runs first a
# systematic advantage of roughly the size of the effect being looked for.
#
# Running the whole campaign twice with the order flipped is the cheapest control
# that actually works: an arm that wins in both passes won on merit, and one that
# wins only where it goes first won on position.
#
# WHY run-placement-experiment.sh AND NOT THE SHORTER CAPACITY RUNS. Only this
# driver submits wide and rescales narrow, and that is the one thing that gives the
# assigner a choice. Measured the same day: submitting straight at parallelism 2
# left slices=8 with freeSlots=8, `tm-2-medium` excluded from the pool entirely, and
# all twelve cells — both arms, every repetition — produced the identical spread
# {slow=6, fast=2}. Two algorithms with no decision to make cannot be told apart.
# With SUBMIT_PAR=3 TARGET_PAR=2 the same job runs as slices=8 with freeSlots=12 and
# the arms place across all three machines.
#
# Usage:
#   ARMS="STOCK LPT" RATE=7000 REPS=6 scripts/run-paired-campaign.sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ARMS="${ARMS:-STOCK LPT}"
RATE="${RATE:-7000}"
REPS="${REPS:-6}"
QUERY="${QUERY:-q8}"
SUBMIT_PAR="${SUBMIT_PAR:-3}"
TARGET_PAR="${TARGET_PAR:-2}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.ref.RefNexmarkJob}"
TM_DEPLOYMENTS="${TM_DEPLOYMENTS:-flink-tm-fast:1 flink-tm-medium:1 flink-tm-slow:1}"
DRAIN_DEPLOYMENTS="${DRAIN_DEPLOYMENTS:-flink-taskmanager:0}"
WARMUP="${WARMUP:-60}"
WINDOW="${WINDOW:-60}"
PUBLISH_LOADS="${PUBLISH_LOADS:-0}"
# Checkpoint storage is wiped before EVERY ARM, not per pass — the accumulation biases
# the two arms within a pass against each other, so the reset belongs in the driver.
# See the CLEAN_CHECKPOINTS block in run-placement-experiment.sh for the measurements.

set -- $ARMS
[ $# -eq 2 ] || { echo "ERROR: this control needs exactly two arms" >&2; exit 2; }
FORWARD="$1 $2"
REVERSE="$2 $1"

STAMP=$(date +%Y%m%d-%H%M%S)
PAIR_DIR="$ROOT_DIR/results/paired-campaign/$STAMP"
mkdir -p "$PAIR_DIR"

echo "=========================================="
echo "  Paired campaign — $QUERY at $RATE rec/s"
echo "  pass A: $FORWARD"
echo "  pass B: $REVERSE"
echo "  submit par $SUBMIT_PAR -> measured at $TARGET_PAR, reps $REPS"
echo "=========================================="

run_pass() {
    local label="$1" order="$2"
    echo ""
    echo "########## pasada $label: $order"
    # PUBLISH_LOADS is left off on purpose: measuring the load vector during a
    # saturating campaign feeds the arms the busy-time of starved subtasks, which
    # reads LOWER the more starved they are. The vector is published beforehand at
    # a rate the cluster sustains.
    ARMS="$order" QUERY="$QUERY" RATE="$RATE" REPS="$REPS" \
    SUBMIT_PAR="$SUBMIT_PAR" TARGET_PAR="$TARGET_PAR" \
    SLOT_SHARING="$SLOT_SHARING" JOB_CLASS="$JOB_CLASS" \
    TM_DEPLOYMENTS="$TM_DEPLOYMENTS" DRAIN_DEPLOYMENTS="$DRAIN_DEPLOYMENTS" \
    WARMUP="$WARMUP" WINDOW="$WINDOW" PUBLISH_LOADS="$PUBLISH_LOADS" \
        "$SCRIPT_DIR/run-placement-experiment.sh" 2>&1 | tee "$PAIR_DIR/pass-$label.log"

    local produced
    produced=$(ls -td "$ROOT_DIR"/results/placement-experiment/*/ 2>/dev/null | head -1)
    [ -n "$produced" ] || return 1

    # A directory is not a result. When the cluster was unreachable on 2026-09-03
    # the driver still created its output directory, so the pair ran the second
    # pass against a dead cluster and reported "sin episodios utilizables" forty
    # seconds later. A pass with no episodes aborts the pair instead.
    local episodes
    # `grep -c` over a glob prints one count PER FILE, so the arithmetic test below
    # saw "0\n0" and died with "integer expression expected". wc -l over the
    # concatenation is the count we actually meant.
    episodes=$(cat "$produced"/*/episodes-*.csv 2>/dev/null | wc -l | tr -d ' ')
    episodes=${episodes:-0}
    if [ "$episodes" -lt 2 ]; then
        echo "  ! la pasada $label no produjo episodios — se aborta el par" >&2
        return 1
    fi
    echo "$produced" > "$PAIR_DIR/pass-$label.dir"
    echo "  pasada $label -> $produced ($episodes líneas de episodio)"
}

if ! run_pass A "$FORWARD"; then
    echo "" >&2
    echo "ABORTADO: la pasada A no produjo datos. Sin ella la B no tiene con qué" >&2
    echo "compararse, y el control de orden es justamente el punto del par." >&2
    exit 1
fi
if ! run_pass B "$REVERSE"; then
    echo "" >&2
    echo "La pasada B falló. La A queda en $(cat "$PAIR_DIR/pass-A.dir" 2>/dev/null)," >&2
    echo "pero SIN control de orden: no es un resultado, es media medición." >&2
    exit 1
fi

echo ""
echo "=========================================="
DIRS=""
for p in A B; do
    [ -f "$PAIR_DIR/pass-$p.dir" ] && DIRS="$DIRS $(cat "$PAIR_DIR/pass-$p.dir")"
done
if [ -n "$DIRS" ]; then
    python3 "$SCRIPT_DIR/analyse_paired_campaign.py" ${KEEP_FIRST:+--keep-first} ${ANALYSE_SLICES:+--slices $ANALYSE_SLICES} $DIRS | tee "$PAIR_DIR/summary.txt"
fi
echo ""
echo "resultados -> $PAIR_DIR"
