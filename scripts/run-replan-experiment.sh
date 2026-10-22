#!/bin/bash
# ============================================
# Part B: one job at constant parallelism, a monitor that may re-place operators, and
# (optionally) a change in the cluster half-way through
# ============================================
#
# WHY (2026-10-12). The placement campaigns apply a plan by rescaling 3 -> 2 on a schedule,
# which costs two restarts per epoch and redistributes key groups each time. With the fork's
# replan trigger (ThesisReplanTrigger) a plan can be applied at unchanged parallelism, so part B
# runs ONE job, rescaled once to TARGET_PAR to bring every slot into the pool, and from then on
# only the monitor restarts it — when it decides a move pays.
#
# Arms:
#   MON    scripts/replan_monitor.py with the rule (option A)
#   NOMON  the same script with --observe-only: v4's plan applied once at the start, never again;
#          identical observation overhead, so the difference is the monitor's decisions
#
# EVENT, optional: "<machine>:<limit>@<seconds>" — at that many seconds into the monitored run,
# cap that machine's disk (scripts/limit-disk.sh). E.g. "medium:5M@1800" takes the disk away from
# where v4 puts the joins: the scenario in which a fixed placement cannot recover and a monitor
# should. Several events separated by spaces.
#
# Usage:
#   ARM=MON  DIST="SINE:3600:20000,40000" scripts/run-replan-experiment.sh
#   ARM=NOMON EVENT="medium:5M@1800" scripts/run-replan-experiment.sh
#
# PRE-REQUISITE (once): slot.idle.timeout long enough that the pool keeps its 12 slots for the
# whole run (default 300 s would release the 4 surplus ones and shrink the monitor's choices):
#   scripts/patch-flink-property.sh jobmanager slot.idle.timeout 86400000
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-flink}"
NODE="${THESIS_NODE:-minikube}"

ARM="${ARM:-MON}"
QUERY="${QUERY:-q8}"
RATE="${RATE:-40000}"
DIST="${DIST:-SINE:3600:20000,40000}"
DURATION="${DURATION:-5400}"          # seconds the monitor runs, after the job is at TARGET_PAR
EVENT="${EVENT:-}"
INITIAL_DISK="${INITIAL_DISK:-fast:5M}"
# v4's table for a query: the one trained on it with CPU 2/1/0.5 at its campaign rate
# (scripts/run-trainings.sh). The default is the run's OWN query; V4_Q<n> arms use another's.
table_for() { ls "$ROOT_DIR"/results/characterizer/"$1"-v4-cpu2105-r*-ucb.json 2>/dev/null | head -1 \
    | sed "s|^$ROOT_DIR/||"; }
QTABLE="${QTABLE:-$(table_for "$QUERY")}"
QTABLE="${QTABLE:-results/characterizer/q8-v4-20261008.json}"
BASE=v4
INTERVAL="${INTERVAL:-60}"
WINDOW="${WINDOW:-45}"
WHEN="${WHEN:-now}"
MONITOR_ARGS="${MONITOR_ARGS:-}"

MODEL="${MODEL:-}"
# The three arms of the part-B comparison (2026-10-13) all run scripts/plan_bandit.py, so every arm
# is measured by the same loop with the same windows:
#   STOCK   the fork's STOCK arm (Flink's own placement), measured only
#   V4      v4's plan applied once at the start, then measured only (no monitor)
#   BANDIT  v4's plan at the start, then the online bandit (v2)
# MON / NOMON are the earlier rule-based monitor (scripts/replan_monitor.py), kept for reference.
FORK_ARM=RL
case "$ARM" in
    STOCK) BANDIT_MODE=stock; FORK_ARM=STOCK ;;
    # Round Robin (2026-10-19): the fork's ROUND_ROBIN arm, measured only — the second baseline of
    # the retrained-v4 comparison (STOCK / RR / V4), alongside Flink's own placement.
    RR) BANDIT_MODE=stock; FORK_ARM=ROUND_ROBIN ;;
    # Controls (2026-10-21). STOCK_BANDIT: the bandit over Flink's own placement — no v4 plan,
    # no v4 candidate — to show the monitor works over any base. V4_PRIOR: v4 with an EMPTY table,
    # i.e. its prior alone (heavy work to the fastest machine with room): the reference against
    # which a trained table that transfers badly shows as worse than not learning (overfitting).
    # V4_Q<n>: v4 with the table trained on query Q<n>, for the cross-query generalisation matrix.
    STOCK_BANDIT) BANDIT_MODE=bandit; FORK_ARM=STOCK; BASE=stock ;;
    V4_PRIOR) BANDIT_MODE=v4; QTABLE=none ;;
    V4_PRIOR_BANDIT) BANDIT_MODE=bandit; QTABLE=none ;;
    V4_Q3|V4_Q5|V4_Q6|V4_Q8|V4_Q11)
        BANDIT_MODE=v4
        QTABLE=$(table_for "$(echo "${ARM#V4_}" | tr 'Q' 'q')")
        [ -n "$QTABLE" ] || { echo "ERROR: no hay tabla entrenada para ${ARM#V4_}" >&2; exit 2; } ;;
    V4) BANDIT_MODE=v4 ;;
    BANDIT) BANDIT_MODE=bandit ;;
    MON) MODE_ARGS="" ;;
    NOMON) MODE_ARGS="--observe-only" ;;
    *) echo "ERROR: ARM debe ser STOCK, RR, V4, BANDIT, STOCK_BANDIT, V4_PRIOR, V4_PRIOR_BANDIT o V4_Q<n> (o MON / NOMON)" >&2; exit 2 ;;
