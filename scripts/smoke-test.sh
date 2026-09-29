#!/bin/bash
# ============================================
# Five minutes that decide whether a campaign is worth an hour
# ============================================
#
# WHY THIS EXISTS (2026-09-07). Four campaigns in a row were lost not to the
# system under test but to the instrument, and each failure was only visible after
# the campaign had run:
#
#   * the controller reached Flink over a port-forward that had died, so eight
#     rescales produced three episodes and nothing errored;
#   * the retries added to fix that then blinded it for minutes at a time;
#   * `position == 0` read one source of two — a quarter of the offered load —
#     which is why both arms looked identical in throughput;
#   * and a pre-check meant to save time skipped every epoch, because
#     `slots-available` is zero by design while slot.idle.timeout holds the pool.
#
# All four are visible in ONE repetition. This runs that repetition and checks the
# four things a campaign silently depends on, so a broken instrument costs five
# minutes instead of an hour.
#
# Usage:
#   scripts/smoke-test.sh                      # q8, PER_STAGE, one rep of LPT
#   RATE=6500 SLOT_SHARING=SHARED scripts/smoke-test.sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ARM="${ARM:-LPT}"
QUERY="${QUERY:-q8}"
RATE="${RATE:-6500}"
SLOT_SHARING="${SLOT_SHARING:-PER_STAGE}"
SUBMIT_PAR="${SUBMIT_PAR:-3}"
TARGET_PAR="${TARGET_PAR:-2}"
MAX_EVENT_AGE="${MAX_EVENT_AGE:-5000}"
# MEASURE ON THE WAY UP. Flink's adaptive scheduler lowers parallelism by dropping
# subtasks from slots it already holds — no new slots, no call to the assigner, no
# placement decision. Raising it forces the scheduler to acquire slots and it must
# assign them. Measured 2026-09-07: with the measured step on the way DOWN, not one
# THESIS_ASSIGN line ever appeared at the measured width, so four "measurements" per
# cell all observed the submission's placement running narrower. Measuring on the
# way up puts the decision and the measurement in the same step.
SCHEDULE="${SCHEDULE:-${SUBMIT_PAR}* ${TARGET_PAR}}"
JOB_CLASS="${JOB_CLASS:-com.thesis.benchmark.nexmark.ref.RefNexmarkJob}"

DRIVER_LOG="$(mktemp "${TMPDIR:-/tmp}/smoke-driver.XXXXXX.log")"
echo "=========================================="
echo "  Prueba de humo — $QUERY a $RATE rec/s, $SLOT_SHARING, brazo $ARM"
echo "  horario: $SCHEDULE   (* = paso medido)"
echo "  una repetición con las MISMAS ventanas que la campaña — acortarlas la"
echo "  vuelve no representativa: un reescalado de Q8 redistribuye estado y no"
echo "  cabe en 80 segundos, así que Flink fusiona la ida y la vuelta y el paso"
echo "  medido nunca llega a existir (medido 2026-09-07)."
echo "=========================================="

# TWO REPETITIONS, NOT ONE. The first scale-down of a job does not re-run the
# assigner — Flink drops subtasks from the slots it already holds — so a one-rep
# smoke test can never see a real placement decision and reports a failure that is
# an artefact of its own brevity. The cycle down-up-down is the shortest run that
# exercises what a campaign actually measures.
ARMS="$ARM" QUERY="$QUERY" RATE="$RATE" REPS="${REPS:-2}" \
SLOT_SHARING="$SLOT_SHARING" SUBMIT_PAR="$SUBMIT_PAR" TARGET_PAR="$TARGET_PAR" \
MAX_EVENT_AGE="$MAX_EVENT_AGE" JOB_CLASS="$JOB_CLASS" SCHEDULE="$SCHEDULE" \
TM_DEPLOYMENTS="${TM_DEPLOYMENTS:-flink-tm-fast:1 flink-tm-medium:1 flink-tm-slow:1}" \
DRAIN_DEPLOYMENTS="${DRAIN_DEPLOYMENTS:-flink-taskmanager:0}" \
WARMUP="${WARMUP:-60}" WINDOW="${WINDOW:-60}" PUBLISH_LOADS="${PUBLISH_LOADS:-0}" \
    "$SCRIPT_DIR/run-placement-experiment.sh" > "$DRIVER_LOG" 2>&1 || true

