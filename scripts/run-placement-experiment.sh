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
# Pathname expansion off for the whole script. Several settings are deliberately
# word-split unquoted — SCHEDULE, ARMS, TM_DEPLOYMENTS — and a step written as "1*"
# is a glob: with a file named 1-something in the working directory the shell
# replaced it with that filename, the trailing star vanished, and the campaign
# aborted with "SCHEDULE has no measured step". Nothing here relies on globbing,
# and a run must not depend on what happens to be sitting in the current directory.
set -f

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
# How the job packs operators into slots. Empty (the default) leaves Flink's own slot
# sharing alone, which is what every campaign up to 2026-08-28 measured.
#
# WHY IT MATTERS. Under default slot sharing the unit of placement is the SLICE, and a
# slice holds one subtask of every vertex — so the slices are interchangeable by
# construction and no arm can put a particular operator on a particular machine. That is
# the symmetry behind the early null results, and adding operators does not break it.
#
#   SLOT_SHARING=PER_STAGE      source/filters, cpu chain, window, sink each placeable
#   SLOT_SHARING=PER_OPERATOR   one group per vertex
#
# Slots stop being the MAXIMUM parallelism and become the SUM over groups, so the pool
# fills up fast: on the 12-slot cluster PER_STAGE fits up to SUBMIT_PAR=3 and
# PER_OPERATOR only at 2, where it leaves no free slot and therefore no decision at all.
# The job prints its own requirement at startup ("Slots required").
SLOT_SHARING="${SLOT_SHARING:-}"
# Bytes of padding on every Bid, which is what gives an edge a price. Records are handed over by
# reference inside one slot and serialized when they cross into another JVM, so with a 32-byte Bid
# the two cost the same and a policy that ignores edges loses nothing by ignoring them. Padding puts
# real CPU on the split. Only the Bid carries it, so the expensive edge is source/filters -> cpu
# chain and nothing downstream — the smallest instance of SP-Ant's "collect for communication or
# scatter for compute" dilemma. Empty or 0 reproduces every earlier campaign.
PAYLOAD_BYTES="${PAYLOAD_BYTES:-}"
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
# LPT_ORACLE (2026-09-30): LPT handed the per-vertex costs by DECLARATION, not measurement —
# what a perfect profiler would give it. Busy time is not reproducible on this cluster, so this
# is the honest upper bound of load-aware placement in ONE dimension. It is what separates the
# two things RL knows and unit-weight LPT does not: WHICH operators are expensive (the oracle
# knows it too) and WHICH MACHINE each resource is short on (only RL does — the oracle still
# sees one speed per machine and cannot know that medium's disk is capped). Default: the join
# dominates, the auction stage is the next heaviest, everything else weighs 1.
ORACLE_LOADS="${ORACLE_LOADS:-new-users-join=10,auction=3}"
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
# THE MARGIN COVERS A 40-SECOND DISAGREEMENT ABOUT WHEN THE RESCALE HAPPENED
# (measured 2026-09-21, and it silently cost a whole calibration). The driver starts
# counting when lib_wait_rescale sees every vertex reporting the new parallelism; the
# controller starts counting when the vertices' START TIMES move, which is when the
# tasks are actually deployed. In the 24000 run those were 01:10:14 and 01:10:54. With
# a 20-second margin the driver cancelled the job at 01:18:34 while the controller's
# window still had 20 seconds to run, so the measured step produced NO episode at all —
# and the only row in the file was the previous epoch, whose window straddled the
# rescale and was therefore (correctly) refused as "the job restarted inside the
# measurement window". Four rates, four uncreditable rows, for want of 40 seconds.
# 150, not 90 (2026-09-23). The margin has to cover everything between the driver starting
# its clock and the observer finishing: the ~40 s disagreement about when the rescale
# happened, plus the post-window work (an epoch-key read and a JobManager log scan, ~30 s).
# With --only-parallelism the observer now needs warmup+window+70 and 90 left twenty seconds
# of slack — too little for something that costs a three-hour run when it is wrong.
STEP_MARGIN="${STEP_MARGIN:-150}"
NARROW_HOLD=$((MEASURE_WARMUP + MEASURE_WINDOW + STEP_MARGIN))
# LONG ENOUGH FOR THE OBSERVER, not just for Flink. The controller treats every
# rescale as an epoch and spends warmup+window on each, so a 35-second transition
# left it still measuring a phase that had already ended — and by the time it
# finished, the measured step it should have captured was gone too. Measured
# 2026-09-07: four measured rescales per cell, one episode credited. Holding the
# wide phase as long as the narrow one costs about seven minutes per cell and lets
# every measured step be observed.
WIDE_HOLD="${WIDE_HOLD:-$((MEASURE_WARMUP + MEASURE_WINDOW + STEP_MARGIN))}"

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
# RETRIED, like every other call into this cluster. A single `kubectl get nodes`
# was the very first thing this script did, and a transient TLS handshake timeout —
# seen repeatedly on 2026-09-07 with the cluster demonstrably alive and the API
# server at two restarts in fifteen hours — aborted the campaign before it started.
# Three smoke tests were lost to it and read as failures of the experiment's design.
_reachable=0
for _try in 1 2 3 4; do
    if kubectl get nodes >/dev/null 2>&1; then _reachable=1; break; fi
    sleep 3
done
if [ "$_reachable" != 1 ]; then
    echo "ERROR: cluster unreachable after 4 attempts (minikube start)" >&2; exit 1
fi
JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}')
[ -n "$JM_POD" ] || { echo "ERROR: no running JobManager" >&2; exit 1; }

FORK=$(kubectl get pod -n "$NAMESPACE" "$JM_POD" \
    -o jsonpath='{.spec.containers[0].env[?(@.name=="THESIS_SLOT_ASSIGNER")].value}')
# Running against UNMODIFIED Flink is a legitimate configuration, not a mistake: since 2.x ships
# its own load balancing, stock Flink is the baseline every arm has to beat. It has to be asked
# for explicitly, though — an accidental stock run looks exactly like a campaign where every arm
# tied, which is the failure mode this whole thesis already spent months on.
if [ "$FORK" != "true" ]; then
    if [ "${ALLOW_STOCK_JM:-0}" = "1" ]; then
        echo "NOTE: JobManager is UNMODIFIED Flink. Measuring the stock baseline;"
        echo "      the arm names below are labels, not policies — nothing is being selected."
        STOCK_JM=1
    else
        echo "ERROR: JobManager is not on the fork (scripts/deploy-thesis-fork.sh)." >&2
        echo "       To measure unmodified Flink as the baseline, set ALLOW_STOCK_JM=1." >&2
        exit 1
    fi
