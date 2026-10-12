#!/usr/bin/env python3
"""Part B, learned monitor: a contextual bandit that picks the placement to run, by MEASURING.

WHY (2026-10-12). Comparing fixed placements of Q5 (60 s / 1 s windows) at 3000, 4500 and 6000
ev/s showed that the v4 table cannot drive a monitor: with the counts on medium (saturated, read as
HIGH) it proposes fast; with them on fast (relaxed, read as LOW) it proposes medium; at 6000 it
proposes the worst plan from any starting point (68.7% delivered against 82.2% for the best). What a
slice measures depends on where it runs, so a table keyed on those measurements contradicts itself.

But a placement change now costs about one second (ThesisReplanTrigger), so the monitor does not
have to TRUST any estimate: it can try a placement, measure it, and keep it or go back. This is that,
as a bandit:

  context   the offered-rate level (low / mid / high thirds of the DIST range) — what changes
            during the run and changes which placement is best;
  arms      placements, as a signature (which machines each kind of slice is on), discovered
            online from a candidate generator: STAY; v4's proposal (the SARSA planner, kept as a
            source of ideas, not of decisions); every single-slice move of a heavy slice to
            another machine, or its swap with a light slice there; and the best placement already
            measured in this context;
  reward    delivered/offered minus LATENCY_WEIGHT * ln(p50 latency / 1 s), measured with the
            placement in force;
  policy    UCB per context; a placement never measured in this context starts at the value
            of the one running now plus an optimism bonus, so it gets tried; a move pays a small
            fixed cost, so ties stay put.

Learning persists in --model (JSON: per context, per signature, mean reward and count), so runs
accumulate. --freeze evaluates: greedy, no exploration bonus, no updates.
"""
import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import characterizer_agent as ca                               # noqa: E402
import replan_monitor as rm                                    # noqa: E402

REPLAN_FILE = "/var/thesis/replan"


def signature(kinds, placement):
    """Placement as {kind: sorted machines}: slices of one kind are interchangeable."""
    sig = {}
    for kind, tm in zip(kinds, placement):
        sig.setdefault(kind, []).append(tm)
    return json.dumps({k: sorted(v) for k, v in sorted(sig.items())}, sort_keys=True)


def from_signature(sig, kinds, running):
    """A per-slice placement realising `sig` with as few moves from `running` as possible."""
    wanted = json.loads(sig)
    proposed = list(running)
    for kind, machines in wanted.items():
        idx = [i for i, k in enumerate(kinds) if k == kind]
        if len(idx) != len(machines):
            return None
        for i, tm in zip(idx, machines):
            proposed[i] = tm
    return rm.fewest_moves(proposed, running, kinds)


def fits(placement, capacity):
    used = {}
    for tm in placement:
        used[tm] = used.get(tm, 0) + 1
    return all(used[tm] <= capacity.get(tm, 0) for tm in used)


def candidates(obs, layout, kinds, running, capacity, v4_plan, args):
    """{signature: (per-slice placement, how it was proposed)}, the running one included."""
    out = {signature(kinds, running): (running, "quedarse")}

    def add(placement, why):
        if placement and fits(placement, capacity):
            out.setdefault(signature(kinds, placement), (placement, why))

    if v4_plan:
        add(rm.fewest_moves(v4_plan, running, kinds), "propuesta de v4")
    costs = [c for c, _sat in obs["per_slice"]]
    peak = max(costs) if costs and max(costs) > 0 else 1.0
    heavy = [i for i, (c, sat) in enumerate(obs["per_slice"]) if sat or c / peak >= args.busy_cut]
    light = [i for i in range(len(running)) if i not in heavy]
    used = {}
    for tm in running:
        used[tm] = used.get(tm, 0) + 1
    for i in heavy:
        for tm in capacity:
            if tm == running[i]:
                continue
            moved = list(running)
            moved[i] = tm
            if used.get(tm, 0) < capacity[tm]:
                add(moved, f"mover {kinds[i][:20]} a {tm.split('-')[-1]}")
            for j in light:                       # or swap with a light slice living there
                if running[j] == tm:
                    swapped = list(moved)
                    swapped[j] = running[i]
                    add(swapped, f"intercambiar {kinds[i][:20]} con {kinds[j][:14]} en "
                                 f"{tm.split('-')[-1]}")
                    break
    return out


