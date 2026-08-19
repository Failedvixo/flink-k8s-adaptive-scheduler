#!/bin/bash
# ============================================
# Controlled placement experiment — isolates the arm from the rescale it rode in on
# ============================================
#
# WHY THIS EXISTS (measured 2026-08-06, results/fork-campaign/q11-sine):
# the autoscaler-driven campaign cannot answer "do the arms differ". Grouping its
# episodes by the rescale configuration they occurred in shows the reward is set by
# the CONFIGURATION, not the arm:
#
#     slices/freeSlots   n   reward   sd      arms
#     8/8                6   0.569    0.025   all six
#     2/6                3   0.438    0.005   FCFS, ROUND_ROBIN, LEAST_LOADED
#     2/8                3   0.411    0.022   STOCK, STOCK, ACO
#
# Between strata: 0.14. Between arms within a stratum: 0.005-0.025. And the global
# within-arm spread (0.0715) is 4.7x the between-arm spread (0.0151) — because the
# autoscaler handed each arm a different mix of configurations. The campaign's
# "spread(reward) = 0.0469, best=GA" is that imbalance, not a placement effect.
#
# WHAT THIS DOES DIFFERENTLY
#   1. A CONTROLLED SCHEDULE of transitions, identical for every arm, driven from
#      here instead of by the autoscaler, with lowerBound == upperBound so the
#      parallelism Flink lands on is not itself a variable. Every measured step is
#      forced while the pool still holds more slots than the job needs, which is
#      the only condition in which the assigner has a decision at all.
#   2. REPETITIONS, because one measurement per arm cannot beat the within-stratum
#      noise. n = REPS per arm at each width in the schedule.
#
# WHY ONE JOB PER ARM AND NOT ONE SHARED JOB (decided with the user, 2026-08-06):
# interleaving the arms inside a single job would control for drift (session state
# accumulating, JIT, GC) but would contaminate the very mechanism under study — the
# PREVIOUS placement is an INPUT to the assigner. PlacementInstance scores its
# locality term against previousAllocations, and STOCK delegates to
# StateLocalitySlotAssigner, which anchors each slice to the slot already holding its
# state. In a shared job, arm B would be deciding against the layout arm A left
# behind. One job per arm keeps the carryover INSIDE an arm (each repetition inherits
# that same arm's previous placement — a consistent condition) and gives every arm an
# identical initial condition.
#
# Reward measurement is NOT reimplemented: arm_controller.py runs in --observe mode
# (records, publishes nothing, writes no Q-table) so the numbers come from the exact
# code the campaign and the meta-schedulers use, and it credits the arm the
# JobManager LOGGED rather than the one requested.
#
# Requires: JobManager on the fork (scripts/deploy-thesis-fork.sh) and the REST
# port-forward (kubectl port-forward -n flink svc/flink-jobmanager 8081:8081).
#
#   scripts/run-placement-experiment.sh              # ~45 min, n=3, one width
#   REPS=8 scripts/run-placement-experiment.sh       # ~2 h at one width
#
#   # down, then UP, then down again — three widths per repetition
#   REPS=4 SCHEDULE="4* 6* 2* 8" scripts/run-placement-experiment.sh
#
#   QUERY=q5 REPS=5 scripts/run-placement-experiment.sh
#
# Results: results/placement-experiment/<timestamp>/<ARM>/
# Analyse:  python3 scripts/analyse_placement_experiment.py <that dir>

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR" || exit 1