else
    STOCK_JM=0
fi

# ------------------------------------------------------------------
# REST access
# ------------------------------------------------------------------
# The driver never touches the host port-forward: `kubectl port-forward` dies on a
# connection reset and leaves the PROCESS alive with a dead tunnel, so every curl
# silently returns nothing. A 2 h run lost 4 of 6 arms to exactly that on
# 2026-08-11 — the jobs were submitted and running fine, the script just could not
# see them, waited out its RUNNING timeout and skipped the arm. Job control now
# goes through the JobManager pod itself.
# RETRIED, because a single transient failure used to cost a whole measurement.
# Observed 2026-09-03 on the reference Q8 at RATE=8000: with the job in deep
# overload and 300 MB checkpoints, the JobManager is busy enough that the exec'd
# curl occasionally exceeds its timeout. The callers could not tell that apart from
# a real answer — the rescale payload builder saw an empty body, gave up with
# "could not build payload", and the step silently never happened, so the rep was
# measured in whatever parallelism it had been left in. Three tries turn a flaky
# transport into a slow one.
jm_curl() {
    local out=""
    for _attempt in 1 2 3; do
        out=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
            curl -s -m 20 "http://localhost:8081$1" 2>/dev/null)
        [ -n "$out" ] && break
        sleep 2
    done
    printf '%s' "$out"
}

# arm_controller.py does still need a reachable HTTP endpoint, so the tunnel is
# kept alive here rather than being assumed. Any stale forwarder is replaced: one
# with a dead tunnel is worse than none, because it answers the port.
# THE PROBE TIMEOUT WAS THE BUG, not the tunnel. Measured 2026-09-03: the forwarder
# was up and listening (`Forwarding from 127.0.0.1:8081` in the log, kubectl holding
# the socket) and the campaign still aborted with "could not establish the REST
# port-forward", because a JobManager busy with 300 MB RocksDB checkpoints answers
# /overview in more than the three seconds this probe allowed. A slow JobManager is
# not an absent one.
ensure_port_forward() {
    curl -s -m 15 http://localhost:8081/overview >/dev/null 2>&1 && return 0
    # Anchored at kubectl so the pattern can never match the shell that is running
    # this script (or the watchdog subshell), only a real forwarder.
    pkill -f "^kubectl port-forward.*flink-jobmanager.*8081" >/dev/null 2>&1
    sleep 1
    nohup kubectl port-forward -n "$NAMESPACE" svc/flink-jobmanager 8081:8081 \
        >> "$OUT_ROOT/port-forward.log" 2>&1 &
    for _ in $(seq 1 20); do
        sleep 2
        curl -s -m 15 http://localhost:8081/overview >/dev/null 2>&1 && return 0
    done
    return 1
}

# NO LONGER FATAL. arm_controller.py now reaches Flink through `kubectl exec`
# (--jm-pod), so the tunnel is a convenience for a human with a browser, not a
# dependency of the measurement. It used to be fatal, and on 2026-09-03 it aborted
# a campaign whose tunnel was in fact up and answering in 0.7 s — the probe just
# had a three-second budget against a JobManager busy with checkpoints.
if ! ensure_port_forward; then
    echo "WARNING: no REST port-forward; the campaign continues (the controller" >&2
    echo "         talks to the JobManager pod directly)." >&2
fi

# Re-check often enough that the controller loses at most one poll to a reset.
( while true; do
      sleep 15
      curl -s -m 15 http://localhost:8081/overview >/dev/null 2>&1 || ensure_port_forward
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
        fi
        # Always waited on, never only after a scale: a deployment already sitting at the
        # requested replica count can still have a pod that never became ready (Pending on
        # cpu, CrashLoopBackOff), and skipping the wait in that case let a whole campaign
        # run with a TaskManager silently missing from the pool.
        if ! kubectl rollout status "deployment/$deployment" -n "$NAMESPACE" \
                --timeout=180s >/dev/null 2>&1; then
            echo "  ! $deployment is not ready after 180s" >&2
        fi
    done
}
scale_taskmanagers "$TM_DEPLOYMENTS"

# The pool the assigner will actually see. A campaign whose whole point is a contrast
# between machine classes is void if one class never registered, so the mismatch is
# reported up front instead of being discovered in the mapping= lines afterwards.
assert_taskmanagers_registered() {
    local expected="$1" registered=0 waited=0
    [ -n "$expected" ] || return 0
    ensure_port_forward
    # Polled, not sampled once: a TaskManager that was registered with the PREVIOUS JobManager has
    # to notice the new one through a heartbeat timeout, which takes about two minutes. Checking
    # immediately after a deploy reports an empty cluster that is merely late.
    while [ "$waited" -lt 210 ]; do
        registered=$(jm_curl /overview | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("taskmanagers", 0))' 2>/dev/null || echo 0)
        [ "$registered" = "$expected" ] && break
        [ "$waited" = 0 ] && echo "  waiting for TaskManagers to register ($registered/$expected)..."
        sleep 10
        waited=$((waited + 10))
    done
    echo "TaskManagers registered: $registered (expected $expected) after ${waited}s"
    if [ "$registered" != "$expected" ]; then
        echo "  ! WARNING: $((expected - registered)) TaskManager(s) missing from the pool;" >&2
        echo "  ! placement arms will be compared over a smaller cluster than declared." >&2
        [ "${REQUIRE_ALL_TMS:-0}" = "1" ] && exit 1
    fi
}
# NO TWO OBSERVERS AT ONCE (2026-09-28). A run whose driver was killed leaves its
# arm_controller and characterizer_agent alive — they are separate processes, and
# `pkill -f run-placement-experiment.sh` does not touch them. Measured that night: the agent of
# a dead run kept publishing to /var/thesis/assignment while a new run was training, so the
# placement applied at each rescale was whichever process wrote last, and the new agent was
# crediting ITS trajectory for a reward the OLD plan produced. Mis-attributed episodes do not
# merely waste a night, they poison the table. Silent corruption is worth refusing to start for.
STALE=$(pgrep -f "scripts/(arm_controller|characterizer_agent)\.py" 2>/dev/null | tr '\n' ' ')
if [ -n "${STALE// /}" ]; then
    echo "ERROR: ya hay un observador o un agente corriendo (pids: $STALE)." >&2
    echo "       Dos agentes escriben el mismo /var/thesis/assignment y se pisan el plan." >&2
    echo "       Termínalos antes de empezar:  pkill -f 'scripts/(arm_controller|characterizer_agent).py'" >&2
    [ "${ALLOW_STALE_OBSERVERS:-0}" = "1" ] || exit 1
    echo "       ALLOW_STALE_OBSERVERS=1 — se continúa bajo tu responsabilidad." >&2
