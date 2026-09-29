#!/usr/bin/env python3
"""Sample a running job and derive per-record operator costs.

Split out of scripts/profile-operators.sh on 2026-09-02 after the shell version
failed at runtime: sampling the REST API from bash needed five nested levels of
quoting (bash -> python -c "..." -> python string literals -> JSON), which bash
mis-parsed in a way `bash -n` could not see. Everything that reads metrics or does
arithmetic lives here; the shell script only submits the job and cancels it.

Reads the Flink REST API through `kubectl exec` on the JobManager pod, the way the
rest of the harness does — a port-forward dies silently under load and leaves curl
talking to a dead tunnel.

Usage (normally called by scripts/profile-operators.sh):
    python3 scripts/profile_operators.py --job <jid> --out <dir> --rate 60000
"""

import argparse
import json
import math
import os
import statistics as st
import subprocess
import sys
import time
from collections import defaultdict

NAMESPACE = "flink"
METRICS = ["busyTimeMsPerSecond", "numRecordsInPerSecond",
           "numRecordsOutPerSecond", "numBytesOutPerSecond"]


def jm(pod, path, explain=False):
    """One REST call, returning parsed JSON or None.

    With explain=True the failure is described instead of swallowed. A bare
    "could not read the job" sent us hunting the wrong thing on 2026-09-02: the
    call can fail because the pod name went stale after a JobManager restart,
    because the job died, or because curl itself never ran — and those need
    different fixes.
    """
    try:
        out = subprocess.run(
            ["kubectl", "exec", "-n", NAMESPACE, pod, "--",
             "curl", "-s", "-m", "15", f"http://localhost:8081{path}"],
            capture_output=True, text=True, timeout=40)
    except subprocess.SubprocessError as exc:
        if explain:
            print(f"  kubectl exec falló: {exc}", file=sys.stderr)
        return None
    if out.returncode != 0:
        if explain:
            print(f"  kubectl exec devolvió {out.returncode}: "
                  f"{(out.stderr or '').strip()[:300]}", file=sys.stderr)
        return None
    try:
        return json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        if explain:
            body = (out.stdout or "").strip()
            print(f"  respuesta no-JSON de {path}: "
                  f"{body[:300] if body else '(vacía)'}", file=sys.stderr)
        return None


def finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def median(values):
    values = [v for v in values if not math.isnan(v)]
    return st.median(values) if values else float("nan")


