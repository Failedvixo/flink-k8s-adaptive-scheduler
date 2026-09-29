#!/usr/bin/env python3
"""How much state each operator holds, per subtask, against the memory its slot offers.

WHY THIS EXISTS. The proposed second dimension of the agent's state — «is this operator
bound by CPU or by state?» — is only worth adding if Q8's operators actually differ on it.
The suspicion is that they do not: that the join is simply the most expensive on BOTH axes,
in which case every slice lands in the same profile and the richer table degenerates into
the current one. That is a measurement, not an opinion.

Checkpointed state size is the right proxy for the demand, rather than RocksDB cache
misses, because it is a property of the OPERATOR: an operator placed on a memory-rich
machine stops missing the cache and would look as though it no longer needed memory, which
is the same self-confirming loop that cost the campaign of 2026-09-22 — only oscillating
instead of stuck.

Read it against the managed memory a slot offers on each machine (96 / 48 / 32 MB at the
time of writing): an operator whose per-subtask state exceeds that budget is spilling to
disk, and IS state-bound wherever it lands.

Usage: scripts/state-size.py          # needs a RUNNING job with at least one checkpoint
"""
import json
import subprocess
import sys


def jm_curl(pod, path):
    out = subprocess.run(
        ["kubectl", "exec", "-n", "flink", pod, "--", "curl", "-s", "-m", "20",
         f"http://localhost:8081{path}"], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout)
    except (ValueError, json.JSONDecodeError):
        return None


def main():
    pod = subprocess.run(
        ["kubectl", "get", "pods", "-n", "flink", "-l", "component=jobmanager",
         "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True).stdout.strip()
    if not pod:
        print("ERROR: no hay JobManager en ejecución", file=sys.stderr)
        return 2

    overview = jm_curl(pod, "/jobs/overview") or {}
    running = [j for j in overview.get("jobs", []) if j.get("state") == "RUNNING"]
    if not running:
        print("ERROR: no hay job RUNNING", file=sys.stderr)
        return 2
    jid = running[0]["jid"]

    detail = jm_curl(pod, f"/jobs/{jid}") or {}
    names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
    pars = {v["id"]: v.get("parallelism", 1) for v in detail.get("vertices", [])}

    stats = jm_curl(pod, f"/jobs/{jid}/checkpoints") or {}
    completed = (stats.get("latest") or {}).get("completed")
    if not completed:
        print("ERROR: el job todavía no completó un checkpoint", file=sys.stderr)
        return 2
    cid = completed["id"]

    details = jm_curl(pod, f"/jobs/{jid}/checkpoints/details/{cid}") or {}
    tasks = details.get("tasks", {})
    if not tasks:
        print("ERROR: el checkpoint no trae desglose por tarea", file=sys.stderr)
        return 2

    print(f"job {jid[:8]}  checkpoint #{cid}  "
          f"total {details.get('state_size', 0) / 1e6:.1f} MB")
    print()
    print(f"{'vértice':32} {'par':>3} {'estado total':>13} {'por subtarea':>13}  cabe en")
    rows = []
    for vid, t in tasks.items():
        size = t.get("state_size", 0)
        par = pars.get(vid, t.get("num_subtasks", 1)) or 1
        rows.append((size, vid, par))
    for size, vid, par in sorted(rows, reverse=True):
        per = size / par
        # The managed memory a slot offers on each machine, smallest first.
        fits = [b for b, mb in (("slow", 32), ("medium", 48), ("fast", 96))
                if per <= mb * 1e6]
        where = "/".join(fits) if fits else "NINGUNA — desborda a disco"
        print(f"{names.get(vid, vid)[:32]:32} {par:3} "
              f"{size / 1e6:10.1f} MB {per / 1e6:10.1f} MB  {where}")
    print()
    print("Lectura: si un solo operador concentra el estado y su tamaño por subtarea supera")
    print("el presupuesto de las máquinas chicas, el eje 'estado' distingue de verdad. Si el")
    print("estado está repartido parejo o cabe en todas, el perfil de dos ejes no aporta y la")
    print("tabla nueva degeneraría igual que la actual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