ARMS="${ARMS:-STOCK FCFS ROUND_ROBIN LEAST_LOADED LPT ACO GA}"
QUERY="${QUERY:-q11}"
# Measurements of the SAME transition per arm. Three fits the ~45 min of the earlier
# pilot; with a within-stratum sd of ~0.02, n=8 is what separates arms differing by
# less than 0.03.
REPS="${REPS:-3}"
# The pool holds SUBMIT_PAR slots; the job is then narrowed to TARGET_PAR, so the
# assigner sees freeSlots(SUBMIT_PAR) > slices(TARGET_PAR) — the only condition in
# which it has a decision at all (SlotSharingSlotAllocator.determineVertexParallelism).
SUBMIT_PAR="${SUBMIT_PAR:-8}"
TARGET_PAR="${TARGET_PAR:-4}"
# The sequence of parallelisms each repetition walks through, `*` marking the
# steps that are MEASURED (held long enough for warmup+window). Unmarked steps
# are transitions only, held just long enough to take effect.
#
# The assigner has a decision whenever the target lands BELOW the slots the pool
# still holds, which is why the default measures a narrowing and then restores.
# That is not "only scaling up" — the measured event is a scale-DOWN, and the
# restore exists to put the slots back. An UP step also gets a decision as long
# as it lands under the retained slot count, so a schedule like
#
#     SCHEDULE="4* 6* 2* 8"
#
# measures three: down 8->4, UP 4->6, and down 6->2, before restoring to 8. The
# up step is worth having on its own terms — its new subtasks carry no prior
# state, so the locality half of the cost function is exercised differently than
# on the way down. Each distinct width lands in its own stratum in the analysis.
SCHEDULE="${SCHEDULE:-${TARGET_PAR}* ${SUBMIT_PAR}}"
MEASURE_WARMUP="${WARMUP:-30}"
MEASURE_WINDOW="${WINDOW:-30}"
# How often the observer samples during the warmup, which is what sets the resolution
# of the rescale-cost columns (restart_gap_s, recovery_s, rescale_deficit_events).
# The observer's default of 5s is fine for detecting a new placement but far too
# coarse for a transient that lasts a handful of seconds. One second costs one cheap
# REST call per second and also makes the epoch boundary itself more accurate.
RECOVERY_INTERVAL="${RECOVERY_INTERVAL:-1}"
# CONSTANT by default: a shaped arrival rate is a nuisance variable here. The question
# is which placement is better, not how each arm copes with a rate change.
DIST="${DIST:-CONSTANT}"
RATE="${RATE:-60000}"
TM_REPLICAS="${TM_REPLICAS:-5}"
# Which TaskManager deployments make up the cluster, as "<deployment>:<replicas>" pairs.
# One uniform pool by default, which is exactly what every run before 2026-08-18 used.
#
# A heterogeneous cluster cannot be one deployment: replicas share a pod template, so a
# per-machine CPU limit needs a deployment per speed class. Point this at them to run the
# campaign on unequal machines — the setting the offline bench identifies as the one that
# stops every arm from tying (9-14% at 4 slices, against 0.0% on identical TaskManagers):
#
#   TM_DEPLOYMENTS="flink-tm-fast:1 flink-tm-medium:1 flink-tm-slow:1"
#
# Set up the classes and publish the speed vector with scripts/set-taskmanager-classes.sh
# --heterogeneous first; this only holds the cluster at the shape the campaign assumes.
TM_DEPLOYMENTS="${TM_DEPLOYMENTS:-flink-taskmanager:$TM_REPLICAS}"
# TM_REPLICAS describes the uniform pool only, so a multi-deployment cluster made it record
# a TaskManager count the run never had (both 2026-08-18 heterogeneous campaigns stamped 5
# while running on 3). The cluster size is whatever TM_DEPLOYMENTS actually asks for.
TM_REPLICAS=$(echo "$TM_DEPLOYMENTS" | tr ' ' '\n' | awk -F: '{n += $2} END {print n + 0}')
NAMESPACE=flink

# ------------------------------------------------------------------
# workload
# ------------------------------------------------------------------
# WHY THERE IS A SECOND JOB (2026-08-12): every Nexmark query measured so far
# partitions on a key the generator draws uniformly, and with slot sharing each
# slice packs one subtask of every vertex — so all slices carry the SAME load and
# every placement with the same distribution is equivalent BY SYMMETRY. There was
# nothing for an arm to optimise, which is the structural reason three controlled
# runs found nothing.
#
# ConfigurableGraphJob fixes that at the root: its CPU-load operator has its own
# parallelism, independent of the global one. With PIN_PARALLELISM=2 and a global
# width of 4, slices 0-1 carry a CPU-load subtask and slices 2-3 do not — the
# slices become genuinely heterogeneous, by a factor set through CPU_LOAD. Placing
# the two heavy slices on one TaskManager or splitting them across two is then a
# large, arithmetically obvious difference, which is what a placement policy is
# supposed to decide.
#
# It also sidesteps the OOM that killed arms under q11: its window keys on
# auctionId bounded by maxAuctionId (500 by default), so the state is small and
# fixed rather than one session per distinct bidder.
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.NexmarkRealJob}"
CPU_LOAD="${CPU_LOAD:-2500}"
MAX_EVENT_AGE="${MAX_EVENT_AGE:-15000}"
JOB_WINDOW="${JOB_WINDOW:-10}"
# Vertices matching this name pattern keep their parallelism through every
# rescale. Leaving it empty scales everything, which is what the Nexmark runs did
# — and precisely what flattens the slices into interchangeability.
PIN_VERTEX="${PIN_VERTEX:-}"
PIN_PARALLELISM="${PIN_PARALLELISM:-2}"

