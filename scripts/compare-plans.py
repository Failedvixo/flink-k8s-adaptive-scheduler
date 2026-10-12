#!/usr/bin/env python3
"""Measure several fixed placements, in turns, on ONE running job — and what v4 would choose.

WHY (2026-10-12, part B, scenario C). Before a monitor can be shown to track a moving optimum,
there has to be one: the best placement must CHANGE with the rate. On Q8 it does not (v4 sends the
joins to medium at 20 000 and at 40 000 alike). On Q5 with CAPSys' 60 s / 1 s window the counts are
CPU-bound and the best pass of the campaign put them on fast, so the answer may depend on the rate.

The replan trigger makes this cheap: a placement change is one restart of about a second, not a
rescale, so several plans can be compared on the same job, at the same rate, in rotating order.
For each turn: apply the plan, settle, measure delivered/offered, sink latency and machine
pressure (replan_monitor.observe), and ask the v4 table which plan IT would pick from what it sees.

Plans are given as NAME=ASSIGNMENTS, assignments "operator-substring[:subtask]=taskmanager" joined
by commas; operators not mentioned keep whatever the PREVIOUS plan gave them. Example (Q5):

  scripts/compare-plans.py --rate 4500 --reps 2 \\
    --plan "counts-medium=count=tm-2-medium,max=tm-2-medium,bid-source=tm-3-fast,sink=tm-1-slow" \\
    --plan "counts-fast=count=tm-3-fast,max=tm-2-medium,bid-source=tm-2-medium,sink=tm-1-slow"
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import characterizer_agent as ca                               # noqa: E402
import replan_monitor as rm                                    # noqa: E402

REPLAN_FILE = "/var/thesis/replan"


def parse_plan(text):
    name, _, body = text.partition("=")
    rules = []
    for item in filter(None, (x.strip() for x in body.split(","))):
        target, _, tm = item.rpartition("=")
        op, _, sub = target.partition(":")
        rules.append((op, int(sub) if sub else None, tm))
    return name, rules


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", action="append", required=True)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--dist", default="CONSTANT")
    ap.add_argument("--settle", type=float, default=120.0)
    ap.add_argument("--window", type=float, default=180.0)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--qtable", default="results/characterizer/q8-v4-20261008.json")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--out", default="results/compare-plans")
    # observe() reads these from args
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--saturation-cut", type=float, default=900.0)
    ap.add_argument("--disk-suffix", default=".rocksdb_compact_write_bytes")
    ap.add_argument("--busy-cut", type=float, default=0.5)
    ap.add_argument("--disk-floor", type=float, default=1e6)
    args = ap.parse_args()

    ac._NAMESPACE = args.namespace
    import subprocess
    ac._JM_POD = subprocess.run(
        ["kubectl", "get", "pods", "-n", args.namespace, "-l", "component=jobmanager",
         "--field-selector=status.phase=Running", "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True, timeout=30).stdout.strip() or None
    plans = [parse_plan(p) for p in args.plan]
    agent = ca.SliceSarsa(0.0, 0.0, Path(args.qtable), 3, ucb=0.0)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"compare-{time.strftime('%Y%m%d-%H%M%S')}-r{int(args.rate)}.csv"
    _, err = ca.node_command(args.node, ["sh", "-c", "printf RL > /var/thesis/arm"])

    order = []
    for rep in range(args.reps):
        k = rep % len(plans)                         # rotate, so no plan always goes first
        order += [(rep, plans[(k + i) % len(plans)]) for i in range(len(plans))]
    print(f"{len(order)} turnos, ~{len(order) * (args.settle + args.window) / 60:.0f} min "
          f"(tasa {args.rate:.0f}, {args.dist})", flush=True)

    fields = ["rep", "plan", "offered", "rps", "ratio", "latency_p50_ms", "machines",
              "placement", "v4_would_choose", "v4_agrees"]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for rep, (name, rules) in order:
            jid = ac.running_job(args.rest)
            detail = ac.rest(args.rest, f"/jobs/{jid}") if jid else None
            layout = ca.read_layout(args.node)
            if not detail or not layout:
                sys.exit("ERROR: sin job RUNNING o sin /var/thesis/slices")
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            where = {}
            for v in detail.get("vertices", []):
                for sub, tm in ac.subtask_taskmanagers(args.rest, jid, v["id"]).items():
                    where[(v["id"], sub)] = tm
            # Rules apply to WHOLE SLICES (2026-10-12): Q5's bid-source and bid-watermarks are two
            # operators in one slice, and moving only the one the rule named split the slice — the
            # first run asked medium for 5 slots. A rule matches a slice if it names any of its
            # operators; ":N" picks the slice of subtask N.
            target = dict(where)
            for op, sub, tm in rules:
                hits = [m for m in layout["slices"]
                        if any(op in names[v] for v, _ in m) and (sub is None or m[0][1] == sub)]
                if not hits:
                    sys.exit(f"ERROR: '{op}' no coincide con ningún operador")
                for members in hits:
                    for k in members:
                        target[k] = tm
            free = dict(layout["tms"])
            # Slots are per slice (slot sharing), so count slices, not subtasks.
            per_tm = {}
            for members in layout["slices"]:
                tm = target.get(members[0])
                per_tm[tm] = per_tm.get(tm, 0) + 1
            over = {tm: n for tm, n in per_tm.items() if n > free.get(tm, 0)}
            if over:
                sys.exit(f"ERROR: el plan '{name}' no cabe en los slots: {over} (libres {free})")
            ok, error = ca.publish_plan({len(layout["slices"]): {f"{vid}:{s}": tm
                                                                for (vid, s), tm in target.items()}},
                                        args.node)
            token = str(int(time.time() * 1000))
            if ok and target != where:
                _, error = ca.node_command(args.node, ["sh", "-c",
                                           f"printf '%s now\\n' {token} > {REPLAN_FILE}"])
            if not ok or error:
                sys.exit(f"ERROR al aplicar '{name}': {error}")
            print(f"\n[rep {rep + 1}] {name}: "
                  f"{'replan pedido' if target != where else 'ya estaba aplicado'}; "
                  f"asentando {args.settle:.0f}s", flush=True)
            time.sleep(args.settle)

            jid = ac.running_job(args.rest)
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            obs = rm.observe(args, jid, detail, layout)
            if obs is None:
                print("  ! la ventana cruzó un reinicio — turno descartado", flush=True)
                continue
            latency = ca.sink_record_latency_ms(args.rest, jid, detail)
            wrong = [k for k in target if obs["where"].get(k) != target[k]]

            # What v4 would choose from what it sees under THIS placement.
            speeds = ca.read_speeds(args.node)
            ranked = sorted(layout["tms"], key=lambda t: (-speeds.get(t[0], 1.0), t[0]))
            plan, _ = ca.decide(agent, obs["per_slice"], [f for _t, f in ranked], 0.0,
                                args.busy_cut, disk=obs["per_slice_disk"], machine_load=True,
                                disk_floor=args.disk_floor)
            kinds = [", ".join(sorted({names.get(v, v) for v, _ in m})) for m in layout["slices"]]
            running = [obs["where"].get(m[0]) for m in layout["slices"]]
            v4 = rm.fewest_moves([ranked[plan[i]][0] for i in range(len(running))], running, kinds) \
                if plan else None
            describe = lambda tms: "; ".join(sorted({f"{kinds[i].split(',')[0][:18]}->"    # noqa
                                                     f"{tms[i].split('-')[-1]}"
                                                     for i in range(len(tms))}))
            agrees = v4 == running
            mach = {tm.split("-")[-1]: {"thr": None if m["throttled"] is None
                                        else round(m["throttled"], 2), "stall": round(m["stall"], 3)}
                    for tm, m in obs["machines"].items()}
            print(f"  entrega {obs['ratio']:.1%} ({obs['rps']:.0f} de {obs['offered']:.0f})  "
                  f"latencia p50 {latency} ms  máquinas {mach}"
                  f"{'  ! ' + str(len(wrong)) + ' subtareas fuera del plan' if wrong else ''}",
                  flush=True)
            print(f"  v4 {'se quedaría' if agrees else 'cambiaría a: ' + describe(v4)}", flush=True)
            writer.writerow({"rep": rep + 1, "plan": name, "offered": round(obs["offered"]),
                             "rps": round(obs["rps"]), "ratio": round(obs["ratio"], 4),
                             "latency_p50_ms": latency, "machines": json.dumps(mach),
                             "placement": describe(running),
                             "v4_would_choose": describe(v4) if v4 else "",
                             "v4_agrees": agrees})
            handle.flush()

    rows = list(csv.DictReader(csv_path.open()))
    print(f"\nRESUMEN a {args.rate:.0f} ev/s  ({csv_path})")
    for name, _rules in plans:
        rs = [r for r in rows if r["plan"] == name]
        if rs:
            ratio = sum(float(r["ratio"]) for r in rs) / len(rs)
            lats = [float(r["latency_p50_ms"]) for r in rs if r["latency_p50_ms"] not in ("", "None")]
            print(f"  {name:20s} entrega {ratio:6.1%}  latencia p50 "
                  f"{(sum(lats) / len(lats)) if lats else float('nan'):8.0f} ms  "
                  f"v4 de acuerdo {sum(r['v4_agrees'] == 'True' for r in rs)}/{len(rs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
