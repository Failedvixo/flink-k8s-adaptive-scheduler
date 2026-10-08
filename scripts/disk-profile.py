#!/usr/bin/env python3
"""How much each operator writes to RocksDB, and how much of that actually reaches the disk.

WHY (2026-10-08). The agent decides "writes to disk" from rocksdb.bytes.written, which counts
every put — including the ones absorbed by the memtable and never flushed. Q5's counters write
~15 MB/s of it and almost nothing to disk; the agent therefore kept them off the machine with
the capped disk and lost 2-8% against the best plan at 140 000 ev/s. compaction-write-bytes
counts what RocksDB rewrites on disk. If it separates Q3/Q8 (state that reaches the disk) from
Q5 (state that stays in memory), it is the feature the agent should use instead.

Takes two readings of the cumulative counters, INTERVAL seconds apart, and prints per vertex:
logical writes (MB/s), compaction writes to disk (MB/s) and stall (seconds of stall per second).

Usage: scripts/disk-profile.py [INTERVAL]     # default 60 s; needs a RUNNING job on RocksDB
"""
import json
import subprocess
import sys
import time

NS = "flink"
WANTED = ("bytes_written", "compact", "stall")


def jm(pod, path):
    out = subprocess.run(["kubectl", "exec", "-n", NS, pod, "--", "curl", "-s", "-m", "15",
                          f"http://localhost:8081{path}"],
                         capture_output=True, text=True, timeout=40).stdout
    try:
        return json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return None


def reading(pod, jid, vertices, ids):
    """{(vertex name, metric suffix): summed cumulative value over subtasks}."""
    totals = {}
    for v in vertices:
        for i in range(int(v.get("parallelism", 0))):
            key = (v["id"], i)
            if not ids.get(key):
                continue
            got = jm(pod, f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics"
                          f"?get={','.join(ids[key])}") or []
            for m in got:
                try:
                    val = float(m["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                name = (v["name"], m["id"].split(".")[-1])
                totals[name] = totals.get(name, 0.0) + val
    return totals


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    pod = subprocess.run(["kubectl", "get", "pods", "-n", NS, "-l", "component=jobmanager",
                          "--field-selector=status.phase=Running",
                          "-o", "jsonpath={.items[-1:].metadata.name}"],
                         capture_output=True, text=True).stdout.strip()
    jobs = [j for j in (jm(pod, "/jobs/overview") or {}).get("jobs", [])
            if j.get("state") == "RUNNING"]
    if not jobs:
        print("ERROR: no hay job RUNNING", file=sys.stderr)
        return 2
    jid = jobs[0]["jid"]
    vertices = (jm(pod, f"/jobs/{jid}") or {}).get("vertices", [])
    ids, suffixes = {}, set()
    for v in vertices:
        for i in range(int(v.get("parallelism", 0))):
            listing = jm(pod, f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics") or []
            chosen = [m["id"] for m in listing
                      if "rocksdb" in m["id"] and any(w in m["id"] for w in WANTED)]
            if chosen:
                ids[(v["id"], i)] = chosen
                suffixes.update(c.split(".")[-1] for c in chosen)
    if not ids:
        print("ERROR: ningún operador expone métricas de RocksDB", file=sys.stderr)
        return 1
    print(f"métricas encontradas: {', '.join(sorted(suffixes))}")
    if not any("compact" in s for s in suffixes):
        print("! no aparece compaction-write-bytes: no está activada en los TaskManagers")
    print(f"midiendo {interval:.0f} s ...")
    first, t0 = reading(pod, jid, vertices, ids), time.time()
    time.sleep(interval)
    second, t1 = reading(pod, jid, vertices, ids), time.time()
    dt = t1 - t0

    rates = {}
    for (name, suffix), val in second.items():
        rates.setdefault(name, {})[suffix] = (val - first.get((name, suffix), val)) / dt
    print()
    print(f"{'operador':40} {'lógico MB/s':>12} {'compactación MB/s':>18} {'stall s/s':>10}")
    for name, r in sorted(rates.items()):
        logical = sum(v for k, v in r.items() if "bytes_written" in k) / 1e6
        compact = sum(v for k, v in r.items() if "compact" in k and "write" in k) / 1e6
        stall = sum(v for k, v in r.items() if "stall" in k) / 1e6
        print(f"{name[:40]:40} {logical:12.2f} {compact:18.2f} {stall:10.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
