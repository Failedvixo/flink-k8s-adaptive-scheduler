#!/bin/bash
# ============================================
# Per-arm campaign over the forked slot assigner
# ============================================
#
# Runs every placement arm on its own, for every Nexmark query, under every
# arrival distribution — the same shape as the earlier per-strategy campaign on
# the Kubernetes side, but the thing being varied is now the slice→slot decision
# inside the JobManager.
#
# The point of running the arms alone is to find WHERE they disagree. A scenario
# in which every arm scores the same teaches a meta-scheduler nothing, so the
# scenario with the largest spread across arms is the one worth training on;
# scripts/analyse_fork_campaign.py computes that spread from these results.
#
# Each cell: publish the arm -> autoscaler submits the job and drives rescales
# (the only moments the arm is applied) -> arm_controller.py records the
# per-rescale Flink metrics -> results land in
#   results/fork-campaign/<query>-<dist>/<ARM>/
#
# The meta-schedulers are cells too: pass ARMS="BANDIT SARSA" to run the
# learners over the same grid, which is what makes them comparable with the
# fixed arms and with STOCK.
#
# Disk: only what training and comparison actually read is kept —
# summary.json, the per-rescale episodes CSV, and the assigner's own log lines.
# The autoscaler logs and the full job graph are summarised and then deleted,
# because a full grid is hundreds of cells and the previous campaign's raw logs
# were what filled the disk. KEEP=all disables the pruning while debugging.
#
# Usage:
#   scripts/run-fork-campaign.sh
#   PILOT=1 QUERIES=q5 scripts/run-fork-campaign.sh   # 5 min, SINE only
#   ARMS="ACO GA" QUERIES=q5 DISTS=SINE scripts/run-fork-campaign.sh
#   ARMS="BANDIT SARSA" scripts/run-fork-campaign.sh
#
# Env: ARMS QUERIES DISTS DURATION RATE TM_REPLICAS WARMUP WINDOW KEEP PILOT

set -u

# A short shakedown run: one distribution, five minutes, and a measurement
# window that fits inside it. Long enough to prove the machinery end to end,
# too short for the numbers to mean anything.
if [ "${PILOT:-0}" = "1" ]; then
    DISTS="${DISTS:-SINE}"
    DURATION="${DURATION:-300}"
    WARMUP="${WARMUP:-30}"
    WINDOW="${WINDOW:-30}"
    # The autoscaler's default cadence (20s settle + 10s poll + 30s cooldown)
    # fits barely one scaling action into five minutes, and an arm that is never
    # applied cannot be measured. Tighten it so a pilot actually exercises the
    # decision path — at the cost of a twitchier autoscaler than the real runs use.
    export POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-10}"
    export COOLDOWN_SEC="${COOLDOWN_SEC:-20}"
    export SCALE_UP_THRESH="${SCALE_UP_THRESH:-50}"
    export SCALE_DOWN_THRESH="${SCALE_DOWN_THRESH:-30}"
    export MAX_CPU_PARALLELISM="${MAX_CPU_PARALLELISM:-8}"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ARMS="${ARMS:-STOCK FCFS ROUND_ROBIN LEAST_LOADED LPT ACO GA}"
QUERIES="${QUERIES:-q5 q8}"
DISTS="${DISTS:-CONSTANT SINE RAMP}"
DURATION="${DURATION:-600}"
TM_REPLICAS="${TM_REPLICAS:-5}"
# Discard the first WARMUP seconds after each rescale: the job restarts from a
# checkpoint and its metrics are meaningless until it has caught up.
#
# Held under different names than the job's own WINDOW (its window operator size,
# exported per cell below) so that setting one cannot silently change the other.
MEASURE_WARMUP="${WARMUP:-60}"
MEASURE_WINDOW="${WINDOW:-60}"
# The caller's rate, if any. Read once: RATE is exported per cell, so consulting
# it again inside the loop would hand every later cell the first cell's rate.
RATE_OVERRIDE="${RATE:-}"
NAMESPACE=flink
OUT_ROOT="${OUT_ROOT:-results/fork-campaign}"

cd "$ROOT_DIR" || exit 1

# ------------------------------------------------------------------
# per-query wiring (mirrors experiment-q5.sh / experiment-q8.sh)
# ------------------------------------------------------------------
query_job_args() {
    echo "$1 ${ZIPF_ALPHA:-0.5} ${HOT_POOL:-1000}"
}

# The operator the autoscaler rescales — and therefore the only vertex whose
# placement the arms get to decide. Taken from the per-query experiment scripts;
# a wrong name here makes the autoscaler abort instead of silently rescaling the
# wrong operator.
query_heavy_vertex() {
    case "$1" in
        q0)  echo "q0-passthrough" ;;
        q1)  echo "q1-currency" ;;
        q2)  echo "q2-selection" ;;
        q3)  echo "q3-state-join" ;;
        q4)  echo "q4-cat-avg" ;;
        q5)  echo "hot-items-count" ;;
        q6)  echo "q6-seller-avg" ;;
        q7)  echo "q7-max-bid" ;;
        q8)  echo "new-users-join" ;;
        q9)  echo "q9-winning-bid" ;;
        q10) echo "q10-sink" ;;
        q11) echo "q11-sessions" ;;
        q12) echo "q12-proc-sessions" ;;
        *)   echo "" ;;
    esac
}