# ------------------------------------------------------------------
# poisoning
# ------------------------------------------------------------------
# Measured 2026-08-17: stock Flink placed as well as every fork arm — but not by
# deciding well. StateLocalitySlotAssigner anchors each slice to the slot already
# holding its state, so it INHERITS the previous layout. In the plain schedule the
# previous layout is the one at SUBMIT_PAR, where the slices barely fit and are
# spread by force, so stock inherits a good arrangement and keeps it.
#
# That makes the comparison unable to separate "decides well" from "copied
# something already good". Poisoning fixes it: before measuring, walk the job
# through a deliberately BAD layout so that state anchors there. Then stock's
# locality pulls towards the bad arrangement while a load-aware arm has to
# override it — which is the conflict the thesis is actually about.
#
# The bad layout is produced by an arm that concentrates the expensive slices, with
# the published per-vertex weights temporarily disabled (that is what made ACO/GA
# concentrate before the weights existed). Set POISON_ARM empty to skip.
# ------------------------------------------------------------------
# capacity loss and recovery
# ------------------------------------------------------------------
# The scenario that targets what stock Flink is structurally blind to.
# StateLocalitySlotAssigner maximises ONE thing — keeping each slice on the slot
# already holding its state — and has no notion that slices can cost different
# amounts. So it preserves whatever layout it inherits, including a bad one.
#
# This induces the bad layout the way a cluster does it, not the way an
# experimenter would: capacity disappears. Scaled down to DRAIN_REPLICAS
# TaskManagers the job has nowhere to spread, so the expensive slices end up
# together BY PHYSICS, not by any policy's choice — which is what keeps this from
# being a rigged starting condition. Capacity then comes back, and the next
# rescale asks each arm the real question: do you redistribute, or do you keep
# what you inherited? A TaskManager restarting and rejoining is the most ordinary
# event a cluster has.
#
# DRAIN_PAR must fit in DRAIN_REPLICAS × slots-per-TM, or the job cannot run
# while the capacity is gone. With one TaskManager and two slots, a width of 2
# puts both CPU-load subtasks on the same machine with no ambiguity at all.
DRAIN_REPLICAS="${DRAIN_REPLICAS:-0}"
# What the cluster shrinks to, in the same "<deployment>:<replicas>" form as
# TM_DEPLOYMENTS. On a heterogeneous cluster this is where the scenario gets its teeth:
# leaving only the SLOW machine makes the inherited layout genuinely bad, so the arm that
# refuses to redistribute keeps paying for it after capacity returns —
#   DRAIN_DEPLOYMENTS="flink-tm-fast:0 flink-tm-medium:0 flink-tm-slow:1"
DRAIN_DEPLOYMENTS="${DRAIN_DEPLOYMENTS:-flink-taskmanager:$DRAIN_REPLICAS}"
# Measure the per-vertex weights once on the first arm and hold them for the campaign.
PUBLISH_LOADS="${PUBLISH_LOADS:-1}"
LOADS_PUBLISHED=0
DRAIN_PAR="${DRAIN_PAR:-2}"
DRAIN_HOLD="${DRAIN_HOLD:-60}"
RESTORE_HOLD="${RESTORE_HOLD:-60}"

POISON_ARM="${POISON_ARM:-}"
POISON_SCHEDULE="${POISON_SCHEDULE:-$TARGET_PAR}"
POISON_HOLD="${POISON_HOLD:-45}"

# The measured (narrow) phase must outlast the controller's warmup+window plus its
# 5s poll and the rescale itself. The restore (wide) phase only has to put the slots
# back; the episode the controller records there lands in a different stratum and the
# analysis drops it.
NARROW_HOLD=$((MEASURE_WARMUP + MEASURE_WINDOW + 20))
WIDE_HOLD="${WIDE_HOLD:-35}"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT_ROOT="${OUT_ROOT:-results/placement-experiment/$STAMP}"
mkdir -p "$OUT_ROOT"

query_job_args() { echo "$1 ${ZIPF_ALPHA:-0.5} ${HOT_POOL:-1000}"; }
query_heavy_vertex() {
    case "$1" in
        q0) echo "q0-passthrough" ;;      q1) echo "q1-currency" ;;
        q2) echo "q2-selection" ;;        q3) echo "q3-state-join" ;;
        q4) echo "q4-cat-avg" ;;          q5) echo "hot-items-count" ;;
        q6) echo "q6-seller-avg" ;;       q7) echo "q7-max-bid" ;;
        q8) echo "new-users-join" ;;      q9) echo "q9-winning-bid" ;;
        q10) echo "q10-sink" ;;           q11) echo "q11-sessions" ;;
        q12) echo "q12-proc-sessions" ;;  *) echo "" ;;
    esac
}

# ------------------------------------------------------------------
# preconditions
# ------------------------------------------------------------------
if ! kubectl get nodes >/dev/null 2>&1; then
    echo "ERROR: cluster unreachable (minikube start)" >&2; exit 1
