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
# The analysis counts RUNS and uses each run's WHOLE-RUN LEDGER (scripts/run_ledger.py): delivered
# over offered from start to end — settling after moves and restarts included, every event weighing
# the same whatever the rate — and the time-weighted p50 / p99 latency; then a permutation test per
# pair of arms.
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
# Empty = each run uses the table trained on its OWN query (run-replan-experiment.sh, table_for).
export QTABLE="${QTABLE:-}"
export MEASURE_LAST="${MEASURE_LAST:-600}"

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
import csv, glob, itertools, os, random, sys
out = sys.argv[1]
MEASURE_LAST = float(os.environ.get("MEASURE_LAST", "600"))
runs = []
for line in open(f"{out}/runs.txt"):
    p, arm, d = line.split()
    # THE EVALUATION USES THE WHOLE-RUN LEDGER, not the monitor's windows: settling after a move
    # and restarts count, and every event weighs the same whatever the rate at the time.
    led = list(csv.DictReader(open(f"{d}/ledger.csv"))) if glob.glob(f"{d}/ledger.csv") else []
    if not led:
        print(f"  ! pasada {p} {arm}: sin ledger.csv en {d}")
        continue
    end = led[-1]
    ratio = float(end["delivered_cum"]) / float(end["offered_cum"])
    lats = [float(r["p50_ms"]) for r in led if r["p50_ms"] not in ("", "None")]
    p99 = [float(r["p99_ms"]) for r in led if r["p99_ms"] not in ("", "None")]
    mon = [r for f in glob.glob(f"{d}/monitor2-*.csv") for r in csv.DictReader(open(f))]
    # THE LAST MEASURE_LAST SECONDS (2026-10-20), next to the whole run: CAPSys (EuroSys'25 §6.1)
    # collects 10 min of metrics after a 6-min warm-up, i.e. a placement's STEADY state. The
    # whole-run figure is still the headline for a monitor — it pays its exploration there — but
    # the window is what compares with the literature.
    t_end = float(end["elapsed_s"])
    first = min(led, key=lambda r: abs(float(r["elapsed_s"]) - (t_end - MEASURE_LAST)))
    w_off = float(end["offered_cum"]) - float(first["offered_cum"])
    w_ratio = (float(end["delivered_cum"]) - float(first["delivered_cum"])) / w_off if w_off > 0 else None
    w_lats = [float(r["p50_ms"]) for r in led if r["p50_ms"] not in ("", "None")
              and float(r["elapsed_s"]) >= t_end - MEASURE_LAST]
    runs.append({"pass": int(p), "arm": arm, "ratio": min(1.0, ratio),
                 "w_ratio": w_ratio, "w_lat": sum(w_lats) / len(w_lats) if w_lats else None,
                 "lat": sum(lats) / len(lats) if lats else None,
                 "p99": sum(p99) / len(p99) if p99 else None,
                 "moves": sum(r["decision"] == "mover" for r in mon), "restarts": int(end["restarts"])})

arms = list(dict.fromkeys(r["arm"] for r in runs))
print(f"\nÚLTIMOS {MEASURE_LAST / 60:.0f} MIN de cada corrida (estado estable, como CAPSys):")
for a in arms:
    rs = [r for r in runs if r["arm"] == a and r["w_ratio"] is not None]
    if rs:
        wl = [r["w_lat"] for r in rs if r["w_lat"] is not None]
        print(f"  {a:8s} entrega {sum(r['w_ratio'] for r in rs) / len(rs):6.1%}  latencia p50 "
              f"{(sum(wl) / len(wl) / 1000) if wl else float('nan'):5.1f} s   "
              + "  ".join(f"{r['w_ratio']:.1%}" for r in sorted(rs, key=lambda r: r['pass'])))
print("\nTODA LA CORRIDA (incluye asentamiento, reinicios y exploración):")
print(f"\n{'brazo':8s} {'n':>2s} {'entrega':>9s} {'latencia p50':>13s} {'p99':>8s} {'cambios':>8s}   por pasada (entrega, toda la corrida)")
for a in arms:
    rs = [r for r in runs if r["arm"] == a]
    lat = [r["lat"] for r in rs if r["lat"] is not None]
    print(f"{a:8s} {len(rs):2d} {sum(r['ratio'] for r in rs) / len(rs):9.1%} "
          f"{(sum(lat) / len(lat) / 1000) if lat else float('nan'):11.1f} s "
          f"{(sum(x['p99'] for x in rs if x['p99']) / max(1, sum(1 for x in rs if x['p99'])) / 1000):6.1f} s "
          f"{sum(r['moves'] for r in rs) / len(rs):8.1f}   "
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
    for key, label in (("ratio", "entrega"), ("lat", "latencia p50"), ("p99", "latencia p99"),
                       ("w_ratio", f"entrega últimos {MEASURE_LAST / 60:.0f} min"),
                       ("w_lat", f"latencia p50 últimos {MEASURE_LAST / 60:.0f} min")):
        x = [r[key] for r in runs if r["arm"] == b and r[key] is not None]
        y = [r[key] for r in runs if r["arm"] == a and r[key] is not None]
        if len(x) >= 2 and len(y) >= 2:
            d = (sum(x) / len(x)) / (sum(y) / len(y)) - 1
            print(f"  {b} vs {a}: {label} {d:+.1%}  p={perm(x, y):.3f}  (n={len(x)} vs {len(y)})")
EOF
echo "  -> $OUT"