def score(obs, latency_ms, weight):
    lat = max(1.0, float(latency_ms)) if latency_ms not in (None, -1) else None
    return obs["ratio"] - (weight * math.log(lat / 1000.0) if lat else 0.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="JSON with what has been learned")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--qtable", default="results/characterizer/q8-v4-20261008.json")
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--dist", default="CONSTANT")
    ap.add_argument("--levels", default="",
                    help="offered-rate cut points between contexts, e.g. '3500,5000'; default: "
                         "thirds of the DIST range")
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--warmup", type=float, default=120.0, help="settle after a restart")
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--ucb", type=float, default=0.05, help="exploration bonus scale")
    ap.add_argument("--optimism", type=float, default=0.05,
                    help="bonus of a placement never measured in this context")
    ap.add_argument("--move-cost", type=float, default=0.01)
    ap.add_argument("--latency-weight", type=float, default=0.1)
    ap.add_argument("--out-dir", default="results/plan-bandit")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--jm-pod", default="")
    # observe() / v4 settings
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--saturation-cut", type=float, default=900.0)
    ap.add_argument("--disk-suffix", default=".rocksdb_compact_write_bytes")
    ap.add_argument("--busy-cut", type=float, default=0.5)
    ap.add_argument("--disk-floor", type=float, default=1e6)
    args = ap.parse_args()

    ac._JM_POD = args.jm_pod or None
    ac._NAMESPACE = args.namespace
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_path = Path(args.model)
    model = json.loads(model_path.read_text()) if model_path.exists() else {}
    stats = model.setdefault("contexts", {})
    agent = ca.SliceSarsa(0.0, 0.0, Path(args.qtable), 3, ucb=0.0)

    if args.levels:
        cuts = [float(x) for x in args.levels.split(",")]
    elif args.dist.upper().startswith(("SINE", "STEPS")):
        _kind, _period, rng = args.dist.split(":")
        values = [float(x) for x in rng.split(",")]
        lo, hi = min(values), max(values)
        cuts = [lo + (hi - lo) / 3, lo + 2 * (hi - lo) / 3]
    else:
        cuts = []
    level_names = ["baja", "media", "alta"][:len(cuts) + 1] if cuts else ["única"]

    def context(offered):
        return level_names[sum(offered >= c for c in cuts)]

    print("=" * 66)
    print(f"  Bandit de emplazamiento — {'EVALUACIÓN (congelado)' if args.freeze else 'ENTRENAMIENTO'}")
    print(f"  modelo {model_path}; contextos por tasa ofrecida: "
          f"{', '.join(f'{n}' for n in level_names)} (cortes {', '.join(f'{c:.0f}' for c in cuts) or '-'})")
    print("=" * 66, flush=True)

    fields = ["time", "elapsed_s", "context", "offered", "ratio", "latency_ms", "reward",
              "placement", "decision", "why", "candidates"]
    csv_path = out / f"bandit-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    started = time.time()
    last_key, settle_until, moves = None, 0.0, 0
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while time.time() - started < args.duration:
            jid = ac.running_job(args.rest)
            key = ac.epoch_key(args.rest, jid) if jid else None
            if key is None:
                time.sleep(5)
                continue
            if key != last_key:
                last_key = key
                settle_until = max(settle_until, ac.epoch_started_at(key, time.time()) + args.warmup)
            if time.time() < settle_until:
                time.sleep(5)
                continue
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            layout = ca.read_layout(args.node)
            if not detail or not layout:
                time.sleep(5)
                continue
            obs = rm.observe(args, jid, detail, layout)
            if obs is None:
                continue
            latency = ca.sink_record_latency_ms(args.rest, jid, detail)
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            kinds = [", ".join(sorted({names.get(v, v) for v, _ in m})) for m in layout["slices"]]
            running = [obs["where"].get(m[0]) for m in layout["slices"]]
            if None in running:
                continue
            capacity = dict(layout["tms"])
            ctx = context(obs["offered"])
            here = signature(kinds, running)
            reward = score(obs, latency, args.latency_weight)

            # Learn: the placement in force earned `reward` in this context.
            cell = stats.setdefault(ctx, {})
            entry = cell.setdefault(here, {"n": 0, "mean": 0.0})
            if not args.freeze:
                entry["n"] += 1
                entry["mean"] += (reward - entry["mean"]) / entry["n"]

            # Candidates, then UCB.
            speeds = ca.read_speeds(args.node)
            ranked = sorted(layout["tms"], key=lambda t: (-speeds.get(t[0], 1.0), t[0]))
            plan, _ = ca.decide(agent, obs["per_slice"], [f for _t, f in ranked], 0.0,
                                args.busy_cut, disk=obs["per_slice_disk"], machine_load=True,
                                disk_floor=args.disk_floor)
            v4 = [ranked[plan[i]][0] for i in range(len(running))] if plan else None
            cands = candidates(obs, layout, kinds, running, capacity, v4, args)
            best_known = max(((s, e) for s, e in cell.items() if e["n"] > 0),
                             key=lambda se: se[1]["mean"], default=(None, None))[0]
            if best_known and best_known not in cands:
                placement = from_signature(best_known, kinds, running)
                if placement and fits(placement, capacity):
                    cands[best_known] = (placement, "el mejor medido en este contexto")
            total = sum(e["n"] for e in cell.values()) or 1
            here_value = entry["mean"] if entry["n"] else reward

            def value(sig):
                e = cell.get(sig)
                if sig == here and not (e and e["n"]):
                    return here_value
                if e and e["n"]:
                    v = e["mean"]
                    if not args.freeze:
                        v += args.ucb * math.sqrt(math.log(total + 1) / e["n"])
                else:
                    v = here_value + (0.0 if args.freeze else args.optimism)
                    if args.freeze:
                        v = -math.inf          # evaluation: only placements already measured
                return v - (0.0 if sig == here else args.move_cost)

            choice = max(cands, key=value)
            placement, why = cands[choice]
            decision = "quedarse" if choice == here else "mover"
            if decision == "mover":
                plans = {len(running): {f"{vid}:{sub}": placement[i]
                                        for i, members in enumerate(layout["slices"])
                                        for vid, sub in members}}
                ok, error = ca.publish_plan(plans, args.node)
                token = str(int(time.time() * 1000))
                if ok:
                    _, error = ca.node_command(args.node, ["sh", "-c",
                                               f"printf '%s now\\n' {token} > {REPLAN_FILE}"])
                if not ok or error:
                    decision, why = "error", str(error)
                else:
                    moves += 1
                    settle_until = time.time() + args.warmup

            describe = lambda pl: " ".join(f"{kinds[i].split(',')[0].split(':')[-1].strip()[:9]}"  # noqa
                                            f"@{pl[i].split('-')[-1][:1]}" for i in range(len(pl)))
            known = {s: e for s, e in cell.items() if e["n"]}
            print(f"  [{time.strftime('%H:%M:%S')}] ctx {ctx:5s} pedido {obs['offered']:6.0f} "
                  f"entrega {obs['ratio']:5.1%} lat {latency} ms  recompensa {reward:+.3f} "
                  f"(n={entry['n']})  | {len(cands)} candidatos, {len(known)} medidos aquí  -> "
                  f"{decision.upper()}" + (f": {why}" if decision != "quedarse" else ""), flush=True)
            writer.writerow({"time": time.strftime("%H:%M:%S"),
                             "elapsed_s": round(time.time() - started), "context": ctx,
                             "offered": round(obs["offered"]), "ratio": round(obs["ratio"], 4),
                             "latency_ms": latency, "reward": round(reward, 4),
                             "placement": describe(running), "decision": decision, "why": why,
                             "candidates": len(cands)})
            handle.flush()
            if not args.freeze:
                model["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                model_path.parent.mkdir(parents=True, exist_ok=True)
                model_path.write_text(json.dumps(model, indent=1, sort_keys=True))

    print(f"\nfin: {moves} cambios de emplazamiento; {csv_path}", flush=True)
    for ctx, cell in stats.items():
        ranked_sigs = sorted(((e["mean"], e["n"], s) for s, e in cell.items() if e["n"]),
                             reverse=True)[:3]
        print(f"  contexto {ctx}:")
        for mean, n, s in ranked_sigs:
            short = {k.split(",")[0].split(":")[-1].strip()[:12]: [m.split("-")[-1] for m in v]
                     for k, v in json.loads(s).items()}
            print(f"    {mean:+.3f} (n={n})  {short}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