fi
JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')
[ -n "$JM_POD" ] || { echo "ERROR: no running JobManager" >&2; exit 1; }

FORK=$(kubectl get pod -n "$NAMESPACE" "$JM_POD" \
    -o jsonpath='{.spec.containers[0].env[?(@.name=="THESIS_SLOT_ASSIGNER")].value}')
[ "$FORK" = "true" ] || { echo "ERROR: JobManager is not on the fork (scripts/deploy-thesis-fork.sh)" >&2; exit 1; }

# ------------------------------------------------------------------
# REST access
# ------------------------------------------------------------------
# The driver never touches the host port-forward: `kubectl port-forward` dies on a
# connection reset and leaves the PROCESS alive with a dead tunnel, so every curl
# silently returns nothing. A 2 h run lost 4 of 6 arms to exactly that on
# 2026-08-11 — the jobs were submitted and running fine, the script just could not
# see them, waited out its RUNNING timeout and skipped the arm. Job control now
# goes through the JobManager pod itself.
jm_curl() {
    kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
        curl -s -m 15 "http://localhost:8081$1" 2>/dev/null
}

# arm_controller.py does still need a reachable HTTP endpoint, so the tunnel is
# kept alive here rather than being assumed. Any stale forwarder is replaced: one
# with a dead tunnel is worse than none, because it answers the port.
ensure_port_forward() {
    curl -s -m 3 http://localhost:8081/overview >/dev/null 2>&1 && return 0
    # Anchored at kubectl so the pattern can never match the shell that is running
    # this script (or the watchdog subshell), only a real forwarder.
    pkill -f "^kubectl port-forward.*flink-jobmanager.*8081" >/dev/null 2>&1
    sleep 1
    nohup kubectl port-forward -n "$NAMESPACE" svc/flink-jobmanager 8081:8081 \
        >> "$OUT_ROOT/port-forward.log" 2>&1 &
    for _ in $(seq 1 20); do
        sleep 1
        curl -s -m 3 http://localhost:8081/overview >/dev/null 2>&1 && return 0
    done
    return 1
}

if ! ensure_port_forward; then
    echo "ERROR: could not establish the REST port-forward to the JobManager" >&2
    exit 1
fi

# Re-check often enough that the controller loses at most one poll to a reset.
( while true; do
      sleep 15
      curl -s -m 3 http://localhost:8081/overview >/dev/null 2>&1 || ensure_port_forward
  done ) &
PF_WATCHDOG=$!
trap 'kill "$PF_WATCHDOG" 2>/dev/null' EXIT

# An arm measured against a different TM count is not comparable with the others.
scale_taskmanagers() {
    # "<deployment>:<replicas> ..." — scales only what actually differs, so the common
    # case costs one read per deployment and no rollout wait.
    local spec
    for spec in $1; do
        local deployment="${spec%%:*}"
        local replicas="${spec##*:}"
        local current
        current=$(kubectl get deploy "$deployment" -n "$NAMESPACE" \
            -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")
        if [ -z "$current" ]; then
            echo "  ! deployment $deployment does not exist" >&2
            continue
        fi
        if [ "$current" != "$replicas" ]; then
            echo "  scaling $deployment $current -> $replicas"
            kubectl scale deployment "$deployment" -n "$NAMESPACE" --replicas="$replicas" >/dev/null
            kubectl rollout status "deployment/$deployment" -n "$NAMESPACE" --timeout=180s >/dev/null
        fi
    done
}
scale_taskmanagers "$TM_DEPLOYMENTS"

LOCAL_JAR="$ROOT_DIR/flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar"
[ -f "$LOCAL_JAR" ] || { echo "ERROR: build the benchmark first (cd flink-nexmark-job && mvn package)" >&2; exit 1; }
echo "Uploading benchmark jar to $JM_POD..."
kubectl cp "$LOCAL_JAR" "$NAMESPACE/$JM_POD:/tmp/nexmark.jar"

[ -n "$(query_heavy_vertex "$QUERY")" ] || { echo "ERROR: unknown query '$QUERY'" >&2; exit 1; }

SLOT_IDLE_TIMEOUT=$(jm_curl "/jobmanager/config" | python3 -c "
import json, sys
cfg = {e['key']: e['value'] for e in json.load(sys.stdin)}
print(cfg.get('slot.idle.timeout', 'default'))" 2>/dev/null || echo unknown)
export SLOT_IDLE_TIMEOUT


# One cycle is the whole schedule, so the job must outlast REPS of it — the
# generator stops itself at this duration and a short job would truncate the last
# repetitions of every arm.
CYCLE=0
MEASURED_STEPS=0
for STEP in $SCHEDULE; do
    case "$STEP" in
        *\*) CYCLE=$((CYCLE + NARROW_HOLD)); MEASURED_STEPS=$((MEASURED_STEPS + 1)) ;;
        *)   CYCLE=$((CYCLE + WIDE_HOLD)) ;;
    esac
