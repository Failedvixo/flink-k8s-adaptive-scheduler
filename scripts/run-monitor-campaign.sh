#!/bin/bash
# ============================================
# Part B campaign: STOCK vs v4 without monitor vs v4 + online bandit, in rotated order
# ============================================
#
# WHY (2026-10-13). The monitor has to be compared with what a deployment would otherwise run:
# Flink's own placement (STOCK) and v4's plan left alone (V4). Each run is one job at constant
# parallelism for DURATION seconds under the same rate profile, measured by the same loop
# (scripts/plan_bandit.py in its three modes) — so exploration costs are INCLUDED in the bandit's
# numbers, as they would be in production. Order rotates every pass (A B C, B C A, C A B), as in the
# multi-arm placement campaigns, so no arm always runs first or last in a session.
#
# The analysis counts RUNS: each run's mean delivered/offered and mean p50 latency over all its
# windows, then a permutation test per pair of arms.
#
# Usage:
#   QUERY=q5 JOB_WINDOW="60:1" RATE=6000 DIST="SINE:3600:2000,6000" DURATION=7200 RUNS=3 \
#     scripts/run-monitor-campaign.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ARMS="${ARMS:-STOCK V4 BANDIT}"
RUNS="${RUNS:-3}"
export QUERY="${QUERY:-q5}"
export RATE="${RATE:-6000}"
export DIST="${DIST:-SINE:3600:2000,6000}"
export DURATION="${DURATION:-7200}"
export JOB_WINDOW="${JOB_WINDOW:-10}"
export QTABLE="${QTABLE:-results/characterizer/q8-v4-20261008.json}"

set -- $ARMS
NARMS=$#
[ $(( RUNS % NARMS )) -eq 0 ] || echo "AVISO: RUNS=$RUNS no es múltiplo de $NARMS brazos; el orden quedará desbalanceado" >&2

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT_DIR/results/monitor-campaign/$STAMP"
mkdir -p "$OUT"
PER_RUN=$(( DURATION / 60 + 8 ))
echo "=========================================="
echo "  Campaña de monitoreo — $QUERY a $RATE ($DIST)"
echo "  brazos: $ARMS   pasadas: $RUNS   corrida: ${DURATION}s"
echo "  duración estimada: ~${PER_RUN} min por corrida x $(( NARMS * RUNS )) corridas = ~$(( PER_RUN * NARMS * RUNS / 60 )) h"
echo "  -> $OUT"
echo "=========================================="
echo "QUERY=$QUERY RATE=$RATE DIST=$DIST DURATION=$DURATION JOB_WINDOW=$JOB_WINDOW QTABLE=$QTABLE ARMS=$ARMS RUNS=$RUNS" > "$OUT/config.txt"

arms_list=($ARMS)
for pass in $(seq 0 $(( RUNS - 1 ))); do
    order=()
    for i in $(seq 0 $(( NARMS - 1 ))); do order+=("${arms_list[$(( (i + pass) % NARMS ))]}"); done
    echo "################ pasada $(( pass + 1 )) / $RUNS: ${order[*]}  ($(date +%H:%M))"
    for arm in "${order[@]}"; do
        ARM="$arm" "$SCRIPT_DIR/run-replan-experiment.sh" 2>&1 | tee "$OUT/pass-$(( pass + 1 ))-$arm.log"
        dir=$(grep -o "results/replan-experiment/[0-9-]*-$arm" "$OUT/pass-$(( pass + 1 ))-$arm.log" | tail -1)
        echo "$(( pass + 1 )) $arm $ROOT_DIR/$dir" >> "$OUT/runs.txt"
    done
done

echo ""
echo "=========================================="
echo "  ANÁLISIS"
echo "=========================================="
python3 - "$OUT" <<'EOF' | tee "$OUT/summary.txt"
import csv, glob, itertools, random, sys
out = sys.argv[1]
runs = []
for line in open(f"{out}/runs.txt"):
    p, arm, d = line.split()
    rows = [r for f in glob.glob(f"{d}/monitor2-*.csv") for r in csv.DictReader(open(f))]
    if not rows:
        print(f"  ! pasada {p} {arm}: sin registros en {d}")
        continue
    ratio = sum(float(r["ratio"]) for r in rows) / len(rows)
    lats = [float(r["latency_ms"]) for r in rows if r["latency_ms"] not in ("", "None", "-1", "-1.0")]
    moves = sum(r["decision"] == "mover" for r in rows)
    runs.append({"pass": int(p), "arm": arm, "ratio": ratio, "lat": sum(lats) / len(lats) if lats else None,
                 "windows": len(rows), "moves": moves})

arms = list(dict.fromkeys(r["arm"] for r in runs))
print(f"\n{'brazo':8s} {'n':>2s} {'entrega':>9s} {'latencia p50':>13s} {'cambios':>8s}   por pasada (entrega)")
for a in arms:
    rs = [r for r in runs if r["arm"] == a]
    lat = [r["lat"] for r in rs if r["lat"] is not None]
    print(f"{a:8s} {len(rs):2d} {sum(r['ratio'] for r in rs) / len(rs):9.1%} "
          f"{(sum(lat) / len(lat) / 1000) if lat else float('nan'):11.1f} s {sum(r['moves'] for r in rs) / len(rs):8.1f}   "
          + "  ".join(f"{r['ratio']:.1%}" for r in sorted(rs, key=lambda r: r['pass'])))

def perm(x, y, iters=20000):
    obs = abs(sum(x) / len(x) - sum(y) / len(y))
    pool, n = x + y, len(x)
    combos = list(itertools.combinations(range(len(pool)), n))
    if len(combos) <= iters:
        hits = sum(abs(sum(pool[i] for i in c) / n - sum(pool[i] for i in range(len(pool)) if i not in c) / (len(pool) - n)) >= obs - 1e-12 for c in combos)
        return hits / len(combos)
    hits = 0
    for _ in range(iters):
        random.shuffle(pool)
        hits += abs(sum(pool[:n]) / n - sum(pool[n:]) / (len(pool) - n)) >= obs - 1e-12
    return hits / iters

print("\nComparaciones (por corrida; prueba de permutación):")
for a, b in itertools.combinations(arms, 2):
    for key, label in (("ratio", "entrega"), ("lat", "latencia")):
        x = [r[key] for r in runs if r["arm"] == b and r[key] is not None]
        y = [r[key] for r in runs if r["arm"] == a and r[key] is not None]
        if len(x) >= 2 and len(y) >= 2:
            d = (sum(x) / len(x)) / (sum(y) / len(y)) - 1
            print(f"  {b} vs {a}: {label} {d:+.1%}  p={perm(x, y):.3f}  (n={len(x)} vs {len(y)})")
EOF
echo "  -> $OUT"