# CONSTANT is driven at a higher base rate than the shaped distributions, whose
# multipliers would otherwise push the peak past what the cluster can absorb.
dist_rate() {
    if [ -n "$RATE_OVERRIDE" ]; then
        echo "$RATE_OVERRIDE"
        return
    fi
    case "$1" in
        CONSTANT) echo 100000 ;;
        *)        echo 60000 ;;
    esac
}

is_meta_arm() {
    [ "$1" = "BANDIT" ] || [ "$1" = "SARSA" ]
}

# ------------------------------------------------------------------
# preconditions
# ------------------------------------------------------------------
if ! kubectl get nodes >/dev/null 2>&1; then
    echo "ERROR: cluster unreachable (minikube start)" >&2
    exit 1
fi

JM_POD=$(kubectl get pod -n "$NAMESPACE" -l component=jobmanager \
    --field-selector=status.phase=Running -o jsonpath='{.items[-1:].metadata.name}')
if [ -z "$JM_POD" ]; then
    echo "ERROR: no running JobManager" >&2
    exit 1
fi

FORK_ENABLED=$(kubectl get pod -n "$NAMESPACE" "$JM_POD" \
    -o jsonpath='{.spec.containers[0].env[?(@.name=="THESIS_SLOT_ASSIGNER")].value}')
if [ "$FORK_ENABLED" != "true" ]; then
    echo "ERROR: the JobManager is not running the fork (scripts/deploy-thesis-fork.sh)" >&2
    exit 1
fi

if ! curl -s -m 5 http://localhost:8081/overview >/dev/null; then
    echo "ERROR: no Flink REST at localhost:8081" >&2
    echo "       kubectl port-forward -n $NAMESPACE svc/flink-jobmanager 8081:8081 &" >&2
    exit 1
fi