RUN=$(ls -td "$ROOT_DIR"/results/placement-experiment/*/ 2>/dev/null | head -1)
[ -n "$RUN" ] || { echo "FALLA: la corrida no produjo directorio" >&2; exit 1; }
echo ""
echo "corrida: $RUN"

# NEVER SWALLOW THE DRIVER'S OUTPUT. On 2026-09-07 the driver exited on an early
# validation, left an empty results directory, and this script reported six checks
# failing with no clue why — the one line that said what was wrong had gone to
# /dev/null. The guard must look at THIS run's directory: a glob over all of them
# matches some older successful run and hides the failure all over again.
if ! ls "$RUN"/*/episodes-*.csv >/dev/null 2>&1; then
    echo ""
    echo "El driver no produjo episodios en esta corrida. Sus últimas líneas:"
    tail -25 "$DRIVER_LOG" | sed 's/^/  | /'
fi
echo ""

RATE="$RATE" RUN="$RUN" python3 - <<'PY'
import csv, glob, os, re, subprocess, sys

run = os.environ["RUN"]
rate = float(os.environ["RATE"])
failures = []


def check(name, ok, detail):
    print(f"  [{'PASA ' if ok else 'FALLA'}] {name}: {detail}")
    if not ok:
        failures.append(name)


episodes = []
for path in glob.glob(os.path.join(run, "*", "episodes-*.csv")):
    episodes.extend(csv.DictReader(open(path)))

# 1. Did the observer see anything at all?
epochs = 0
for path in glob.glob(os.path.join(run, "*", "arm-controller.log")):
    epochs += sum(1 for line in open(path) if line.startswith("[epoch"))
check("el controlador registró epochs", epochs > 0, f"{epochs} epochs")

# 2. Is throughput the WHOLE job's, or one source of several? A reference Nexmark
#    query has a source per event type; reading only the first understates the rate
#    and hides any difference between arms. The bar is NOT "close to the requested
#    rate" — above capacity the job is meant to fall short, and does. It is "more
#    than any single source could supply on its own": Q8 splits the offered rate
#    one part persons to three parts auctions, so anything at or below a quarter is
#    the single-source bug rather than saturation.
if episodes:
    rps = max(float(e.get("source_out_rps") or 0) for e in episodes)
    floor = rate / 4 * 1.5
    check("source_out_rps suma todas las fuentes",
          rps > floor, f"{rps:.0f}; una sola fuente daría ~{rate/4:.0f}")
else:
    check("source_out_rps suma todas las fuentes", False, "sin episodios")

# 3. Did any episode survive the credit rule? An uncredited campaign is an empty one.
credited = [e for e in episodes if (e.get("creditable") or "").strip() in ("1", "true", "True")]
notes = {(e.get("credit_note") or "").strip() for e in episodes if e not in credited}
check("al menos un episodio acreditado", bool(credited),
      f"{len(credited)} de {len(episodes)}"
      + (f" — motivos: {', '.join(sorted(n for n in notes if n))}" if notes else ""))

# 4. Did the MEASURED step actually happen, and with a choice? Two failures hide
#    here. If only the submission width appears, the rescale never materialised —
#    Flink coalesced it with the transition back and the campaign measured the
#    submission placement all along. And if freeSlots equals slices the assigner was
#    handed exactly as many slots as it had slices, so both arms are forced into the
#    same placement however long the campaign runs.
# READ THE JOBMANAGER'S OWN LOG, not the copy the driver collects. That copy is
# written once at the end of a cell and has come back empty repeatedly — on
# 2026-09-07 the controller demonstrably matched assignments for two episodes
# while the collected file had zero lines, so three checks failed on a
# transcription problem rather than on anything the experiment did.
lines = []
for path in glob.glob(os.path.join(run, "*", "thesis-assign.log")):
    lines.extend(open(path).read().splitlines())
if not any("THESIS_ASSIGN" in l for l in lines):
    try:
        pod = subprocess.run(
            ["kubectl", "get", "pod", "-n", "flink", "-l", "component=jobmanager",
             "--field-selector=status.phase=Running",
             "--sort-by=.metadata.creationTimestamp",
             "-o", "jsonpath={.items[-1:].metadata.name}"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if pod:
            out = subprocess.run(["kubectl", "logs", "-n", "flink", pod],
                                 capture_output=True, text=True, timeout=90).stdout
            lines = [l for l in out.splitlines() if "THESIS_ASSIGN" in l]
            print("  (el log recogido estaba vacío; leído del JobManager)")
    except (subprocess.SubprocessError, OSError):
        pass

slack = []
for line in lines:
    m = re.search(r"slices=(\d+) freeSlots=(\d+)", line)
    if m:
        slack.append((int(m.group(1)), int(m.group(2))))
widths = sorted({s for s, _f in slack})
check("hubo más de una decisión de emplazamiento", len(slack) > 1,
      f"{len(slack)} líneas THESIS_ASSIGN, anchos {widths or 'ninguno'}"
      + ("" if len(slack) > 1 else " — solo la sumisión: el reescalado no llamó al asignador"))

# The assigner running is not the same as it running WHERE WE MEASURED. If every
# logged assignment is at the submission width and the episodes are at another, the
# measurement and the decision are in different steps — which is exactly the defect
# that made four campaigns measure nothing.
measured_widths = {int(e["slices"]) for e in episodes if (e.get("slices") or "").isdigit()}
decided_widths = set(widths)
check("la decisión y la medición coinciden en el mismo ancho",
      bool(measured_widths & decided_widths),
      f"medido en {sorted(measured_widths) or '—'}, decidido en {sorted(decided_widths) or '—'}")
best = max((f - s for s, f in slack), default=None)
check("el asignador tuvo holgura", best is not None and best > 0,
      f"mejor holgura {best} slots" if best is not None else "sin líneas THESIS_ASSIGN")

print()
if failures:
    print("NO LANZAR LA CAMPAÑA. Falla: " + ", ".join(failures))
    sys.exit(1)
print("Instrumento sano: la campaña larga puede correr.")
PY