def sample(pod, jid, samples, interval):
    """Per-subtask metric samples, plus which TaskManager each subtask ran on."""
    detail = jm(pod, f"/jobs/{jid}", explain=True)
    if not detail:
        # Say what the cluster thinks is going on rather than only that we failed.
        overview = jm(pod, "/jobs/overview", explain=True)
        if overview:
            print("  jobs que el JobManager conoce:", file=sys.stderr)
            for j in overview.get("jobs", []):
                print(f"    {j.get('jid')}  {j.get('state')}  {j.get('name','')[:60]}",
                      file=sys.stderr)
        else:
            print("  el JobManager tampoco responde /jobs/overview — "
                  "probablemente el pod se reinició y el nombre quedó obsoleto",
                  file=sys.stderr)
        sys.exit(f"ERROR: no pude leer el job {jid}")
    vertices = [(v["id"], v.get("parallelism", 1), v.get("name", v["id"]))
                for v in detail.get("vertices", [])]

    rows = defaultdict(list)
    tm_of = {}
    for round_index in range(samples):
        for vid, par, _name in vertices:
            vdetail = jm(pod, f"/jobs/{jid}/vertices/{vid}")
            hosts = {}
            if vdetail:
                for s in vdetail.get("subtasks", []):
                    hosts[s.get("subtask")] = s.get("taskmanager-id", "?")
            for i in range(par):
                tm_of[(vid, i)] = hosts.get(i, "?")
                m = jm(pod, f"/jobs/{jid}/vertices/{vid}/subtasks/{i}/metrics"
                            f"?get={','.join(METRICS)}")
                got = {x["id"]: x.get("value") for x in m} if m else {}
                rows[(vid, i)].append([finite(got.get(k)) for k in METRICS])
        if round_index < samples - 1:
            time.sleep(interval)
    return detail, rows, tm_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pod", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--samples", type=int, default=9)
    ap.add_argument("--interval", type=float, default=4)
    args = ap.parse_args()

    started = time.time()
    detail, rows, tm_of = sample(args.pod, args.job, args.samples, args.interval)
    # Reported because the nominal sampling time and the real one differ by an order
    # of magnitude, and a job that ends mid-measurement is otherwise silent about it.
    print(f"  muestreo real: {time.time() - started:.0f}s "
          f"(nominal {args.samples * args.interval:.0f}s)")
    if detail.get("state") != "RUNNING":
        print(f"  ! el job terminó durante el muestreo (state={detail.get('state')}); "
              f"las últimas rondas pueden estar incompletas")
    json.dump(detail, open(os.path.join(args.out, "job-details.json"), "w"), indent=2)
    names = {v["id"]: v.get("name", v["id"]) for v in detail.get("vertices", [])}

    # Machine speeds are read for the record and NOT applied. The first version
    # divided every measurement by them, on the theory that the same work costs
    # four times the busy-milliseconds on `slow` (1 core) as on `fast` (4). The
    # data refuted it: filter-auctions reported busy=47 on `fast` against 31 on
    # `medium` — MORE on the faster machine. A subtask is a single thread and uses
    # at most one core, so with the cluster below saturation it gets a whole core
    # anywhere and its busy time is already machine-independent. Normalising
    # injected a 3x artefact into a quantity that had none.
    speeds = {}
    speeds_path = os.path.join(args.out, "speeds.txt")
    if os.path.exists(speeds_path):
        for line in open(speeds_path):
            parts = line.split()
            if len(parts) == 2:
                try:
                    speeds[parts[0]] = float(parts[1])
                except ValueError:
                    pass

    profile = {}
    for (vid, sub), samples in rows.items():
        busy = median([s[0] for s in samples])
        rec_in = median([s[1] for s in samples])
        rec_out = median([s[2] for s in samples])
        bytes_out = median([s[3] for s in samples])
        # A source has no input, so its throughput is its output. Everything
        # downstream is priced per record IT consumed — Q8's join sees a fraction
        # of what the filters ahead of it see, and charging it the source's rate
        # would bill it for work it never does.
        records = rec_in if (not math.isnan(rec_in) and rec_in > 0) else rec_out
        entry = profile.setdefault(vid, {"name": names.get(vid, vid), "subtasks": []})
        entry["subtasks"].append({
            "taskmanager": tm_of.get((vid, sub), "?"),
            "speed": speeds.get(tm_of.get((vid, sub), "?"), 1.0),
            "busy_ms_s": busy, "records_s": records, "bytes_out_s": bytes_out,
            "cpu_ms_per_record": (busy / records)
                if (records and records > 0 and not math.isnan(busy)) else float("nan"),
            "bytes_per_record": (bytes_out / records)
                if (records and records > 0 and not math.isnan(bytes_out)) else float("nan"),
        })

    print()
    print(f"{'vertex':38} {'par':>3} {'busy ms/s':>10} {'reg/s':>9} "
          f"{'ms CPU/1k reg':>14} {'bytes/reg':>10}  máquinas")
    print("-" * 116)
    summary = {}
    for vid, entry in profile.items():
        cpu = median([s["cpu_ms_per_record"] for s in entry["subtasks"]])
        bpr = median([s["bytes_per_record"] for s in entry["subtasks"]])
        busy = median([s["busy_ms_s"] for s in entry["subtasks"]])
        recs = median([s["records_s"] for s in entry["subtasks"]])
        tms = ",".join(sorted({s["taskmanager"] for s in entry["subtasks"]}))
        summary[vid] = {"name": entry["name"], "cpu_ms_per_record": cpu,
                        "bytes_per_record": bpr, "busy_ms_s": busy,
                        "records_s": recs, "subtasks": len(entry["subtasks"])}
        fmt = lambda v, w, p=2: (f"{v:{w}.{p}f}" if not math.isnan(v) else f"{'—':>{w}}")
        print(f"{entry['name'][:38]:38} {len(entry['subtasks']):>3} "
              f"{fmt(busy, 10, 0)} {fmt(recs, 9, 0)} {fmt(cpu * 1000, 14)} "
              f"{fmt(bpr, 10, 1)}  {tms[:26]}")

    # THE CHECK THAT THE 10000 rec/s RUN NEEDED. busyTimeMsPerSecond is reported in
    # whole milliseconds out of 1000, so below a few percent utilisation it
    # quantises to a handful of values and stops ranking anything: that run had two
    # subtasks of the SAME operator doing the SAME work report 30 and exactly 0.
    busies = [s["busy_ms_s"] for s in summary.values() if not math.isnan(s["busy_ms_s"])]
    if busies and max(busies) < 100:
        print()
        print(f"  !! RESOLUCIÓN INSUFICIENTE: el vértice más ocupado está en "
              f"{max(busies):.0f} ms/s de 1000.")
        print("     A esta carga la métrica cuantiza y los costos no ordenan nada.")
        print("     Sube --rate; el criterio es la tasa MÁS BAJA que resuelva.")

    missing = [s["name"] for s in summary.values()
               if math.isnan(s["cpu_ms_per_record"])]
    if missing:
        print()
        print("  ! sin costo de CPU (la métrica no se reporta para estos vértices):")
        for m in missing:
            print(f"      {m}")
        print("    Publicar 0 para ellos NO es neutral: el fork trata un slice de")
        print("    costo <= 0 como 1e-6, es decir gratis.")

    json.dump({"profile_rate": args.rate, "speeds": speeds,
               "vertices": summary, "detail": profile},
              open(os.path.join(args.out, "profile.json"), "w"), indent=2)
    print()
    print(f"perfil -> {os.path.join(args.out, 'profile.json')}")


if __name__ == "__main__":
    main()