# The number of TaskManagers decides how much room the assigner has, so a cell
# measured against a different TM count is not comparable with the others. Pin
# it once, here, rather than trusting whatever the previous run left behind.
CURRENT_TMS=$(kubectl get deploy flink-taskmanager -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
if [ "$CURRENT_TMS" != "$TM_REPLICAS" ]; then
    echo "Scaling TaskManagers $CURRENT_TMS -> $TM_REPLICAS"
    kubectl scale deployment flink-taskmanager -n "$NAMESPACE" --replicas="$TM_REPLICAS" >/dev/null
    kubectl rollout status deployment/flink-taskmanager -n "$NAMESPACE" --timeout=180s >/dev/null
fi

LOCAL_JAR="$ROOT_DIR/flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar"
if [ ! -f "$LOCAL_JAR" ]; then
    echo "ERROR: build the benchmark first (cd flink-nexmark-job && mvn package)" >&2
    exit 1
fi
echo "Uploading benchmark jar to $JM_POD..."
kubectl cp "$LOCAL_JAR" "$NAMESPACE/$JM_POD:/tmp/nexmark.jar"

TOTAL=0
for _a in $ARMS; do for _q in $QUERIES; do for _d in $DISTS; do TOTAL=$((TOTAL + 1)); done; done; done
CELL=0

echo "=========================================="
echo "  Fork campaign"
echo "  arms:      $ARMS"
echo "  queries:   $QUERIES"
echo "  dists:     $DISTS"
echo "  duration:  ${DURATION}s per cell   ($TOTAL cells)"
echo "  measure:   warmup ${MEASURE_WARMUP}s + window ${MEASURE_WINDOW}s per rescale"
echo "=========================================="

cleanup_jobs() {
    kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink list 2>/dev/null |
        grep -oP '[0-9a-f]{32}' |
        xargs -r -I {} kubectl exec -n "$NAMESPACE" "$JM_POD" -- flink cancel {} >/dev/null 2>&1
}

for ARM in $ARMS; do
  for QUERY in $QUERIES; do
    for DIST in $DISTS; do
      CELL=$((CELL + 1))
      DIST_TAG=$(echo "$DIST" | tr '[:upper:]' '[:lower:]')
      CELL_DIR="$OUT_ROOT/${QUERY}-${DIST_TAG}/${ARM}"
      mkdir -p "$CELL_DIR"

      echo ""
      echo "######################################################"
      echo "###  [$CELL/$TOTAL] arm=$ARM  query=$QUERY  dist=$DIST"
      echo "######################################################"

      cleanup_jobs
      sleep 5

      if is_meta_arm "$ARM"; then
          # The learner owns the arm file; start it from whatever it learned in
          # earlier cells so the policy accumulates across the campaign.
          echo "  meta-scheduler: $ARM (publishes its own arm)"
      else
          "$SCRIPT_DIR/publish-arm.sh" "$ARM" >/dev/null || exit 1
          echo "  published arm: $ARM"
      fi

      RATE="$(dist_rate "$DIST")"
      export RATE
      export DURATION ARRIVAL_DIST="$DIST"
      export RESULTS_DIR="$CELL_DIR"
      export JOB_CLASS="com.thesis.benchmark.nexmark.NexmarkRealJob"
      export EXTRA_JOB_ARGS="$(query_job_args "$QUERY")"
      export HEAVY_VERTEX_PATTERN="$(query_heavy_vertex "$QUERY")"
      export PARALLELISM=8 WINDOW=10 CPU_LOAD=2500 MAX_EVENT_AGE=15000
      # Every vertex wide enough to be parallel scales, not just the heavy one.
      # With slot sharing the group's width — and therefore how many slots the job
      # asks for — is set by the WIDEST vertex, so pinning the source leaves
      # freeSlots == slices at every rescale and the arm never gets to decide.
      export SCALE_ALL_VERTICES="${SCALE_ALL_VERTICES:-1}"
      # Start WIDE and let the autoscaler narrow the pipeline. The initial range
      # is applied to every scalable vertex, so starting narrow would throttle the
      # source, starve the heavy vertex, and leave the autoscaler reading an idle
      # signal it never scales up from — the job would simply sit at its floor.
      # It is the scale-DOWNS that create freeSlots > slices anyway.
      export MIN_CPU_PARALLELISM="${MIN_CPU_PARALLELISM:-2}"
      export MAX_CPU_PARALLELISM="${MAX_CPU_PARALLELISM:-8}"
      export INITIAL_CPU_PAR="${INITIAL_CPU_PAR:-8}"

      # The autoscaler submits the job and drives the rescales; without it the
      # assigner is asked exactly once and the arm barely matters.
      ./autoscaler.sh > "$CELL_DIR/autoscaler-stdout.log" 2>&1 &
      AUTOSCALER_PID=$!

      CONTROLLER_ARGS=(--out-dir "$CELL_DIR" --warmup "$MEASURE_WARMUP" --window "$MEASURE_WINDOW")
      if is_meta_arm "$ARM"; then
          CONTROLLER_ARGS+=(--meta "$(echo "$ARM" | tr '[:upper:]' '[:lower:]')"
                            --qtable "$OUT_ROOT/${ARM}-table.json")
      else
          CONTROLLER_ARGS+=(--fixed-arm "$ARM")
      fi

      # Give the autoscaler time to submit before the controller looks for a job.
      sleep 20
      # -u because the controller is killed with the cell: buffered stdout would be
      # discarded unflushed, and its log is the only record of why an epoch went
      # uncredited.
      timeout $((DURATION + 180)) python3 -u "$SCRIPT_DIR/arm_controller.py" "${CONTROLLER_ARGS[@]}" \
          > "$CELL_DIR/arm-controller.log" 2>&1 &
      CONTROLLER_PID=$!

      wait "$AUTOSCALER_PID"
      kill "$CONTROLLER_PID" 2>/dev/null
      wait "$CONTROLLER_PID" 2>/dev/null

      JOB_ID=$(cat "$CELL_DIR/job-id.txt" 2>/dev/null || echo "")
      # Read the job graph BEFORE cancelling: once the job is gone the REST API
      # keeps only a stub, and the per-vertex parallelism is what tells us the
      # rescales actually happened.
      if [ -n "$JOB_ID" ]; then
          kubectl exec -n "$NAMESPACE" "$JM_POD" -- \
              curl -s "http://localhost:8081/jobs/$JOB_ID" > "$CELL_DIR/job-details.json" 2>/dev/null
      fi
      kubectl logs -n "$NAMESPACE" "$JM_POD" --since="$((DURATION + 300))s" 2>/dev/null |
          grep -E "THESIS_ASSIGN|THESIS_ARM" > "$CELL_DIR/thesis-assign.log"

      python3 "$SCRIPT_DIR/summarise_fork_cell.py" "$CELL_DIR" \
          --arm "$ARM" --query "$QUERY" --dist "$DIST" --rate "$RATE" \
          --duration "$DURATION" --tms "$TM_REPLICAS" \
          --warmup "$MEASURE_WARMUP" --window "$MEASURE_WINDOW" --job-id "$JOB_ID" \
          ${KEEP:+--keep "$KEEP"} || echo "  ! summary failed for $CELL_DIR"

      cleanup_jobs
      echo "  -> $CELL_DIR"
      sleep 10
    done
  done
done

echo ""
echo "=========================================="
echo "  Campaign done — $TOTAL cells in $OUT_ROOT"
echo "  Next: python3 scripts/analyse_fork_campaign.py $OUT_ROOT"
echo "=========================================="
