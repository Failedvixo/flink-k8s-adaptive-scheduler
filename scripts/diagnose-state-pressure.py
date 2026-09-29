#!/usr/bin/env python3
"""Is this operator limited by CPU or by its state backend?

WHY THIS EXISTS (2026-09-14). Two attempts to measure whether memory matters for
placement both failed on the same rock: the raw RocksDB counters are not comparable
between machines. Measured the same day, the join's cache hit rate was WORSE on the
fast machine (21-25%) than on the slow one (26-27%) and it stalled twice as long
(38 s vs 19 s) — not because `fast` has less memory per slot, it has more (48 MB
against 32), but because four cores push far more state churn per second than one
does. The counters track throughput, not scarcity.

So nothing here is reported raw. Stall is reported as a FRACTION OF WALL TIME, which
is the only form in which "this task spent 12.6% of its life waiting for RocksDB" is
a statement about pressure rather than about speed. Bytes are reported per record for
the same reason.

The comparison this is built for holds the placement fixed and changes ONE machine's
managed memory, so the cores, the slots and the assignment are identical between the
two runs and memory is the only difference. Comparing across machines does not work,
which is what the two failed attempts established.

Usage:
  scripts/diagnose-state-pressure.py                    # the running job, heaviest stateful vertex
  scripts/diagnose-state-pressure.py --vertex join      # match vertices by name
"""
import argparse
import json
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--vertex", default="join",
                help="substring of the vertex name to diagnose (default: join)")
ap.add_argument("--namespace", default="flink")
args = ap.parse_args()


def jm_pod():
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", args.namespace, "-l", "component=jobmanager",
         "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True).stdout.strip()
    if not out:
        sys.exit("ERROR: no running JobManager pod")
    return out


POD = jm_pod()


def rest(path):
    """One attempt, short timeout. A retry inside a diagnostic hides a dead cluster as
    a slow one, which is the failure mode that cost four campaigns in September."""
    out = subprocess.run(
        ["kubectl", "exec", "-n", args.namespace, POD, "--",
         "curl", "-s", "-m", "10", f"http://localhost:8081{path}"],
        capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        sys.exit(f"ERROR: could not read {path}")


jobs = [j for j in rest("/jobs").get("jobs", []) if j.get("status") == "RUNNING"]
if not jobs:
    sys.exit("ERROR: no RUNNING job")
jid = jobs[0]["id"]
detail = rest(f"/jobs/{jid}")
runtime_s = detail.get("duration", 0) / 1000.0

print(f"job {jid[:8]}  activo {runtime_s/60:.1f} min")

# Offered load, summed over EVERY source. Reading one of the two sources reported a
# quarter of the real rate for weeks (Q8 has a person source and an auction source),
# so the sum is taken over every source vertex.
#
# THE TOPOLOGY COMES FROM /plan, NOT FROM /jobs/{id} (fixed 2026-09-14). The vertex
# entries in the job detail carry no `inputs` field at all in Flink 2.3, so testing for
# one marked EVERY vertex as a source and the first run of this script reported a total
# of 108376 rec/s for a job offered 38000 — it had added the whole pipeline together,
# counting each record once per operator it passed through.
plan_nodes = rest(f"/jobs/{jid}/plan").get("plan", {}).get("nodes", [])
source_ids = {n["id"] for n in plan_nodes if not n.get("inputs")}
if not source_ids:
    sys.exit("ERROR: the plan reports no source vertices")

total_in = 0.0
print("\nfuentes:")
for v in detail["vertices"]:
    if v["id"] not in source_ids:
        continue
    m = rest(f"/jobs/{jid}/vertices/{v['id']}/subtasks/metrics"
             f"?get=numRecordsOutPerSecond")
    rate = sum(float(x.get("sum", 0)) for x in m if x["id"] == "numRecordsOutPerSecond")
    total_in += rate
    print(f"  {v['name'][:40]:42} {rate:9.0f} rec/s")
print(f"  {'TOTAL':42} {total_in:9.0f} rec/s")

targets = [v for v in detail["vertices"] if args.vertex.lower() in v["name"].lower()]
if not targets:
    sys.exit(f"ERROR: no vertex matching '{args.vertex}'")

for v in targets:
    vid = v["id"]
    print(f"\n{v['name']}  (paralelismo {v['parallelism']})")
    where = {s["subtask"]: s.get("taskmanager-id", "?")
             for s in rest(f"/jobs/{jid}/vertices/{vid}").get("subtasks", [])}

    ids = [m["id"] for m in rest(f"/jobs/{jid}/vertices/{vid}/subtasks/metrics")
           if "rocksdb" in m["id"] or m["id"] in ("busyTimeMsPerSecond", "numRecordsInPerSecond")]
    if not ids:
        print("  (sin métricas de RocksDB — ¿operador sin estado, o métricas nativas apagadas?)")
        continue

    print(f"  {'sub':>3} {'máquina':16} {'busy ms/s':>10} {'aciertos':>9}"
          f" {'stall %reloj':>13} {'lectura/reg':>12}")
    for i in sorted(where):
        vals = {m["id"]: float(m["value"])
                for m in rest(f"/jobs/{jid}/vertices/{vid}/subtasks/{i}/metrics"
                              f"?get={','.join(ids)}")}
        get = lambda suffix: next(
            (val for key, val in vals.items() if key.endswith(suffix)), 0.0)

        hit, miss = get("rocksdb_block_cache_hit"), get("rocksdb_block_cache_miss")
        hit_rate = 100 * hit / (hit + miss) if (hit + miss) else float("nan")
        # Justin (arXiv 2505.19739) reads below 80% as state-bound rather than CPU-bound.
        flag = " *" if hit_rate < 80 else ""
        # The figure that is actually comparable between runs: microseconds of stall
        # against microseconds of wall clock.
        stall_pct = 100 * get("rocksdb_stall_micros") / (runtime_s * 1e6) if runtime_s else 0
        records = get("numRecordsInPerSecond") * runtime_s
        per_rec = get("rocksdb_bytes_read") / records if records else float("nan")

        print(f"  {i:>3} {where[i][:16]:16} {vals.get('busyTimeMsPerSecond', 0):10.0f}"
              f" {hit_rate:8.1f}%{flag} {stall_pct:12.1f}% {per_rec:11.0f} B")

print("\n  * por debajo del 80% de aciertos = acotado por estado (criterio de Justin).")
print("  Comparable ENTRE CORRIDAS con el mismo emplazamiento; NO entre máquinas de")
print("  distinta velocidad — los contadores siguen al throughput, no a la escasez.")