esac
[ "$QTABLE" = none ] || [ -f "$ROOT_DIR/$QTABLE" ] || [ -f "$QTABLE" ] || { echo "ERROR: no existe $QTABLE" >&2; exit 2; }

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT_DIR/results/replan-experiment/$STAMP-$ARM"
mkdir -p "$OUT"
LOG="$OUT/driver.log"
say() { echo "$@" | tee -a "$LOG"; }

STALE=$(pgrep -f "scripts/(arm_controller|characterizer_agent|agent_v5_loop|replan_monitor|plan_bandit)\.py" | tr '\n' ' ')
if [ -n "$STALE" ]; then
    echo "ERROR: procesos de una corrida anterior siguen vivos: $STALE" >&2
    echo "  pkill -f 'scripts/(arm_controller|characterizer_agent|agent_v5_loop|replan_monitor|plan_bandit).py'" >&2
    exit 1
fi

JM_POD=$(kubectl get pods -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running -o jsonpath='{.items[-1:].metadata.name}')
IDLE=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- curl -s -m 20 localhost:8081/jobmanager/config \
    2>/dev/null | python3 -c "
import json, sys
print({e['key']: e['value'] for e in json.load(sys.stdin)}.get('slot.idle.timeout', '?'))" 2>/dev/null)
if [ "${IDLE:-?}" != "?" ] && [ "$IDLE" -lt $(( (DURATION + 1800) * 1000 )) ] 2>/dev/null; then
    say "! slot.idle.timeout=$IDLE ms es menor que la corrida: el pool soltará los slots sobrantes."
    say "  scripts/patch-flink-property.sh jobmanager slot.idle.timeout 86400000"
    exit 1
fi

say "=========================================="
say "  Parte B — $ARM   $QUERY a $RATE ($DIST)"
say "  monitoreo ${DURATION}s, cada ${INTERVAL}s (ventana ${WINDOW}s), replan '$WHEN'"
say "  disco inicial: $INITIAL_DISK   eventos: ${EVENT:-ninguno}"
say "  brazo del fork: $FORK_ARM   tabla v4: $QTABLE${MODEL:+   modelo del bandit: $MODEL}"
say "  ventana de la query: ${JOB_WINDOW:-por defecto}"
say "  -> $OUT"
say "=========================================="

# Clean slate: no plan (the fork falls back to LPT at the first rescale; the monitor then applies
# v4's plan), no pending replan request, no checkpoints or multipart leftovers.
docker exec "$NODE" sh -c "rm -f /var/thesis/assignment /var/thesis/replan; \
    rm -rf /var/thesis/minio/flink-checkpoints/checkpoints/* /var/thesis/minio/.minio.sys/multipart/*" \
    </dev/null >/dev/null 2>&1
"$SCRIPT_DIR/publish-arm.sh" "$FORK_ARM" >>"$LOG" 2>&1
for m in fast medium slow; do "$SCRIPT_DIR/limit-disk.sh" "$m" off >>"$LOG" 2>&1; done
for spec in $INITIAL_DISK; do
    "$SCRIPT_DIR/limit-disk.sh" "${spec%%:*}" "${spec##*:}" 2>&1 | tee -a "$LOG"
done

# One job, submitted wide so all twelve slots enter the pool, rescaled once to TARGET_PAR.
# Submitted wide so every slot enters the pool: Q11 has three stages, and at 3 it asks for only 9
# slots — the pool can then come up without fast (2026-10-20). At 4 it asks for all 12.
SUBMIT_PAR="${SUBMIT_PAR:-$([ "$QUERY" = q11 ] && echo 4 || echo 3)}"
KEEP=1 QUERY="$QUERY" RATE="$RATE" DIST="$DIST" DURATION=$(( DURATION + 1800 )) JOB_WINDOW="${JOB_WINDOW:-10}" \
    SUBMIT_PAR="$SUBMIT_PAR" \
    SETTLE=10 SAMPLES=1 INTERVAL=1 "$SCRIPT_DIR/profile-job.sh" 2>&1 | tail -4 | tee -a "$LOG"