fi

EXPECTED_TMS=$(echo "$TM_DEPLOYMENTS" | tr ' ' '\n' | awk -F: 'NF{s+=$NF} END{print s+0}')
assert_taskmanagers_registered "$EXPECTED_TMS"

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
# THE WIDTH THE OBSERVER SHOULD WAIT FOR. Passed to arm_controller so it does not spend a
# full warmup+window on the submission's placement before reaching the step under test; see
# the --only-parallelism note there. Only when every measured step is the same width: a
# schedule that measures two widths needs the observer to look at both.
# RANDOM_TARGETS="1 2": el paso medido sortea su paralelismo en cada repeticion, en vez de
# repetir siempre el mismo. ES CONTRA EL SOBREAJUSTE, y es lo que pide el plan del profesor:
# entrenando a un solo ancho la tabla memoriza UNA geometria de slices — con 12 slots y cuatro
# etapas, un ancho fijo recorre siempre la misma secuencia de mascaras — en lugar de aprender
# una regla que valga para varias. Los anchos utiles aqui son 4 y 8 slices (paralelismo 1 y 2):
# a paralelismo 3 las slices igualan los slots y el asignador no tiene eleccion alguna.
MEASURED_PAR=""
if [ -n "${RANDOM_TARGETS:-}" ]; then
    MEASURED_PAR=$(echo $RANDOM_TARGETS | tr ' ' ',')
fi
for STEP in $SCHEDULE; do
    case "$STEP" in
        *\*) W="${STEP%\*}"
             [ -n "${RANDOM_TARGETS:-}" ] && continue   # ya cubierto por la lista sorteada
             if [ -z "$MEASURED_PAR" ]; then MEASURED_PAR="$W"
             elif [ "$MEASURED_PAR" != "$W" ]; then MEASURED_PAR=""; break; fi ;;
    esac
done
POISON_COST=0
for PSTEP in $POISON_SCHEDULE; do POISON_COST=$((POISON_COST + POISON_HOLD)); done
[ -n "$POISON_ARM" ] || POISON_COST=0
DRAIN_COST=0
[ "$DRAIN_REPLICAS" -gt 0 ] 2>/dev/null && DRAIN_COST=$((DRAIN_HOLD + RESTORE_HOLD + 120))
# THE JOB HAS TO OUTLIVE THE SCHEDULE, AND THE SCHEDULE IS NOT ONLY THE HOLDS
# (measured 2026-09-23, and it cost a three-hour run). The sources stop after
# JOB_DURATION seconds, so if the number is short the job FINISHES mid-window and every
# cell records the straddling epoch instead of the measured one — six cells, six rows of
# "the job restarted inside the measurement window", zero usable data. The old estimate
# counted the holds plus a flat 180 s and ignored three costs that grow with the
# configuration: the settle before the first transition (which is MEASURE_WARMUP, 360 s
# here, not a constant), the wait for the job to reach RUNNING, and the wait for EACH
# rescale to land (88 s that night, 187 s the night before, up to RESCALE_TIMEOUT).
# Budget the timeout for every step: overshooting costs nothing, because the driver
# cancels the job when the schedule ends, while undershooting loses the whole run.
STEPS_PER_REP=$(set -- $SCHEDULE; echo $#)
JOB_DURATION=$(( REPS * (CYCLE + STEPS_PER_REP * ${RESCALE_TIMEOUT:-300})
                 + MEASURE_WARMUP + POISON_COST + DRAIN_COST + 180 ))
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
    payload=$(echo "$job_json" | TARGET="$target" PIN="$PIN_VERTEX" PINPAR="$PIN_PARALLELISM" \
        DECLARED="$(dirname "$log")/declared-par-${jid:0:8}.json" python3 -c "
import json, os, re, sys
data = json.load(sys.stdin)
t = int(os.environ['TARGET'])
# DECLARED WIDTHS, NOT CURRENT ONES (2026-09-29). Which vertices are scalable used to be read off
# the job's CURRENT parallelism (par > 1). Once a repetition pinned new-users-join to 1, the join
# WAS at 1, looked like a vertex declared at 1 on purpose, and stayed there for the rest of the
# job: every later 'none' draw ran the pinned geometry (7 slices, never 8). The first call sees
# the job as submitted, before any pin, so its widths are recorded once and read from then on.
declared_path = os.environ['DECLARED']
try:
    declared = json.load(open(declared_path))
except (OSError, ValueError):
    declared = {v['id']: max(v.get('parallelism', 1), 1) for v in data.get('vertices', [])}
    json.dump(declared, open(declared_path, 'w'))
# PIN accepts either a single pattern (with PINPAR as its width) or a list of
# 'pattern:width' pairs separated by commas. Several pins at DIFFERENT widths are
# what give the slices several distinct demand levels: a slice carries a vertex
# only if that vertex's width reaches its index, so two pins at 2 and 4 over a
# graph of width 6 produce three tiers of slice rather than two.
#
# That matters more than it sounds. Measured by enumeration on the latency
# objective, a greedy's gap to the optimum is 0.0% with one demand level, ~3%
# with two, and 6-9% with four — so a workload with a single pin cannot show a
# placement policy doing anything, no matter how many operators the query has.
pin = os.environ.get('PIN', '')
# OR, not a default argument (2026-09-29): an EMPTY PINPAR is present, so .get() returns ''
# instead of the default and int('') raises. RANDOM_PINS sets it empty on the 'none' draw, and
# every such repetition silently failed to build a payload and stayed at the wide width —
# which cost the 'none' geometry of two whole training runs.
pinpar = int(os.environ.get('PINPAR') or '2')
pins = []
if ':' in pin:
    for part in pin.split(','):
        pattern, _, width = part.rpartition(':')
        if pattern.strip():
            pins.append((pattern.strip(), int(width)))
elif pin:
    pins.append((pin, pinpar))

reqs = {}
for v in data.get('vertices', []):
    par = declared.get(v['id'], max(v.get('parallelism', 1), 1))
    name = v.get('name', '')
    pinned = next((w for pattern, w in pins if re.search(pattern, name, re.IGNORECASE)), None)
    if pinned is not None:
        # Held at a width the rest of the graph does not share. That gap is what
        # makes some slices carry this operator and others not — scaling it with
        # everything else would make every slice identical again.
        reqs[v['id']] = {'parallelism': {'lowerBound': pinned, 'upperBound': pinned}}
    elif par > 1:
        reqs[v['id']] = {'parallelism': {'lowerBound': t, 'upperBound': t}}
    else:
        # Declared at 1 on purpose in several queries (Q5's global top-N): that 1
        # is semantics, not a performance choice.
        reqs[v['id']] = {'parallelism': {'lowerBound': par, 'upperBound': par}}
print(json.dumps(reqs))
" 2>/dev/null)
    if [ -z "$payload" ]; then
        # Counted as a failed rescale, so three in a row abort the arm like any other. It used
        # to return quietly and the rep was measured at whatever width the job already had.
        echo "    ! could not build payload for $label" | tee -a "$log"
        RESCALE_MISSES=$(( ${RESCALE_MISSES:-0} + 1 ))
        return 1
    fi
    # RETRIED, like every other call into the JobManager. This one was missed when
    # jm_curl got its retries on 2026-09-03 and failed within minutes: an empty
    # HTTP code meant the exec never ran, the transition back to the wide
    # parallelism never happened, and the NEXT repetition's "measured" PUT became a
    # no-op — the job was already at that width, so no rescale fired, no placement
    # decision was taken, and the episode recorded whatever was already running.
    # A dropped transition silently costs the repetition after it, not this one.
    code=""
    for _try in 1 2 3; do
        code=$(kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
            curl -s -o /dev/null -w "%{http_code}" -m 20 -X PUT \
            "http://localhost:8081/jobs/$jid/resource-requirements" \
            -H "Content-Type: application/json" -d "$payload" 2>/dev/null)
        case "$code" in 200|202) break ;; esac
        sleep 3
    done
    echo "    [$label] PUT parallelism=$target -> HTTP $code" | tee -a "$log"
    case "$code" in
        200|202)
            # WAIT FOR IT TO LAND. The PUT is acknowledged instantly and the job
            # keeps running at its old width for another two and a half minutes
            # while the adaptive scheduler walks Idling -> Stabilizing ->
            # Stabilized -> Transitioning. Measured 2026-09-07: 156 seconds from
            # request to the assigner producing a placement, against a hold of 140.
            # The campaign moved on before its own rescale arrived, so every
            # measured episode observed the SUBMISSION's placement — which is why
            # assignments appeared in two repetitions out of four and why so many
            # episodes were credited at the submission width.
            python3 "$SCRIPT_DIR/lib_wait_rescale.py" --pod "$JM_POD" --job "$jid" \
                --target "$target" --namespace "$NAMESPACE" \
                --timeout "${RESCALE_TIMEOUT:-300}" 2>&1 | tee -a "$log"
            # ABORT AFTER A FEW RESCALES THAT NEVER LAND (2026-09-28, and it cost ten hours).
            # A job that stops rescaling does not recover on its own: the run of 2026-09-27 got
            # stuck at parallelism 1 — a width that cannot sustain the rate, so it thrashed —
            # and the driver kept PUTting requirements at it for SEVEN HOURS, writing the same
            # warning thirty-four times and producing five epochs and zero credited episodes.
            # `tee` in the pipeline above hides the exit status, hence PIPESTATUS.
            if [ "${PIPESTATUS[0]}" -eq 0 ]; then
                RESCALE_MISSES=0
            else
                RESCALE_MISSES=$(( ${RESCALE_MISSES:-0} + 1 ))
                echo "    ! reescalados sin aterrizar consecutivos: $RESCALE_MISSES" | tee -a "$log"
            fi
            ;;
        *) echo "    ! rescale not accepted (HTTP ${code:-none}) — this rep will be" \
                "measured in whatever configuration the job is already in" | tee -a "$log" ;;
    esac
}