done
if [ "$MEASURED_STEPS" -eq 0 ]; then
    echo "ERROR: SCHEDULE has no measured step — mark at least one with '*'" >&2; exit 1
fi
POISON_COST=0
for PSTEP in $POISON_SCHEDULE; do POISON_COST=$((POISON_COST + POISON_HOLD)); done
[ -n "$POISON_ARM" ] || POISON_COST=0
DRAIN_COST=0
[ "$DRAIN_REPLICAS" -gt 0 ] 2>/dev/null && DRAIN_COST=$((DRAIN_HOLD + RESTORE_HOLD + 120))
JOB_DURATION=$(( REPS * CYCLE + POISON_COST + DRAIN_COST + 180 ))
NARMS=$(echo "$ARMS" | wc -w)

echo "=========================================="
echo "  Controlled placement experiment"
echo "  workload:   ${JOB_CLASS##*.}${PIN_VERTEX:+   pinned: /$PIN_VERTEX/ at $PIN_PARALLELISM}"
echo "  query:      $QUERY   dist: $DIST   rate: $RATE"
echo "  arms:       $ARMS"
echo "  submit at:  p=$SUBMIT_PAR"
echo "  schedule:   $SCHEDULE   (* = measured; $MEASURED_STEPS per rep)"
echo "  reps:       $REPS per arm, one job per arm  -> n=$REPS per arm per width"
echo "  measure:    warmup ${MEASURE_WARMUP}s + window ${MEASURE_WINDOW}s"
echo "  slot.idle.timeout: ${SLOT_IDLE_TIMEOUT} ms   (holds the pool geometry still)"
echo "  est. time:  ~$(( NARMS * (REPS * CYCLE + 90) / 60 )) min"
echo "  out:        $OUT_ROOT"
echo "=========================================="

cleanup_jobs() {
    kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink list 2>/dev/null |
        grep -oP '[0-9a-f]{32}' |
        xargs -r -I {} kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel {} >/dev/null 2>&1
}

# Pin every scalable vertex to exactly `target` (lowerBound == upperBound) so the
# parallelism Flink lands on is not itself a variable. Vertices declared at 1 stay at
# 1: in several queries that 1 is semantics (Q5's global top-N), not a choice.
set_parallelism() {
    local jid="$1" target="$2" label="$3" log="$4"
    local job_json payload code
    job_json=$(jm_curl "/jobs/$jid")
    payload=$(echo "$job_json" | TARGET="$target" PIN="$PIN_VERTEX" PINPAR="$PIN_PARALLELISM" python3 -c "
import json, os, re, sys
data = json.load(sys.stdin)
t = int(os.environ['TARGET'])
pin = os.environ.get('PIN', '')
pinpar = int(os.environ.get('PINPAR', '2'))
reqs = {}
for v in data.get('vertices', []):
    par = max(v.get('parallelism', 1), 1)
    name = v.get('name', '')
    if pin and re.search(pin, name, re.IGNORECASE):
        # Held at a width the rest of the graph does not share. That gap is what
        # makes some slices carry this operator and others not — scaling it with
        # everything else would make every slice identical again.
        reqs[v['id']] = {'parallelism': {'lowerBound': pinpar, 'upperBound': pinpar}}
    elif par > 1:
        reqs[v['id']] = {'parallelism': {'lowerBound': t, 'upperBound': t}}
    else:
        # Declared at 1 on purpose in several queries (Q5's global top-N): that 1
        # is semantics, not a performance choice.
        reqs[v['id']] = {'parallelism': {'lowerBound': par, 'upperBound': par}}
print(json.dumps(reqs))
" 2>/dev/null)
    if [ -z "$payload" ]; then
        echo "    ! could not build payload for $label" | tee -a "$log"; return 1
    fi
    code=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
        curl -s -o /dev/null -w "%{http_code}" -m 15 -X PUT \
        "http://localhost:8081/jobs/$jid/resource-requirements" \
        -H "Content-Type: application/json" -d "$payload" 2>/dev/null)
    echo "    [$label] PUT parallelism=$target -> HTTP $code" | tee -a "$log"
    case "$code" in
        200|202) ;;
        *) echo "    ! rescale not accepted (HTTP ${code:-none}) — this rep will be" \
                "measured in whatever configuration the job is already in" | tee -a "$log" ;;
    esac
}