T0=$(date +%s)
if [ -n "${BANDIT_MODE:-}" ]; then
    timeout $(( DURATION + 300 )) python3 -u "$SCRIPT_DIR/plan_bandit.py" --mode "$BANDIT_MODE" --base "$BASE" \
        --jm-pod "$JM_POD" --qtable "$QTABLE" --rate "$RATE" --dist "$DIST" \
        --duration "$DURATION" ${MODEL:+--model "$MODEL"} \
        --out-dir "$OUT" $MONITOR_ARGS \
        < /dev/null > "$OUT/monitor.log" 2>&1 &
else
    timeout $(( DURATION + 300 )) python3 -u "$SCRIPT_DIR/replan_monitor.py" \
        --jm-pod "$JM_POD" --qtable "$QTABLE" --rate "$RATE" --dist "$DIST" \
        --duration "$DURATION" --interval "$INTERVAL" --window "$WINDOW" --when "$WHEN" \
        --out-dir "$OUT" $MODE_ARGS $MONITOR_ARGS \
        < /dev/null > "$OUT/monitor.log" 2>&1 &
fi
MON_PID=$!
say "  monitor pid $MON_PID -> $OUT/monitor.log"
# THE WHOLE-RUN LEDGER (2026-10-13): what the job processed over the entire run, settling and
# restarts included, independently of the monitor — the basis of the evaluation (run_ledger.py).
# The deadline is generous on purpose (2026-10-19): `timeout` counts MONOTONIC seconds and the
# ledger counts wall-clock ones, and on this WSL2 host the monotonic clock runs ~8.4% fast — with
# DURATION + 120 every ledger was killed at wall-clock 1770 s of 1800, before printing its summary.
timeout $(( DURATION * 13 / 10 + 120 )) python3 -u "$SCRIPT_DIR/run_ledger.py" --jm-pod "$JM_POD" \
    --rate "$RATE" --dist "$DIST" --duration "$DURATION" --out "$OUT/ledger.csv" \
    < /dev/null > "$OUT/ledger.log" 2>&1 &
LEDGER_PID=$!

# Events, in time order, relative to the monitor's start.
for ev in $(echo "$EVENT" | tr ' ' '\n' | sort -t@ -k2 -n); do
    at=${ev##*@}; what=${ev%@*}
    while [ $(( $(date +%s) - T0 )) -lt "$at" ] && kill -0 "$MON_PID" 2>/dev/null; do sleep 5; done
    say "  [$(date +%H:%M:%S)] EVENTO t+${at}s: disco de ${what%%:*} -> ${what##*:}"
    echo "$(( $(date +%s) - T0 )) $what" >> "$OUT/events.txt"
    "$SCRIPT_DIR/limit-disk.sh" "${what%%:*}" "${what##*:}" 2>&1 | tee -a "$LOG"
done

wait "$MON_PID"
wait "$LEDGER_PID" 2>/dev/null
say "  monitor terminado"
cat "$OUT/ledger.log" | tee -a "$LOG"

JID=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink list 2>/dev/null | awk '/RUNNING/ {print $4; exit}')
[ -n "$JID" ] && kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel "$JID" >/dev/null 2>&1 \
    && say "  job $JID cancelado"
for m in fast medium slow; do "$SCRIPT_DIR/limit-disk.sh" "$m" off >/dev/null 2>&1; done
kubectl logs -n "$NAMESPACE" "$JM_POD" --since=$(( DURATION + 1800 ))s 2>/dev/null \
    | grep -E "THESIS_REPLAN|THESIS_ASSIGN" > "$OUT/thesis-assign.log"

python3 - "$OUT" <<'EOF' | tee -a "$LOG"
import csv, glob, sys
out = sys.argv[1]
rows = [r for f in glob.glob(f"{out}/monitor-*.csv") + glob.glob(f"{out}/bandit-*.csv")
        + glob.glob(f"{out}/monitor2-*.csv") for r in csv.DictReader(open(f))]
if not rows:
    sys.exit("sin registros del monitor")
events = [(int(l.split()[0]), l.split()[1]) for l in open(f"{out}/events.txt")] \
    if glob.glob(f"{out}/events.txt") else []
def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")
def summary(label, rs):
    if rs:
        lats = [float(r['latency_ms']) for r in rs if r.get('latency_ms') not in (None, '', 'None')]
        print(f"  {label:22s} {len(rs):3d} ventanas  entrega {mean([float(r['ratio']) for r in rs]):6.1%}"
              f"  (<90%: {sum(float(r['ratio']) < 0.9 for r in rs)})"
              + (f"  latencia p50 media {mean(lats) / 1000:.1f} s" if lats else ""))
print("\nRESUMEN")
summary("toda la corrida", rows)
cut = events[0][0] if events else None
if cut is not None:
    summary("antes del evento", [r for r in rows if int(r["elapsed_s"]) < cut])
    summary("después del evento", [r for r in rows if int(r["elapsed_s"]) >= cut])
moves = [r for r in rows if r["decision"] == "mover"]
print(f"  replanificaciones: {len(moves)}")
for r in moves:
    print(f"    t+{r['elapsed_s']}s  {r.get('reason') or r.get('why')}")
EOF
say "  -> $OUT"