CELL=0
for ARM in $ARMS; do
  CELL=$((CELL + 1))
  # A REPEATED ARM NAME GETS ITS OWN CELL (2026-09-22). `ARMS="LPT LPT LPT"` is the direct
  # test for a position effect — one deterministic arm run several times in a row, where any
  # difference between the cells IS the position and nothing else. Before this, every
  # repetition wrote into the same directory and overwrote the previous one's episodes.
  RESCALE_MISSES=0
  CELL_DIR="$OUT_ROOT/$ARM"
  [ -d "$CELL_DIR" ] && CELL_DIR="$OUT_ROOT/${ARM}#${CELL}"
  mkdir -p "$CELL_DIR"
  DRIVER_LOG="$CELL_DIR/driver.log"

  echo ""
  echo "######################################################"
  echo "###  [$CELL/$NARMS] arm=$ARM"
  echo "######################################################"

  cleanup_jobs
  sleep 5

  # AND ON THE SAME TASKMANAGERS (2026-09-07). Something accumulates inside the
  # TaskManager JVMs across jobs, and it is measurable: the THIRD measurement of a sweep
  # is degraded no matter what rate it asks for. Three identical runs at 32000 rec/s gave
  # ratios 0.982 / 0.979 / 0.873, and the same shape appears in every ascending sweep of
  # that evening (1.000/0.997/0.842, 1.000/0.956/0.705, 0.996/1.012/0.717) — which is what
  # made a nonexistent "capacity knee" appear between 28000 and 29000. It is not the disk:
  # wiping MinIO between rates does not prevent it. It is the processes — the node held
  # 4.1 GiB resident that `drop_caches` would not free, and these TaskManagers carry a CPU
  # limit but NO memory limit. Restarting them brought the node back to 2.1 GiB and lifted
  # the third run from 0.873 to 0.927, so this mitigates the drift without removing it:
  # roughly two clean measurements per TaskManager lifetime, which is a real constraint on
  # REPS and worth remembering when reading a cell's later repetitions.
  # The speed vector survives this: it is keyed by taskmanager.resource-id (tm-3-fast and
  # friends), which the manifests pin, so a restarted pod keeps its published speed even
  # though its IP changes.
  if [ "${RESTART_TMS:-1}" = "1" ]; then
      # Not `set --`: this runs inside the arm loop, where clobbering the positional
      # parameters would change what the rest of the campaign sees.
      TM_NAMES=""
      # shellcheck disable=SC2086
      for spec in $TM_DEPLOYMENTS; do TM_NAMES="$TM_NAMES ${spec%%:*}"; done
      if [ -n "$TM_NAMES" ]; then
          echo "  reiniciando TaskManagers para empezar el brazo en frío"
          # shellcheck disable=SC2086
          kubectl rollout restart -n "$NAMESPACE" $(for n in $TM_NAMES; do printf 'deploy/%s ' "$n"; done) >/dev/null 2>&1 || true
          for n in $TM_NAMES; do
              kubectl rollout status -n "$NAMESPACE" "deploy/$n" --timeout=180s >/dev/null 2>&1 || true
          done
          # Registration is what matters, not the rollout: a TaskManager that was talking to
          # the previous pod needs a heartbeat timeout to find the JobManager again.
          assert_taskmanagers_registered "$EXPECTED_TMS"
      fi
  fi

  # DISK_LIMIT="fast:10M medium:off": the second resource dimension, reapplied here because a
  # restarted TaskManager is a new pod with a new cgroup, and the cap lived in the old one. Set
  # for EVERY arm, not once, for the same reason every arm gets restarted: an arm that inherited
  # a cap from the previous one — or silently lost it — would not be the machine it claims to
  # be. See scripts/limit-disk.sh for why disk and not memory.
  if [ -n "${DISK_LIMIT:-}" ]; then
      for spec in $DISK_LIMIT; do
          "$SCRIPT_DIR/limit-disk.sh" "${spec%%:*}" "${spec##*:}" 2>&1 | sed 's/^/  /' \
              | tee -a "$CELL_DIR/disk-limit.log"
      done
  fi

  # EVERY ARM MUST START ON THE SAME DISK (2026-09-07). MinIO keeps a cancelled job's
  # checkpoints, so a session accumulates them: after sixteen jobs the hostPath held
  # 4.3 GB, the node had written 394 GB and the container's memory had gone from 2.0 to
  # 4.5 GiB on page cache alone, with no pod ever restarting. Capacity fell with it, and
  # measurably — three calibrations inside twenty-five minutes emitted 33682, 22544 and
  # 20419 rec/s while being asked for progressively LESS (40000, 32000, 29000), so what
  # reads as a capacity ceiling is really the disk filling up.
  # In a campaign this is not noise but bias with a direction: the arm that runs second
  # always meets a dirtier cluster than the arm that runs first, which is exactly the
  # position effect the reversed pass exists to cancel. Cleaning before each arm — the
  # first one included, so a campaign does not inherit whatever ran before it — puts
  # every cell on the same footing.
  # WHAT ACTUALLY GROWS IS .minio.sys/multipart. Measured after sixteen jobs: the two
  # data directories held 4 KB each while `.minio.sys/multipart` held 3.9 GB — the parts
  # of uploads that a cancelled job never finished. Wiping only the checkpoint tree, as
  # this did at first, emptied a directory that was already empty.
  # THE CLUSTER MUST BE IDLE HERE. Deleting a multipart directory while an upload is in
  # flight is precisely what produces the NoSuchUpload (404) that failed a checkpoint and
  # restarted the job on 2026-09-07, costing that campaign its STOCK pass. cleanup_jobs
  # above cancels every job, but `flink cancel` returns on acceptance rather than on
  # completion, so give the last checkpoint a moment to stop writing before removing the
  # parts underneath it.
  if [ "${CLEAN_CHECKPOINTS:-1}" = "1" ]; then
      sleep 5
      # BOUNDED (2026-09-23): this call hung for 41 minutes on the sixth cell of an order
      # experiment and the run simply stopped there — the driver has no other deadline, so an
      # `ssh` that never returns costs the rest of the session. The `rm` is the part that
      # matters; `sync` and `drop_caches` are hygiene, and skipping them beats stalling. Not
      # cleaning at all is announced below, so a cell prepared differently is visible in the
      # log rather than silently mixed into the comparison.
      # `docker exec` FIRST, and a timeout that actually kills (2026-09-25). Two runs died
      # here. `minikube ssh` hangs on this host — the agent has preferred `docker exec` since
      # the SIGTTIN episode of 2026-09-16 for the same reason — and plain `timeout N` only
      # sends TERM: when the child ignores it, timeout waits forever, which is how a call
      # nominally bounded at 180 s was still blocking the driver twenty-four minutes later.
      # `-k` gives it a KILL after the grace period.
      # THE `rm` AND THE CACHE DROP ARE SEPARATE CALLS (2026-09-25). Bundled, they shared a
      # deadline, and it is the second half that blocks: `sync` and `drop_caches` walk the
      # whole page cache, which on WSL2 takes minutes or never returns. Killing the pair
      # therefore threw away the deletion too — and the deletion is the part that matters,
      # since MinIO's multipart leftovers are what degrade capacity within a session. The
      # cache drop is hygiene: attempted, bounded, and its failure is not worth a warning.
      # NO `sudo` ON THE docker exec PATH, AND NO STDIN (2026-09-26). This is what was
      # actually hanging, twice, for the full 180 s: `docker exec` already runs as root inside
      # the minikube container, and `sudo` with stdin attached but no TTY blocks forever
      # waiting for a password nobody can type. `minikube ssh` keeps its `sudo` because there
      # it is configured passwordless — which is why publish-loads.sh never had this problem.
      CLEAN_PATHS="/var/thesis/minio/flink-checkpoints/checkpoints/* \
                   /var/thesis/minio/.minio.sys/multipart/* \
                   /var/thesis/minio/.minio.sys/tmp/*"
      if timeout -k 10 "${CLEAN_TIMEOUT:-180}" docker exec "${THESIS_NODE:-minikube}" \
             sh -c "rm -rf $CLEAN_PATHS 2>/dev/null" </dev/null >/dev/null 2>&1 \
         || timeout -k 10 "${CLEAN_TIMEOUT:-180}" minikube ssh -n "${THESIS_NODE:-minikube}" \
             -- "sudo rm -rf $CLEAN_PATHS 2>/dev/null" </dev/null >/dev/null 2>&1
      then
          echo "  checkpoints limpiados"
          timeout -k 5 30 docker exec "${THESIS_NODE:-minikube}" \
              sh -c "sync; echo 3 > /proc/sys/vm/drop_caches" \
              </dev/null >/dev/null 2>&1 || true
      else
          # Not fatal: a dirty cell is still a cell, and aborting here would throw away a
          # campaign over housekeeping. It is announced so the log records which cells
          # started clean, because that is now part of reading the result.
          echo "  ! no se pudieron limpiar los checkpoints — este brazo arranca sucio" >&2
      fi
  fi

  # THE JAR LIVES INSIDE THE JOBMANAGER POD, so it goes with the pod. Measured
  # 2026-09-07: the JobManager was recreated between two arms of one campaign and
  # the second could not submit at all ("JAR file does not exist: /tmp/nexmark.jar"),
  # losing the cell. Uploading once per campaign assumed a pod that outlives it.
  # Re-uploading per arm costs half a minute against a twenty-minute cell.
  JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
      --field-selector=status.phase=Running --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)
  if ! kubectl exec -n "$NAMESPACE" "$JM_POD" -- test -f /tmp/nexmark.jar 2>/dev/null; then
      echo "  el jar no está en $JM_POD — subiéndolo de nuevo"
      kubectl cp "$LOCAL_JAR" "$NAMESPACE/$JM_POD:/tmp/nexmark.jar" || true
  fi

  # PLAN_<name> ARMS (2026-09-17): a hand-written placement applied through the RL arm, so
  # two fixed placements can be compared head to head in a paired campaign. The fork only
  # knows RL; the cell keeps the PLAN_ label, which is what the analysis groups by. The plan
  # itself is published once the job is running (below), because it is resolved against the
  # job's actual vertices and TaskManagers.
  FORK_ARM="$ARM"
  STATIC_PLAN=""
  ORACLE_ARM=0
  case "$ARM" in
      LPT_ORACLE)
          FORK_ARM="LPT"
          ORACLE_ARM=1
          ;;
      PLAN_*)
          FORK_ARM="RL"
          STATIC_PLAN="$ROOT_DIR/plans/${ARM#PLAN_}.plan"
          [ -f "$STATIC_PLAN" ] || { echo "  ! no existe $STATIC_PLAN"; continue; }
          ;;
  esac
  "$SCRIPT_DIR/publish-arm.sh" "$FORK_ARM" >/dev/null || { echo "  ! publish failed"; continue; }
  echo "  published arm: $FORK_ARM${STATIC_PLAN:+ (plan fijo $(basename "$STATIC_PLAN"))}"
  sleep 3

  # The first eight arguments are shared by both jobs (GraphConfig.fromArgs and
  # NexmarkRealJob deliberately mirror each other), and position 6 is the one that
  # diverges: the synthetic job reads it as the CPU-load operator's own
  # parallelism — the source of the slice asymmetry — while Nexmark reads it as
  # the heavy vertex's initial width.
  # Both Nexmark jobs take the same positional arguments; RefNexmarkJob exists
  # because it builds the query on the REFERENCE Beam generator with a source per
  # event type, not because its CLI differs. Matching the class name exactly meant
  # the reference job silently fell through to the synthetic layout.
  case "${JOB_CLASS##*.}" in
      NexmarkRealJob|RefNexmarkJob) IS_NEXMARK=1 ;;
      *) IS_NEXMARK=0 ;;
  esac
  if [ "$IS_NEXMARK" = 1 ]; then
      ARG6="$SUBMIT_PAR"
      # Nexmark already uses args[8..10] for query/zipf/hotPool, so the mode lands at [11].
      TAIL_ARGS="$(query_job_args "$QUERY") $SLOT_SHARING"
      WHAT="$QUERY"
  else
      ARG6="$PIN_PARALLELISM"
      # The synthetic job stops at args[7], so the mode is args[8] and the payload args[9]. The
      # mode has to be filled in when only a payload is asked for, or the payload would be read
      # as the mode.
      if [ -n "$PAYLOAD_BYTES" ]; then
          TAIL_ARGS="${SLOT_SHARING:-SHARED} $PAYLOAD_BYTES"
      else
          TAIL_ARGS="$SLOT_SHARING"
      fi
      WHAT="${JOB_CLASS##*.} (cpuLoad=${CPU_LOAD} iter/ev, cpuLoadPar=${PIN_PARALLELISM})"
  fi
  [ -n "$SLOT_SHARING" ] && WHAT="$WHAT, slot sharing $SLOT_SHARING"
  [ -n "$PAYLOAD_BYTES" ] && WHAT="$WHAT, payload ${PAYLOAD_BYTES}B"

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

  # DOES THE ASSIGNER EVEN SEE THE WHOLE CLUSTER? `freeSlots` is what the JOB asked
  # for, not what the cluster has, and Flink fills that request from whichever
  # TaskManagers it likes. Measured 2026-09-06: a SHARED job submitted at
  # parallelism 6 asked for 6 slots, Flink covered them from `slow` (6 slots) and
  # `fast` (2), and `tm-2-medium` never entered the pool — the whole campaign ran
  # on two machines out of three and nothing said so. To guarantee every machine
  # participates the job must ask for more slots than the two largest supply.
  sleep 8
  SEEN_TMS=$(kubectl logs -n "$NAMESPACE" "$JM_POD" 2>/dev/null \
      | grep -o "tmsAvailable=[0-9]*" | tail -1 | cut -d= -f2)
  if [ -n "$SEEN_TMS" ] && [ "$SEEN_TMS" -lt "$EXPECTED_TMS" ]; then
      echo "  ! EL POOL SOLO OFRECE $SEEN_TMS de $EXPECTED_TMS TaskManagers." | tee -a "$DRIVER_LOG"
      echo "  ! El brazo decide sobre un clúster más pequeño que el declarado." | tee -a "$DRIVER_LOG"
      echo "  ! Sube SUBMIT_PAR hasta que el job pida más slots que la suma de las" | tee -a "$DRIVER_LOG"
      echo "  ! dos máquinas mayores, o la heterogeneidad no entra en la decisión." | tee -a "$DRIVER_LOG"
  fi

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

  # Before the first rescale, so the measured step is already decided by the fixed plan. The
  # width is the slice count at the measured parallelism under PER_STAGE (four stages in Q8).
  if [ -n "$STATIC_PLAN" ]; then
      if python3 "$SCRIPT_DIR/publish-static-plan.py" --spec "$STATIC_PLAN" \
              --jm-pod "$JM_POD" --width $(( TARGET_PAR * ${PLAN_STAGES:-4} )) \
              > "$CELL_DIR/static-plan.txt" 2>&1; then
          echo "  plan fijo publicado -> $CELL_DIR/static-plan.txt"
      else
          echo "  ! no se pudo publicar el plan fijo:"; sed 's/^/    /' "$CELL_DIR/static-plan.txt"
          cleanup_jobs; continue
      fi
  fi

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
  #
  # The oracle arm declares its vector here, and EVERY other arm run with PUBLISH_LOADS=0 now
  # publishes a no-match file instead of keeping "whatever is there": the assigner holds its
  # last good copy, so an oracle arm would otherwise leak its weights into the next LPT arm
  # — the same leak that made 2026-09-22's "unit" LPT secretly weighted.
  if [ "$ORACLE_ARM" = "1" ]; then
      echo "  LPT oráculo: declarando costos '$ORACLE_LOADS'" | tee -a "$DRIVER_LOG"
      if timeout -k 5 90 "$SCRIPT_DIR/publish-loads.sh" --declare "$ORACLE_LOADS" >>"$DRIVER_LOG" 2>&1; then
          timeout -k 5 60 "$SCRIPT_DIR/publish-loads.sh" --read 2>/dev/null | sed 's/^/    /'
      else
          echo "  ! NO se pudo declarar el vector: esta celda es LPT UNITARIO, no oráculo" \
              | tee -a "$DRIVER_LOG" | tee "$CELL_DIR/ORACLE_FAILED"
      fi
  elif [ "$PUBLISH_LOADS" = "0" ]; then
      timeout -k 5 60 "$SCRIPT_DIR/publish-loads.sh" --disable >>"$DRIVER_LOG" 2>&1 \
          || echo "  ! no pude desactivar los pesos: si hubo un brazo oráculo antes, pueden seguir" \
              | tee -a "$DRIVER_LOG"
  fi
  if [ "$PUBLISH_LOADS" = "1" ] && [ "$LOADS_PUBLISHED" = "0" ] && [ "$ORACLE_ARM" = "0" ]; then
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
      "$SCRIPT_DIR/publish-arm.sh" "$FORK_ARM" >/dev/null || true
      sleep 3
      echo "  poisoned; measuring $ARM from here"
  fi

  timeout $((JOB_DURATION + 120)) python3 -u "$SCRIPT_DIR/arm_controller.py" \
      --observe --out-dir "$CELL_DIR" \
      --jm-pod "$JM_POD" \
      --warmup "$MEASURE_WARMUP" --window "$MEASURE_WINDOW" \
      --poll-interval "$RECOVERY_INTERVAL" \
      ${MEASURED_PAR:+--only-parallelism "$MEASURED_PAR"} \
      > "$CELL_DIR/arm-controller.log" 2>&1 &
  CONTROLLER_PID=$!

  # THE CHARACTERISER RIDES ALONG, it does not replace the observer (2026-09-15). The
  # arm_controller above keeps recording the episode CSV every analysis script reads; the
  # agent only writes /var/thesis/loads, which LPT consumes at the next rescale. Started
  # here so it lives exactly as long as the cell's job — an agent outliving its job would
  # publish loads keyed to vertex ids the next job may not share, and one started by hand
  # in another terminal is easy to leave running into the following arm.
  # Only with the RL arm: that is the one that APPLIES the agent's plan. Against any other
  # arm the agent would be learning from placements it never decided.
  AGENT_PID=""
  # THE AGENT MUST FINISH BEFORE THE NEXT RESCALE (2026-09-16). Its declarations only take
  # effect at the following rescale, so an epoch counts as credited only if they were
  # published before that epoch began. Given the same warmup and window as the observer,
  # the agent lands LATE — its own sampling adds to the window — and the driver rescales at
  # warmup+window+20: measured on the smoke test, one epoch in four was credited. A shorter
  # window leaves the margin, at the cost of a slightly noisier reward, which is the right
  # trade for a signal that is only used to rank three actions.
  AGENT_WINDOW=$(( MEASURE_WINDOW * 4 / 5 ))
  if [ "${CHARACTERISER:-0}" = "1" ]; then
      case "$ARM" in
          RL)
              # A LIVE AGENT STARTS FROM NO PLAN (2026-09-30). Vertex ids survive across
              # submissions, so a plan left by the PREVIOUS job resolves perfectly well against
              # this one and the fork applies it: in the oracle campaign, passes B-D measured
              # the plan the agent had computed in the pass before, from another job's metrics.
              # Without a plan the first rescale falls back to LPT, visibly, and every plan
              # measured afterwards was computed in this job.
              timeout -k 5 30 docker exec "${THESIS_NODE:-minikube}" rm -f /var/thesis/assignment \
                  </dev/null >/dev/null 2>&1 || true
              timeout $((JOB_DURATION + 120)) python3 -u "$SCRIPT_DIR/characterizer_agent.py" \
                  --jm-pod "$JM_POD" --out-dir "$CELL_DIR" \
                  --warmup "$MEASURE_WARMUP" --window "$AGENT_WINDOW" \
                  ${AGENT_QTABLE:+--qtable "$AGENT_QTABLE"} ${AGENT_FREEZE:+--freeze} \
                  ${AGENT_LATENCY_WEIGHT:+--latency-weight "$AGENT_LATENCY_WEIGHT"} \
                  ${AGENT_LOCAL_CREDIT:+--local-credit "$AGENT_LOCAL_CREDIT"} \
                  ${AGENT_UCB:+--ucb "$AGENT_UCB"} \
                  ${MEASURED_PAR:+--only-parallelism "$MEASURED_PAR"} \
                  < /dev/null > "$CELL_DIR/characterizer.log" 2>&1 &
              AGENT_PID=$!
              echo "  agente caracterizador activo (pid $AGENT_PID) -> $CELL_DIR/characterizer.log"
              ;;
          *) echo "  (CHARACTERISER=1 ignorado: solo el brazo RL aplica el plan del agente)" ;;
      esac
  elif [ "$ARM" = RL ]; then
      # SAY WHOSE POLICY THIS IS (2026-09-21). The multi-arm campaign of that night ran the
      # RL arm for nine hours without CHARACTERISER=1 — no campaign script exported it — so
      # no agent ran and the fork applied whatever was left at /var/thesis/assignment: a plan
      # written three days earlier, BEFORE the Q8 join was fixed. The run was still a valid
      # evaluation of a frozen policy, and it is in the thesis as one, but nothing in the
      # output said which policy, and that had to be reconstructed afterwards from the file's
      # mtime. Announce it instead: a frozen evaluation is a choice, not an accident.
      PLAN_AGE=$(docker exec minikube stat -c '%y' /var/thesis/assignment 2>/dev/null | cut -c1-16)
      if [ -n "$PLAN_AGE" ]; then
          echo "  brazo RL SIN agente: se evalúa la política CONGELADA de $PLAN_AGE"
          echo "$PLAN_AGE" > "$CELL_DIR/frozen-policy-date.txt"
          docker exec minikube cat /var/thesis/assignment > "$CELL_DIR/frozen-policy.txt" 2>/dev/null
      else
          echo "  ! brazo RL sin agente Y SIN PLAN publicado: el fork caerá a LPT y esta"
          echo "    celda será una segunda copia de LPT, no una medición de RL."
      fi
  fi
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
      # A DEAD AGENT STOPS THE ARM (2026-10-02). The agent crashed at its second measured epoch
      # (a zero baseline) and this loop measured four more repetitions with nobody publishing
      # plans — an hour and a half of episodes that looked like training and were not. The
      # traceback is in characterizer.log.
      if [ -n "$AGENT_PID" ] && ! kill -0 "$AGENT_PID" 2>/dev/null; then
          echo "  ! el agente (pid $AGENT_PID) murió antes de la rep $rep — se detiene el brazo;" \
               "ver $CELL_DIR/characterizer.log" | tee -a "$DRIVER_LOG"
          break
      fi
      echo "  --- rep $rep/$REPS"
      # RANDOM_PINS: variedad de GEOMETRIA a tasa constante, que es el escalado aleatorio que
      # este cluster admite. Sortear el paralelismo GLOBAL no sirve — con doce slots los unicos
      # anchos son 4 y 8, y la tasa que satura el 8 deja al 4 con una sola subtarea por fuente,
      # insostenible (medido el 2026-09-27: el job se atasco y se perdio la noche). Fijar UN
      # vertice a un ancho menor deja la tasa y el numero de fuentes intactos, y produce slices
      # con niveles de demanda distintos — la condicion para que dos emplazamientos se puedan
      # distinguir. Formato: "vertice:ancho" separados por espacios, y "none" para sin fijar.
      if [ -n "${RANDOM_PINS:-}" ]; then
          RP_ARR=($RANDOM_PINS)
          RP_PICK=${RP_ARR[$(( RANDOM % ${#RP_ARR[@]} ))]}
          if [ "$RP_PICK" = none ]; then
              PIN_VERTEX=""; PIN_PARALLELISM=""
              echo "    [rep$rep] geometria sorteada: sin vertice fijado"
          else
              PIN_VERTEX="${RP_PICK%%:*}"; PIN_PARALLELISM="${RP_PICK##*:}"
              echo "    [rep$rep] geometria sorteada: $PIN_VERTEX fijado a $PIN_PARALLELISM"
          fi
      fi
      for STEP in $SCHEDULE; do
          case "$STEP" in
              *\*) TARGET="${STEP%\*}"; HOLD="$NARROW_HOLD"; KIND="measured"
                   # Sorteado por repeticion, no por celda: dentro de un job cada rescale es una
                   # decision nueva, asi que variar aqui da varias geometrias por entrenamiento.
                   # $RANDOM se evalua en ESTE shell, no dentro de una sustitucion de
                   # comandos: en un subshell hereda una semilla derivada del padre y del PID,
                   # y sale sesgado — probado, siete unos de ocho tiradas sobre dos valores.
                   if [ -n "${RANDOM_TARGETS:-}" ]; then
                       RT_ARR=($RANDOM_TARGETS)
                       TARGET=${RT_ARR[$(( RANDOM % ${#RT_ARR[@]} ))]}
                       echo "    [rep$rep] ancho medido sorteado: paralelismo $TARGET"
                   fi ;;
              *)   TARGET="$STEP";      HOLD="$WIDE_HOLD";   KIND="transition" ;;
          esac
          set_parallelism "$JOB_ID" "$TARGET" "rep$rep p=$TARGET $KIND" "$DRIVER_LOG"
          if [ "${RESCALE_MISSES:-0}" -ge "${MAX_RESCALE_MISSES:-3}" ]; then
              echo "  ! $RESCALE_MISSES reescalados seguidos sin aterrizar — el job no se" \
                   "recupera solo, se aborta el brazo en la rep $rep de $REPS" | tee -a "$DRIVER_LOG"
              break 2
          fi
          # WHAT WAS PLACED, for the RL arm (2026-09-17). Its placement is keyed by task, and
          # which STAGE each slice holds only exists in the layout the fork writes on each
          # decision, overwritten by the next one. Kept per measured step, so a fixed plan can
          # be checked against what actually ran rather than against what was intended.
          # FOR EVERY ARM, not just RL (2026-09-21). Two of STOCK's six jobs collapsed to
          # ~22000 rec/s against ~34000 for the rest, and the campaign could not say why: the
          # number of slices on `tm-1-slow` did not discriminate — STOCK produced both 37232
          # and 21293 with four of them there — so the answer has to be WHICH stage landed
          # where, and that only exists in the layout the fork writes at each decision. It
          # was being saved for the RL arm alone, so the arm whose failure needed explaining
          # was the one with no record of what it did.
          #
          # ONLY THE RL ARM WRITES THAT FILE (found 2026-09-30). STOCK, LPT and LPT_ORACLE never
          # touch it, so for them this copied whatever the last RL decision left — in the
          # oracle campaign, a 7-slice layout from the end of a training run, saved into three
          # 8-slice cells as if they had produced it. For those arms the record is the
          # [THESIS_ASSIGN] line in thesis-assign.log (slice# -> TaskManager).
          if [ "$KIND" = measured ] && [ "$FORK_ARM" = RL ]; then
              docker exec minikube cat /var/thesis/slices \
                  > "$CELL_DIR/slices-rep${rep}.txt" 2>/dev/null || true
          fi
          sleep "$HOLD"
      done
  done

  kill "$CONTROLLER_PID" 2>/dev/null
  wait "$CONTROLLER_PID" 2>/dev/null
  if [ -n "$AGENT_PID" ]; then
      kill "$AGENT_PID" 2>/dev/null
      wait "$AGENT_PID" 2>/dev/null
  fi

  # Read the job graph BEFORE cancelling: once the job is gone the REST API keeps
  # only a stub.
  jm_curl "/jobs/$JOB_ID" > "$CELL_DIR/job-details.json"
  kubectl logs -n "$NAMESPACE" "$JM_POD" --since="$((JOB_DURATION + 200))s" 2>/dev/null |
      grep -E "THESIS_ASSIGN|THESIS_ARM|THESIS_PLAN" > "$CELL_DIR/thesis-assign.log"

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
SLOT_SHARING="$SLOT_SHARING" PAYLOAD_BYTES="$PAYLOAD_BYTES" \
RECOVERY_INTERVAL="$RECOVERY_INTERVAL" STOCK_JM="${STOCK_JM:-0}" \
JM_COST_WEIGHTS="$(kubectl get pod -n "$NAMESPACE" "$JM_POD" -o jsonpath='{range .spec.containers[0].env[?(@.name)]}{.name}={.value} {end}' 2>/dev/null | tr ' ' '\n' | grep '^THESIS_COST' | tr '\n' ' ')" \
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
    "slot_sharing": os.environ.get("SLOT_SHARING", "") or "SHARED",
    "payload_bytes": os.environ.get("PAYLOAD_BYTES", "") or "0",
    "recovery_sample_interval_s": os.environ.get("RECOVERY_INTERVAL", ""),
    # 1 = the JobManager ran UNMODIFIED Flink and no arm was in force. Without this a stock
    # baseline is indistinguishable from a fork campaign in which every arm tied.
    "stock_jobmanager": int(os.environ.get("STOCK_JM", "0")),
    # Read off the RUNNING JobManager, not from this shell: the weights live in the pod's
    # environment and are set at deploy time, so a campaign cannot change them and must not
    # claim to. "" means the pod does not declare it and the fork's default applies.
    "cost_weights": os.environ.get("JM_COST_WEIGHTS", ""),
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
