#!/bin/bash
# ============================================
# Capacity per placement arm, measured without overloading the cluster
# ============================================
#
# WHY THIS REPLACES THE FIXED-RATE CAMPAIGN (2026-09-03). The campaign design was
# "pick a rate above every arm's capacity, then measured throughput IS that
# placement's capacity, so the rate cannot favour anyone". Sound for a stateless
# job; not for this one. Q8 windows on event time, and deep overload backpressures
# the source, which slows event time, which delays window firing, which grows state
# without bound. Measured in one afternoon: checkpoints went 8.9 MB -> 104 -> 169 ->
# 333 MB, and the kube-apiserver was killed by its own liveness probe 74 times.
#
# The fix is to stop overloading. At a rate only slightly above the weakest arm's
# capacity, the FRACTION OF THE REQUESTED RATE an arm sustains is already a
# capacity measure — continuous, bounded, and needing no ceiling search:
#
#     ratio = source output / requested rate     (1.0 = keeps up, <1 = saturated)
#
# It also answers the objection that calibrating with one arm biases the others:
# there is no shared target anyone has to meet, only a common load under which each
# arm's shortfall is its own.
#
# ORDER IS ALTERNATED between repetitions, because it has bitten this project
# twice: in the headline campaign STOCK got WORSE when it ran last, and yesterday's
# LPG-vs-STOCK pair had LPT always first. Odd repetitions run the arms as given,
# even ones reversed, so a monotone drift cancels instead of accumulating on one arm.
#
# Each measurement is one short job (warmup + window), so an infrastructure hiccup
# costs one cell of ~3 minutes rather than a 20-minute campaign — which on this
# cluster is not a hypothetical.
#
# Usage:
#   ARMS="STOCK LPT" RATE=6000 REPS=6 scripts/run-ceiling-experiment.sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ARMS="${ARMS:-STOCK LPT}"
RATE="${RATE:-6000}"
REPS="${REPS:-6}"
QUERY="${QUERY:-q8}"
SUBMIT_PAR="${SUBMIT_PAR:-2}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.ref.RefNexmarkJob}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT_DIR="$ROOT_DIR/results/ceiling-experiment/$STAMP"
mkdir -p "$OUT_DIR"
CSV="$OUT_DIR/measurements.csv"
echo "rep,position,arm,rate,source_out_rps,ratio,backpressure_ms_s,sustained" > "$CSV"

echo "=========================================="
echo "  Capacity per arm — $QUERY at $RATE rec/s"
echo "  arms: $ARMS   reps: $REPS   parallelism: $SUBMIT_PAR"
echo "  order alternates every repetition"
echo "=========================================="

set -- $ARMS
FIRST="$1"; SECOND="${2:-}"
if [ -z "$SECOND" ]; then
    echo "ERROR: this design needs exactly two arms to alternate" >&2
    exit 2
fi

for rep in $(seq 1 "$REPS"); do
    if [ $((rep % 2)) -eq 1 ]; then
        ORDER="$FIRST $SECOND"
    else
        ORDER="$SECOND $FIRST"
    fi
    echo ""
    echo "--- rep $rep/$REPS   orden: $ORDER"

    POS=0
    for ARM in $ORDER; do
        POS=$((POS + 1))
        if ! "$SCRIPT_DIR/publish-arm.sh" "$ARM" >/dev/null 2>&1; then
            echo "  ! no se pudo publicar $ARM — se salta esta celda" | tee -a "$OUT_DIR/run.log"
            continue
        fi

        CELL="$OUT_DIR/rep${rep}-pos${POS}-${ARM}"
        mkdir -p "$CELL"
        # calibrate-rate.sh at a single rate is exactly one measurement: submit,
        # warm up, sample the sources, report the ratio. Reusing it keeps this
        # experiment on the same measurement code as the calibrations.
        if ! QUERY="$QUERY" SUBMIT_PAR="$SUBMIT_PAR" JOB_CLASS="$JOB_CLASS" \
             SLOT_SHARING="$SLOT_SHARING" RATES="$RATE" \
             "$SCRIPT_DIR/calibrate-rate.sh" > "$CELL/calibrate.log" 2>&1; then
            echo "  ! $ARM: la medición falló, ver $CELL/calibrate.log" | tee -a "$OUT_DIR/run.log"
            continue
        fi

        RESULT=$(ls -td "$ROOT_DIR"/results/rate-calibration/*/ 2>/dev/null | head -1)
        if [ -f "$RESULT/calibration.json" ]; then
            cp "$RESULT/calibration.json" "$CELL/calibration.json"
            ROW=$(REP="$rep" POS="$POS" ARM="$ARM" python3 -c "
import json, os
d = json.load(open('$CELL/calibration.json'))
s = d['steps'][0] if d.get('steps') else {}
print(','.join(str(x) for x in [
    os.environ['REP'], os.environ['POS'], os.environ['ARM'], s.get('rate', ''),
    round(s.get('source_out_rps', 0), 1), s.get('ratio', ''),
    round(s.get('backpressure_ms_s', 0), 1), s.get('sustained', '')]))")
            echo "$ROW" >> "$CSV"
            echo "  $ARM (pos $POS): $(echo "$ROW" | cut -d, -f5-6 | tr ',' ' ')"
        else
            echo "  ! $ARM: sin calibration.json" | tee -a "$OUT_DIR/run.log"
        fi

        # Which placement the fork actually applied, recovered while the log still
        # holds it. Verifying the arm from the JobManager rather than from
        # publish-arm.sh caught a false negative on 2026-09-02 and is cheap.
        JM=$(kubectl get pod -n flink -l component=jobmanager \
            --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
        [ -n "$JM" ] && kubectl logs -n flink "$JM" 2>/dev/null \
            | grep -E "THESIS_ASSIGN|THESIS_ARM" | tail -20 > "$CELL/thesis-assign.log" || true
    done
done

echo ""
echo "=========================================="
python3 "$SCRIPT_DIR/summarise_ceiling.py" "$CSV" || cat "$CSV"
echo ""
echo "resultados -> $OUT_DIR"
