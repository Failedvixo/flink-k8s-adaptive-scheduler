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
QTABLE="${QTABLE:-results/characterizer/q8-v4-20261008.json}"
INTERVAL="${INTERVAL:-60}"
WINDOW="${WINDOW:-45}"
WHEN="${WHEN:-now}"
MONITOR_ARGS="${MONITOR_ARGS:-}"

case "$ARM" in
    MON) MODE_ARGS="" ;;
    NOMON) MODE_ARGS="--observe-only" ;;
    *) echo "ERROR: ARM debe ser MON o NOMON" >&2; exit 2 ;;
esac
[ -f "$ROOT_DIR/$QTABLE" ] || [ -f "$QTABLE" ] || { echo "ERROR: no existe $QTABLE" >&2; exit 2; }

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT_DIR/results/replan-experiment/$STAMP-$ARM"
mkdir -p "$OUT"
LOG="$OUT/driver.log"
say() { echo "$@" | tee -a "$LOG"; }

STALE=$(pgrep -f "scripts/(arm_controller|characterizer_agent|agent_v5_loop|replan_monitor)\.py" | tr '\n' ' ')
if [ -n "$STALE" ]; then
    echo "ERROR: procesos de una corrida anterior siguen vivos: $STALE" >&2
    echo "  pkill -f 'scripts/(arm_controller|characterizer_agent|agent_v5_loop|replan_monitor).py'" >&2
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
say "  tabla v4: $QTABLE"
say "  -> $OUT"
say "=========================================="

# Clean slate: no plan (the fork falls back to LPT at the first rescale; the monitor then applies
# v4's plan), no pending replan request, no checkpoints or multipart leftovers.
docker exec "$NODE" sh -c "rm -f /var/thesis/assignment /var/thesis/replan; \
    rm -rf /var/thesis/minio/flink-checkpoints/checkpoints/* /var/thesis/minio/.minio.sys/multipart/*" \
    </dev/null >/dev/null 2>&1
"$SCRIPT_DIR/publish-arm.sh" RL >>"$LOG" 2>&1
for m in fast medium slow; do "$SCRIPT_DIR/limit-disk.sh" "$m" off >>"$LOG" 2>&1; done
for spec in $INITIAL_DISK; do
    "$SCRIPT_DIR/limit-disk.sh" "${spec%%:*}" "${spec##*:}" 2>&1 | tee -a "$LOG"
done

# One job, submitted wide so all twelve slots enter the pool, rescaled once to TARGET_PAR.
KEEP=1 QUERY="$QUERY" RATE="$RATE" DIST="$DIST" DURATION=$(( DURATION + 1800 )) \
    SETTLE=10 SAMPLES=1 INTERVAL=1 "$SCRIPT_DIR/profile-job.sh" 2>&1 | tail -4 | tee -a "$LOG"

T0=$(date +%s)
timeout $(( DURATION + 300 )) python3 -u "$SCRIPT_DIR/replan_monitor.py" \
    --jm-pod "$JM_POD" --qtable "$QTABLE" --rate "$RATE" --dist "$DIST" \
    --duration "$DURATION" --interval "$INTERVAL" --window "$WINDOW" --when "$WHEN" \
    --out-dir "$OUT" $MODE_ARGS $MONITOR_ARGS \
    < /dev/null > "$OUT/monitor.log" 2>&1 &
MON_PID=$!
say "  monitor pid $MON_PID -> $OUT/monitor.log"

# Events, in time order, relative to the monitor's start.
for ev in $(echo "$EVENT" | tr ' ' '\n' | sort -t@ -k2 -n); do
    at=${ev##*@}; what=${ev%@*}
    while [ $(( $(date +%s) - T0 )) -lt "$at" ] && kill -0 "$MON_PID" 2>/dev/null; do sleep 5; done
    say "  [$(date +%H:%M:%S)] EVENTO t+${at}s: disco de ${what%%:*} -> ${what##*:}"
    echo "$(( $(date +%s) - T0 )) $what" >> "$OUT/events.txt"
    "$SCRIPT_DIR/limit-disk.sh" "${what%%:*}" "${what##*:}" 2>&1 | tee -a "$LOG"
done

wait "$MON_PID"
say "  monitor terminado"

JID=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink list 2>/dev/null | awk '/RUNNING/ {print $4; exit}')
[ -n "$JID" ] && kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel "$JID" >/dev/null 2>&1 \
    && say "  job $JID cancelado"
for m in fast medium slow; do "$SCRIPT_DIR/limit-disk.sh" "$m" off >/dev/null 2>&1; done
kubectl logs -n "$NAMESPACE" "$JM_POD" --since=$(( DURATION + 1800 ))s 2>/dev/null \
    | grep -E "THESIS_REPLAN|THESIS_ASSIGN" > "$OUT/thesis-assign.log"

python3 - "$OUT" <<'EOF' | tee -a "$LOG"
import csv, glob, sys
out = sys.argv[1]
rows = [r for f in glob.glob(f"{out}/monitor-*.csv") for r in csv.DictReader(open(f))]
if not rows:
    sys.exit("sin registros del monitor")
events = [(int(l.split()[0]), l.split()[1]) for l in open(f"{out}/events.txt")] \
    if glob.glob(f"{out}/events.txt") else []
def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")
def summary(label, rs):
    if rs:
        print(f"  {label:22s} {len(rs):3d} ventanas  entrega {mean([float(r['ratio']) for r in rs]):6.1%}"
              f"  (<90%: {sum(float(r['ratio']) < 0.9 for r in rs)})")
print("\nRESUMEN")
summary("toda la corrida", rows)
cut = events[0][0] if events else None
if cut is not None:
    summary("antes del evento", [r for r in rows if int(r["elapsed_s"]) < cut])
    summary("después del evento", [r for r in rows if int(r["elapsed_s"]) >= cut])
moves = [r for r in rows if r["decision"] == "mover"]
print(f"  replanificaciones: {len(moves)}")
for r in moves:
    print(f"    t+{r['elapsed_s']}s  {r['reason']}")
EOF
say "  -> $OUT"
