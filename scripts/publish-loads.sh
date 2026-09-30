#!/bin/bash
# ============================================
# Publish the per-vertex processing cost the assigner should weigh slices by
# ============================================
#
# WHY (measured 2026-08-17): the fork's balance term used to count slices per
# TaskManager. A count cannot tell apart arrangements that differ only in WHICH
# slices share a machine — with 4 slices on 3 TMs every arrangement is 2+1+1 —
# so the term cancelled out and ACO/GA were left optimising state locality
# alone. They put both expensive slices on one TaskManager in 10 of 10
# repetitions and lost 45% of throughput doing it.
#
# This publishes what the assigner was missing: how much each vertex actually
# costs. A slice's weight is then the sum over the vertices it contains, which
# differs between slices exactly when some operator runs at a lower parallelism
# than the rest — the case that matters.
#
# The cost is `busyTimeMsPerSecond` averaged over a vertex's subtasks: CPU-time
# per second of wall clock, which is Flink's own measurement of what the paper
# calls RD^c_Tj. Dividing it by `numRecordsInPerSecond` would give the paper's
# rho^Tj_c (cost per record) — a rate-independent signature of the vertex —
# but the absolute figure is what the balance term needs, since two slices are
# only worth separating if they cost a lot RIGHT NOW.
#
# Written the same way as the arm: temp file plus atomic rename, because the
# JobManager may read at any instant and a half-written file would change a
# placement. The assigner caches it for a second and keeps the last good copy on
# a failed read.
#
# Usage:
#   scripts/publish-loads.sh              # measure the running job and publish
#   LOAD_METRIC=rate scripts/publish-loads.sh   # weigh by records/s, not by busy time
#   scripts/publish-loads.sh --read       # what is published right now
#   scripts/publish-loads.sh --clear      # remove it; every slice weighs 1.0 again

set -eu

NODE="${THESIS_NODE:-minikube}"
THESIS_DIR=/var/thesis

# docker exec FIRST, minikube ssh as fallback (2026-09-30), as publish-arm.sh has done since
# 2026-09-26: minikube ssh hung the driver more than once, and a hang here is worse than in the
# arm file because the LPT_ORACLE arm cannot run without its vector. docker exec is already
# root inside the node, so the sudo that minikube ssh needs is stripped for it.
node_sh() {
    timeout -k 5 "${NODE_TIMEOUT:-30}" docker exec "$NODE" sh -c "${1//sudo /}" </dev/null 2>/dev/null \
        || timeout -k 5 "${NODE_TIMEOUT:-30}" minikube ssh -n "$NODE" -- "$1" </dev/null 2>/dev/null
}
LOADS_FILE="$THESIS_DIR/loads"
TMP_FILE="$THESIS_DIR/.loads.tmp"
NAMESPACE=flink

jm_curl() {
    kubectl exec -n "$NAMESPACE" deployment/flink-jobmanager -- \
        curl -s -m 15 "http://localhost:8081$1" 2>/dev/null
}