CELL=0
for ARM in $ARMS; do
  CELL=$((CELL + 1))
  CELL_DIR="$OUT_ROOT/$ARM"
  mkdir -p "$CELL_DIR"
  DRIVER_LOG="$CELL_DIR/driver.log"

  echo ""
  echo "######################################################"
  echo "###  [$CELL/$NARMS] arm=$ARM"
  echo "######################################################"

  cleanup_jobs
  sleep 5

  "$SCRIPT_DIR/publish-arm.sh" "$ARM" >/dev/null || { echo "  ! publish failed"; continue; }
  echo "  published arm: $ARM"
  sleep 3

  # The first eight arguments are shared by both jobs (GraphConfig.fromArgs and
  # NexmarkRealJob deliberately mirror each other), and position 6 is the one that
  # diverges: the synthetic job reads it as the CPU-load operator's own
  # parallelism — the source of the slice asymmetry — while Nexmark reads it as
  # the heavy vertex's initial width.
  if [ "${JOB_CLASS##*.}" = "NexmarkRealJob" ]; then
      ARG6="$SUBMIT_PAR"
      TAIL_ARGS="$(query_job_args "$QUERY")"
      WHAT="$QUERY"
  else
      ARG6="$PIN_PARALLELISM"
      TAIL_ARGS=""
      WHAT="${JOB_CLASS##*.} (cpuLoad=${CPU_LOAD} iter/ev, cpuLoadPar=${PIN_PARALLELISM})"
  fi

  echo "  submitting $WHAT at parallelism $SUBMIT_PAR (duration ${JOB_DURATION}s)..."
  SUBMIT_OUT=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
      flink run -d -c "$JOB_CLASS" /tmp/nexmark.jar \
      "$RATE" "$JOB_DURATION" "$SUBMIT_PAR" "$JOB_WINDOW" "$CPU_LOAD" "$DIST" \
      "$ARG6" "$MAX_EVENT_AGE" $TAIL_ARGS 2>&1)
  JOB_ID=$(echo "$SUBMIT_OUT" | grep -oP '(?<=JobID )[0-9a-f]{32}' | head -1)
  [ -n "$JOB_ID" ] || JOB_ID=$(echo "$SUBMIT_OUT" | grep -oP '[0-9a-f]{32}' | head -1)
  if [ -z "$JOB_ID" ]; then
      echo "  ! could not submit:" | tee -a "$DRIVER_LOG"
      echo "$SUBMIT_OUT" >> "$DRIVER_LOG"; continue
  fi
  echo "$JOB_ID" > "$CELL_DIR/job-id.txt"
  echo "  JobID: $JOB_ID"

  echo -n "  waiting for RUNNING"
  STATE=""
  for _ in $(seq 1 60); do
      STATE=$(jm_curl "/jobs/$JOB_ID" |
          python3 -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
      [ "$STATE" = "RUNNING" ] && break
      echo -n "."; sleep 3
  done
  echo " $STATE"
  if [ "$STATE" != "RUNNING" ]; then
      echo "  ! job never reached RUNNING — skipping this arm" | tee -a "$DRIVER_LOG"
      cleanup_jobs; continue
  fi

  # Let the pipeline fill and the state reach a steady regime, so rep 1 is not
  # systematically colder than rep N.
  echo "  settling ${MEASURE_WARMUP}s before the first transition..."
  sleep "$MEASURE_WARMUP"

  # ---- per-vertex weights: measured ONCE, on the first arm, and then held fixed.
  #
  # The campaign used to inherit whatever was left in /var/thesis/loads from an earlier
  # session, which is two separate problems: the weights may describe a different job
  # entirely (every slice silently falls back to 1.0), and if they are re-measured per arm
  # then each arm is scored against a different weight vector — a difference in the
  # OBJECTIVE, not in the placement, which is exactly the kind of thing that makes arms
  # look different for the wrong reason.
  #
  # Measured on the first arm rather than before the loop because measuring needs a running
  # job, and the first arm has one. Set PUBLISH_LOADS=0 to keep whatever is already there.
  if [ "$PUBLISH_LOADS" = "1" ] && [ "$LOADS_PUBLISHED" = "0" ]; then
      echo "  measuring per-vertex load once for the whole campaign..."
      if "$SCRIPT_DIR/publish-loads.sh" >>"$DRIVER_LOG" 2>&1; then
          LOADS_PUBLISHED=1
          "$SCRIPT_DIR/publish-loads.sh" --read | sed 's/^/    /'
      else
          echo "  ! could not measure loads; every slice will weigh 1.0" | tee -a "$DRIVER_LOG"
          LOADS_PUBLISHED=1
      fi
  fi

  # ---- capacity loss and recovery, before the observer starts, so only the
  # recovery rescale is recorded as an episode and the compaction itself is not.
  if [ "$DRAIN_REPLICAS" -gt 0 ] 2>/dev/null; then
      echo "  draining to $DRAIN_DEPLOYMENTS at parallelism $DRAIN_PAR"
      set_parallelism "$JOB_ID" "$DRAIN_PAR" "drain p=$DRAIN_PAR" "$DRIVER_LOG"
      sleep 10
      scale_taskmanagers "$DRAIN_DEPLOYMENTS"
      sleep "$DRAIN_HOLD"

      DRAIN_STATE=$(jm_curl "/jobs/$JOB_ID" |
          python3 -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
      echo "    job is $DRAIN_STATE with capacity removed" | tee -a "$DRIVER_LOG"

      echo "  restoring the cluster to $TM_DEPLOYMENTS"
      scale_taskmanagers "$TM_DEPLOYMENTS"
      # The slots must be back in the pool before the measured rescale, or the
      # arm is asked to redistribute into capacity that does not exist yet.
      sleep "$RESTORE_HOLD"
      echo "  capacity restored; measuring $ARM from the compacted layout"
  fi

  # ---- poisoning: establish a deliberately bad layout BEFORE the observer starts,
  # so the poison rescales are never recorded as episodes and only the recovery is.
  if [ -n "$POISON_ARM" ]; then
      echo "  poisoning with $POISON_ARM (weights off) over: $POISON_SCHEDULE"
      "$SCRIPT_DIR/publish-loads.sh" --disable >/dev/null 2>&1 || true
      "$SCRIPT_DIR/publish-arm.sh" "$POISON_ARM" >/dev/null || true
      sleep 3
      for PSTEP in $POISON_SCHEDULE; do
          set_parallelism "$JOB_ID" "${PSTEP%\*}" "poison p=${PSTEP%\*}" "$DRIVER_LOG"
          sleep "$POISON_HOLD"
      done
      # Weights back on and the arm under test published, so the NEXT rescale is the
      # one being measured: it sees the poisoned previous allocation and either keeps
      # it (locality wins) or repairs it (load-aware balance wins).
      "$SCRIPT_DIR/publish-loads.sh" --enable >/dev/null 2>&1 || true
      "$SCRIPT_DIR/publish-arm.sh" "$ARM" >/dev/null || true
      sleep 3
      echo "  poisoned; measuring $ARM from here"
  fi

  timeout $((JOB_DURATION + 120)) python3 -u "$SCRIPT_DIR/arm_controller.py" \
      --observe --out-dir "$CELL_DIR" \
      --warmup "$MEASURE_WARMUP" --window "$MEASURE_WINDOW" \
      --poll-interval "$RECOVERY_INTERVAL" \
      > "$CELL_DIR/arm-controller.log" 2>&1 &
  CONTROLLER_PID=$!
  sleep 5

  for rep in $(seq 1 "$REPS"); do
      # THREE outcomes, not two. The old check grepped `flink list` for the id and
      # treated "no match" as "the job died" — but an unreachable API server also
      # produces no match, and on 2026-08-12 a `TLS handshake timeout` from the
      # kube-apiserver aborted a healthy arm that way. Asking for the job's state
      # and retrying separates "cannot ask" from "asked, and it is gone".
      JOB_STATE=""
      for attempt in 1 2 3; do
          JOB_STATE=$(jm_curl "/jobs/$JOB_ID" |
              python3 -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
          [ -n "$JOB_STATE" ] && break
          sleep 5
      done
      if [ -z "$JOB_STATE" ]; then
          echo "  ! cannot reach the JobManager at rep $rep (3 attempts) — treating as" \
               "infrastructure, not as a dead job, and carrying on" | tee -a "$DRIVER_LOG"
      elif [ "$JOB_STATE" != "RUNNING" ]; then
          echo "  ! job is $JOB_STATE at rep $rep — stopping this arm" | tee -a "$DRIVER_LOG"
          break
      fi
      echo "  --- rep $rep/$REPS"
      for STEP in $SCHEDULE; do
          case "$STEP" in
              *\*) TARGET="${STEP%\*}"; HOLD="$NARROW_HOLD"; KIND="measured" ;;
              *)   TARGET="$STEP";      HOLD="$WIDE_HOLD";   KIND="transition" ;;
          esac
          set_parallelism "$JOB_ID" "$TARGET" "rep$rep p=$TARGET $KIND" "$DRIVER_LOG"
          sleep "$HOLD"
      done
  done

  kill "$CONTROLLER_PID" 2>/dev/null
  wait "$CONTROLLER_PID" 2>/dev/null

  # Read the job graph BEFORE cancelling: once the job is gone the REST API keeps
  # only a stub.
  jm_curl "/jobs/$JOB_ID" > "$CELL_DIR/job-details.json"
  kubectl logs -n "$NAMESPACE" "$JM_POD" --since="$((JOB_DURATION + 200))s" 2>/dev/null |
      grep -E "THESIS_ASSIGN|THESIS_ARM" > "$CELL_DIR/thesis-assign.log"

  cleanup_jobs
  echo "  -> $CELL_DIR"
  sleep 10
done

# The speed vector in force is part of the experimental condition: the same arms on the
# same job mean something different on unequal machines, and a result recorded without it
# cannot be told apart from a homogeneous one later.
PUBLISHED_SPEEDS=$("$SCRIPT_DIR/publish-speeds.sh" --read 2>/dev/null | tr '\n' ';' || echo "")

ARMS="$ARMS" QUERY="$QUERY" DIST="$DIST" RATE="$RATE" REPS="$REPS" SCHEDULE="$SCHEDULE" \
TM_DEPLOYMENTS="$TM_DEPLOYMENTS" DRAIN_DEPLOYMENTS="$DRAIN_DEPLOYMENTS" \
PUBLISHED_SPEEDS="$PUBLISHED_SPEEDS" PUBLISH_LOADS="$PUBLISH_LOADS" \
RECOVERY_INTERVAL="$RECOVERY_INTERVAL" \
SUBMIT_PAR="$SUBMIT_PAR" TARGET_PAR="$TARGET_PAR" TM_REPLICAS="$TM_REPLICAS" \
SLOT_IDLE_TIMEOUT="$SLOT_IDLE_TIMEOUT" JOB_CLASS="$JOB_CLASS" CPU_LOAD="$CPU_LOAD" \
PIN_VERTEX="$PIN_VERTEX" PIN_PARALLELISM="$PIN_PARALLELISM" \
POISON_ARM="$POISON_ARM" POISON_SCHEDULE="$POISON_SCHEDULE" \
DRAIN_REPLICAS="$DRAIN_REPLICAS" DRAIN_PAR="$DRAIN_PAR" \
MEASURE_WARMUP="$MEASURE_WARMUP" MEASURE_WINDOW="$MEASURE_WINDOW" \
python3 - <<'PY' > "$OUT_ROOT/run.json"
import json, os
print(json.dumps({
    "query": os.environ["QUERY"], "dist": os.environ["DIST"],
    "rate": int(os.environ["RATE"]), "arms": os.environ["ARMS"].split(),
    "reps": int(os.environ["REPS"]),
    "schedule": os.environ["SCHEDULE"].split(),
    "submit_par": int(os.environ["SUBMIT_PAR"]),
    "target_par": int(os.environ["TARGET_PAR"]),
    "warmup_s": int(os.environ["MEASURE_WARMUP"]),
    "window_s": int(os.environ["MEASURE_WINDOW"]),
    "tm_replicas": int(os.environ["TM_REPLICAS"]),
    "tm_deployments": os.environ.get("TM_DEPLOYMENTS", ""),
    "drain_deployments": os.environ.get("DRAIN_DEPLOYMENTS", ""),
    # "" means every TaskManager is nominal, i.e. a homogeneous cluster.
    "taskmanager_speeds": [
        line for line in os.environ.get("PUBLISHED_SPEEDS", "").split(";")
        if line.strip() and "nothing published" not in line
    ],
    # Recorded because it is a CHOSEN operating point, not a fix: it decides how
    # long the pool keeps the surplus slots that give the assigner a decision at
    # all. Short (Flink's default) and the pool shrinks under the measurement —
    # which is what made tmsAvailable come out 3, 4 and 5 within one arm on
    # 2026-08-11. Long and the geometry holds still, at the cost of widening the
    # window in which any placement policy can matter. It belongs in the write-up
    # next to the result, so every run stamps the value it ran under.
    "slot_idle_timeout_ms": os.environ.get("SLOT_IDLE_TIMEOUT", "unknown"),
    "job_class": os.environ.get("JOB_CLASS", ""),
    "cpu_load_iter": os.environ.get("CPU_LOAD", ""),
    "pin_vertex": os.environ.get("PIN_VERTEX", ""),
    "pin_parallelism": os.environ.get("PIN_PARALLELISM", ""),
    "drain_replicas": os.environ.get("DRAIN_REPLICAS", ""),
    "published_loads": os.environ.get("PUBLISH_LOADS", ""),
    "recovery_sample_interval_s": os.environ.get("RECOVERY_INTERVAL", ""),
    "drain_par": os.environ.get("DRAIN_PAR", ""),
    "poison_arm": os.environ.get("POISON_ARM", ""),
    "poison_schedule": os.environ.get("POISON_SCHEDULE", ""),
}, indent=2))
PY

echo ""
echo "=========================================="
echo "  Done — $OUT_ROOT"
echo "  Next: python3 scripts/analyse_placement_experiment.py $OUT_ROOT"
echo "=========================================="
