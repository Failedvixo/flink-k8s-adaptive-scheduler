#!/usr/bin/env python3
"""Does the join's state actually fit in the memory its slot gives it?

THE ONLY WAY TO SEPARATE MEMORY FROM CPU ON THIS CLUSTER. Managed memory per slot
(96/48/32 MB) descends in exactly the same order as cores per slot (2.0/0.5/0.17), so any
effect measured BETWEEN machines could be either. This measures the same operator, in the
same job, over the same data, in two different memory budgets: `plans/join_split.plan` puts
join subtask 0 on `tm-3-fast` (96 MB/slot) and subtask 1 on `tm-1-slow` (32 MB/slot). The
keyed shuffle gives each half the keys, so the work is symmetric by construction and the only
asymmetry left is the cache.

WHY IT MATTERS. Measured 2026-09-28, the join holds ALL of Q8's state — 184.2 MB checkpointed,
92.1 MB per subtask, every other operator exactly 0.0. That exceeds two of the three budgets.
But exceeding the cache is a NECESSARY condition, not a sufficient one: if the hot set of a
10-second tumbling window is small, a small cache may serve it perfectly well and the total
size is irrelevant. The cache miss ratio is what tells the two apart.

Read the miss ratio, not the throughput: a keyed join backpressures its inputs when one
subtask lags, so both subtasks end up at the same rate whatever the cache does.

Usage: scripts/join-memory-check.py        # needs the job running with the split plan
"""
import json
import subprocess
import sys

NS = "flink"
# The managed memory each machine gives one slot, for reading the result against.
BUDGET = {"tm-3-fast": 96, "tm-2-medium": 48, "tm-1-slow": 32}


def jm_curl(pod, path):
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, pod, "--", "curl", "-s", "-m", "20",
         f"http://localhost:8081{path}"], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout)
    except (ValueError, json.JSONDecodeError):
        return None


def main():
    pod = subprocess.run(
        ["kubectl", "get", "pods", "-n", NS, "-l", "component=jobmanager",
         "--field-selector=status.phase=Running",
         "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True).stdout.strip()
    jobs = [j for j in (jm_curl(pod, "/jobs/overview") or {}).get("jobs", [])
            if j.get("state") == "RUNNING"]
    if not jobs:
        print("ERROR: no hay job RUNNING", file=sys.stderr)
        return 2
    jid = jobs[0]["jid"]

    detail = jm_curl(pod, f"/jobs/{jid}") or {}
    join = next((v for v in detail.get("vertices", []) if "join" in v.get("name", "")), None)
    if not join:
        print("ERROR: no encuentro el vértice del join", file=sys.stderr)
        return 2

    where = {s["subtask"]: s.get("taskmanager-id", "?")
             for s in (jm_curl(pod, f"/jobs/{jid}/vertices/{join['id']}") or {}).get("subtasks", [])}
    if len(set(where.values())) < 2:
        print(f"! las {len(where)} subtareas del join están en la MISMA máquina "
              f"({next(iter(where.values()))}): no hay comparación posible.", file=sys.stderr)
        print("  Hace falta el plan plans/join_split.plan aplicado.", file=sys.stderr)
        return 1

    wanted = ["rocksdb_block_cache_hit", "rocksdb_block_cache_miss", "rocksdb_stall_micros",
              "rocksdb_bytes_read", "rocksdb_bytes_written", "rocksdb_iter_bytes_read"]
    print(f"job {jid[:8]}   vértice {join['name']}   par {join.get('parallelism')}")
    print()
    print(f"{'sub':>3} {'máquina':14} {'MB/slot':>7} {'fallos':>8} {'stall':>9} "
          f"{'leído':>9} {'escrito':>9}")
    rows = {}
    for i in sorted(where):
        query = ",".join(f"{join['name']}.{w}" for w in wanted)
        payload = jm_curl(pod, f"/jobs/{jid}/vertices/{join['id']}/subtasks/{i}/metrics?get={query}") or []
        v = {}
        for m in payload:
            try:
                v[m["id"].split(".")[-1]] = float(m["value"])
            except (KeyError, TypeError, ValueError):
                pass
        hit, miss = v.get("rocksdb_block_cache_hit", 0), v.get("rocksdb_block_cache_miss", 0)
        ratio = miss / (hit + miss) if hit + miss else float("nan")
        rows[i] = (ratio, v)
        tm = where[i]
        print(f"{i:3} {tm:14} {BUDGET.get(tm, 0):7} {ratio:8.1%} "
              f"{v.get('rocksdb_stall_micros', 0) / 1e6:8.1f}s "
              f"{v.get('rocksdb_bytes_read', 0) / 1e9:8.2f}G "
              f"{v.get('rocksdb_bytes_written', 0) / 1e9:8.2f}G")

    print()
    big = max(rows, key=lambda i: BUDGET.get(where[i], 0))
    small = min(rows, key=lambda i: BUDGET.get(where[i], 0))
    r_big, r_small = rows[big][0], rows[small][0]
    if r_big != r_big or r_small != r_small:
        print("  sin lecturas de caché todavía — espera a que RocksDB se caliente")
        return 0
    print(f"  la subtarea con {BUDGET.get(where[small])} MB falla {r_small:.1%} de las veces; "
          f"la de {BUDGET.get(where[big])} MB, {r_big:.1%}")
    if r_small > r_big * 1.5:
        print("  LA MEMORIA MUERDE: el presupuesto pequeño no sostiene el conjunto caliente,")
        print("  así que este operador tiene una razón física para preferir una máquina con")
        print("  más memoria aunque tenga menos CPU. La segunda dimensión existe.")
    else:
        print("  LA MEMORIA NO MUERDE: el conjunto caliente cabe incluso en el presupuesto")
        print("  pequeño, y los 92 MB de estado total no llegan a ser una restricción.")
        print("  Hay que buscar la segunda dimensión en otro recurso.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