case "${1:-}" in
    --read)
        node_sh "sudo cat $LOADS_FILE 2>/dev/null || echo '(nothing published)'" | tr -d '\r'
        exit 0
        ;;
    --clear)
        node_sh "sudo rm -f $LOADS_FILE" >/dev/null
        echo "cleared — every slice weighs 1.0 again"
        exit 0
        ;;
    # A DECLARED vector, not a measured one. This is an oracle and says so: it answers
    # "what would a PERFECT profiler be worth here?", which is the ceiling on what the
    # characterising agent could ever capture. Worth having because busy time turned out
    # not to be measurable on this cluster (2026-09-21) — so the honest way to bound the
    # load-aware dimension is to hand LPT the ranking and see whether it changes anything.
    # Needs a RUNNING job only to resolve names to JobVertexIDs; those ids survive across
    # submissions and a change of parallelism, so one publish serves a whole campaign.
    #
    #   scripts/publish-loads.sh --declare 'new-users-join=10'
    #   scripts/publish-loads.sh --declare 'join=10,Sink=2'      # unmatched vertices = 1.0
    --declare)
        SPEC="${2:-}"
        [ -n "$SPEC" ] || { echo "ERROR: --declare necesita 'patrón=costo[,patrón=costo]'" >&2; exit 2; }
        JID=$(jm_curl "/jobs/overview" | python3 -c "
import json, sys
jobs = [j for j in json.load(sys.stdin).get('jobs', []) if j.get('state') == 'RUNNING']
print(jobs[0]['jid'] if jobs else '')" 2>/dev/null || true)
        [ -n "$JID" ] || { echo "ERROR: hace falta un job RUNNING para resolver los ids" >&2; exit 1; }
        CONTENT=$(jm_curl "/jobs/$JID" | SPEC="$SPEC" python3 -c "
import json, os, re, sys
pairs = []
for part in os.environ['SPEC'].split(','):
    pattern, _, cost = part.rpartition('=')
    if pattern.strip():
        pairs.append((pattern.strip(), float(cost)))
out = []
for v in json.load(sys.stdin).get('vertices', []):
    name = v.get('name', '')
    cost = next((c for p, c in pairs if re.search(p, name, re.IGNORECASE)), 1.0)
    print(f\"  {name[:40]}  ->  {cost}\", file=sys.stderr)
    out.append(f\"{v['id']} {cost}\")
print('\n'.join(out))")
        [ -n "$CONTENT" ] || { echo "ERROR: no se pudo construir el vector" >&2; exit 1; }
        node_sh "sudo mkdir -p $THESIS_DIR && \
            printf '%s\n' '$CONTENT' | sudo tee $TMP_FILE >/dev/null && \
            sudo mv -f $TMP_FILE $LOADS_FILE && sudo chmod 644 $LOADS_FILE" >/dev/null
        echo "published (DECLARADO, no medido) -> $LOADS_FILE"
        exit 0
        ;;
    # Disable/enable rather than clear/re-measure: re-measuring needs a running job,
    # and the poisoning phase of the placement experiment has to turn the weights off
    # and back on around a rescale without one.
    --disable)
        # Moving the file away does NOT disable the weights: the assigner keeps its last
        # good copy when a read fails, which is deliberate (a half-written file must not
        # change a placement) but makes deletion a no-op. Measured 2026-08-17 — the
        # poisoning phase silently kept the weights and never produced the bad layout.
        #
        # Instead publish a file the assigner can read perfectly well but that names no
        # vertex of this job: sliceLoads() then finds no match, returns null, and every
        # slice falls back to weight 1.0. Same effect, no fork change, takes effect on
        # the next read like any other publish.
        node_sh "sudo mv -f $LOADS_FILE $LOADS_FILE.off 2>/dev/null || true; \
            printf '%s\n' '00000000000000000000000000000000 1.0' | sudo tee $LOADS_FILE >/dev/null; \
            sudo chmod 644 $LOADS_FILE" >/dev/null
        echo "disabled — published a no-match file, so every slice weighs 1.0 until --enable"
        exit 0
        ;;
    --enable)
        node_sh "sudo mv -f $LOADS_FILE.off $LOADS_FILE 2>/dev/null || true" >/dev/null
        echo "enabled"
        exit 0
        ;;
esac

JID=$(jm_curl "/jobs/overview" | python3 -c "
import json, sys
jobs = [j for j in json.load(sys.stdin).get('jobs', []) if j.get('state') == 'RUNNING']
print(jobs[0]['jid'] if jobs else '')" 2>/dev/null || true)
if [ -z "$JID" ]; then
    echo "ERROR: no RUNNING job to measure" >&2; exit 1
fi

DETAIL=$(jm_curl "/jobs/$JID")
VERTICES=$(echo "$DETAIL" | python3 -c "
import json, sys
for v in json.load(sys.stdin).get('vertices', []):
    print(v['id'], v.get('parallelism', 1), v.get('name', '')[:40].replace(' ', '_'))" 2>/dev/null)

# SOURCES NEED THEIR OWN BUSY METRIC (2026-09-15). Flink measures busyTimeMsPerSecond on
# the task mailbox; a legacy SourceFunction runs in its own thread and reports NaN. This
# script used to publish that NaN as 0.0, so the Beam generator — the costliest part of
# the job — was priced as free, and LPT packed both person sources and both sinks onto
# the one-core machine: LPT went from +11.2% over STOCK (unit loads) to -11.0% (published
# loads), with speeds, memory and STOCK unchanged. RefPersonSource and RefAuctionSource
# now expose `generatorBusyMsPerSecond`; its id carries the operator name as a prefix, so
# it is found by suffix once per vertex and read whenever Flink's own figure is NaN.
VERTICES=$(while read -r VID PAR NAME; do
    [ -n "$VID" ] || continue
    GEN=$(jm_curl "/jobs/$JID/vertices/$VID/subtasks/0/metrics" | python3 -c "
import json, sys
try:
    ids = [m['id'] for m in json.load(sys.stdin) if m['id'].endswith('generatorBusyMsPerSecond')]
    print(ids[0] if ids else '-')
except Exception:
    print('-')" 2>/dev/null || echo -)
    echo "$VID $PAR $NAME $GEN"
done <<< "$VERTICES")

# WHY THIS IS SAMPLED REPEATEDLY AND NOT READ ONCE (measured 2026-08-29):
# `busyTimeMsPerSecond` is a short-window gauge, so a single read is a one-second
# photograph. Two campaigns with identical configuration published vectors that
# differed by up to 10x on the same vertex (Filter 26.7 vs 248.0 ms/s), which is
# larger than the differences between the vertices the vector exists to rank.
# Every capacity-aware arm consumes this: LPT sorts by it, ACO and GA score with
# it. Optimising a noisy estimate is how four campaigns of real structure came
# out looking like the noise floor. The median over several spaced samples costs
# well under a minute against campaigns that run for hours.
UNIT="ms/s"
[ "${LOAD_METRIC:-busy}" = "rate" ] && UNIT="rec/s"
SAMPLES="${LOAD_SAMPLES:-9}"
INTERVAL="${LOAD_INTERVAL:-2}"
RAW="$(mktemp)"
trap 'rm -f "$RAW"' EXIT

echo "  sampling ${SAMPLES}x every ${INTERVAL}s..."
for _round in $(seq 1 "$SAMPLES"); do
    while read -r VID PAR NAME GEN; do
        [ -n "$VID" ] || continue
        GET="busyTimeMsPerSecond,numRecordsInPerSecond,numRecordsOutPerSecond"
        [ "$GEN" != "-" ] && GET="$GET,$GEN"
        for i in $(seq 0 $((PAR - 1))); do
            # NaN, not 0.0, when nothing measured the vertex: "unmeasured" and "free" are
            # different claims, and the aggregation below refuses to publish the first as
            # the second.
            BUSY=$(jm_curl "/jobs/$JID/vertices/$VID/subtasks/$i/metrics?get=$GET" |
                LOAD_METRIC="${LOAD_METRIC:-busy}" python3 -c "
import json, os, sys, math
try:
    vals = {m['id']: float(m['value']) for m in json.load(sys.stdin)}
    # LOAD_METRIC=rate publishes RECORDS PER SECOND THROUGH THE SUBTASK instead of CPU
    # occupancy, and it exists because busy time turned out not to be measurable here
    # (2026-09-20/21). Four readings of new-users-join, same placement on `tm-3-fast`,
    # same rate, same query: 40, 184, 324 and finally 0.0 ms/s with idle at 1000 while
    # the operator emitted 7093 rec/s. The rates, over those same runs, never moved by
    # more than 1% (person 1301-1363, auction 3938-4055).
    #
    # What it buys and what it costs, stated plainly: the rate is reproducible and
    # independent of the machine the subtask landed on, which busy time is not, but it
    # prices every record the same, so a windowed join and a tagging Map at equal rates
    # weigh the same. It is a model, and a worse one than a working CPU measurement
    # would be — it is simply the one whose input can be measured twice with the same
    # answer.
    if os.environ.get('LOAD_METRIC') == 'rate':
        rate = vals.get('numRecordsInPerSecond', float('nan'))
        if not math.isfinite(rate) or rate <= 0:
            rate = vals.get('numRecordsOutPerSecond', float('nan'))  # a source has no input
        print(rate if math.isfinite(rate) else 'nan')
        raise SystemExit(0)
    # The generator's own gauge WINS where it exists (2026-09-20), rather than being a
    # fallback for NaN. Flink's figure for a legacy source is not just missing, it is
    # wrong: one auction subtask reported -2648 ms/s beside its twin's 29. A negative
    # cost inverts LPT instead of merely blurring it. Outside [0,1000] nothing is a
    # measurement of one second of wall clock, so it is dropped rather than published.
    gen = next((v for k, v in vals.items() if k.endswith('generatorBusyMsPerSecond')), None)
    busy = gen if gen is not None else vals.get('busyTimeMsPerSecond', float('nan'))
    print(busy if math.isfinite(busy) and 0.0 <= busy <= 1000.0 else 'nan')
except Exception:
    print('nan')" 2>/dev/null || echo nan)
            echo "$VID $i $BUSY" >> "$RAW"
        done
    done <<< "$VERTICES"
    [ "$_round" -lt "$SAMPLES" ] && sleep "$INTERVAL"
done

# Median per subtask across the rounds, then the mean across subtasks. Median
# first because the outliers are transients — a GC pause, a checkpoint — that a
# mean would carry straight into the placement.
#
# Per-SUBTASK cost, not per-vertex: a slice contains one subtask, so summing
# vertex totals would weigh a wide operator as if every slice carried all of it.
COSTS=$(python3 -c "
import collections, math, statistics, sys
samples = collections.defaultdict(list)
for line in open('$RAW'):
    parts = line.split()
    if len(parts) == 3 and math.isfinite(float(parts[2])):
        samples[(parts[0], parts[1])].append(float(parts[2]))
byVertex = collections.defaultdict(list)
for (vid, _subtask), values in samples.items():
    byVertex[vid].append(statistics.median(values))
for vid, medians in byVertex.items():
    print(vid, round(sum(medians) / len(medians), 3))
")
# A vertex with no finite sample is left OUT and named, never published as zero. Leaving
# it out still counts as zero inside the fork's slice sum, so this is a loud failure to
# fix, not a fallback: before 2026-09-15 it was the silent one that lost a campaign.
UNMEASURED=$(while read -r VID PAR NAME GEN; do
    [ -n "$VID" ] || continue
    echo "$COSTS" | grep -q "^$VID " || echo "      $NAME"
done <<< "$VERTICES")
if [ -n "$UNMEASURED" ]; then
    echo "  ! sin medición, quedan FUERA del vector (el fork los suma como 0):" >&2
    echo "$UNMEASURED" >&2
fi

CONTENT=""
while read -r VID PAR NAME GEN; do
    [ -n "$VID" ] || continue
    COST=$(echo "$COSTS" | awk -v v="$VID" '$1 == v {print $2}')
    # Skipped, not zeroed — the second path by which "unmeasured" became "free".
    [ -n "$COST" ] || continue
    SRC=""
    [ "$GEN" != "-" ] && SRC="  (medido por el generador)"
    echo "  $NAME  par=$PAR  ${COST} $UNIT per subtask$SRC"
    CONTENT="${CONTENT}${VID} ${COST}
"
done <<< "$VERTICES"

if [ -z "$CONTENT" ]; then
    echo "ERROR: measured nothing" >&2; exit 1
fi

node_sh "sudo mkdir -p $THESIS_DIR && \
    printf '%s' '$CONTENT' | sudo tee $TMP_FILE >/dev/null && \
    sudo mv -f $TMP_FILE $LOADS_FILE && sudo chmod 644 $LOADS_FILE" >/dev/null

echo "published -> $LOADS_FILE  (takes effect on the next rescale)"
