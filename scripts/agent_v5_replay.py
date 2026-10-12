#!/usr/bin/env python3
"""Rebuild a v5 model offline from the epochs agent_v5_loop.py logged on the cluster.

WHY (2026-10-12). Two trainings on the cluster (60 epochs, ~15 h) were spoilt by features that
could grow without bound — see agent_v5._capped — not by bad data: every epoch logged what ran
(placement-epochNNN: each slice's machine and measured demand), what each machine went through
(characterizer.log: cpu, contention, disk, stall) and the reward. LinUCB's regression is a sum
over samples, so it can be refitted from those logs with the CURRENT features and machine-model
rules, exactly as if the loop had run with them. The data were collected by exploring policies,
which is fine for a regression: it models reward given features, whatever chose the plan.

Prints, per epoch, where the joins ran and where the refitted greedy policy would put them, then
saves a model agent_v5_loop.py can load (--model) to evaluate (--freeze) or keep training.

Usage: scripts/agent_v5_replay.py --out MODEL.json CELL_DIR [CELL_DIR ...]
       (CELL_DIR = results/placement-experiment/<stamp>/RL_V5)
"""
import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_v5 as v5                                          # noqa: E402

EPOCH = re.compile(r"\[epoca (\d+)\] paralelismo \d+.*?\n  slices=\d+ rps=\S+ pedido=(\S+) \(\d+%\) "
                   r"lat=\S+ reward=([-+\d.]+)\n((?:    tm-.*\n)+)")


def epochs(cell):
    """[(epoch, offered, reward, {tm: telemetry}, slices, {slice: tm})] in the order they ran."""
    log = (cell / "characterizer.log").read_text()
    out = []
    for m in EPOCH.finditer(log):
        ep, offered, reward = int(m.group(1)), float(m.group(2)), float(m.group(3))
        telemetry = {}
        for line in m.group(4).splitlines():
            telemetry[line.split()[0]] = ast.literal_eval(line[line.index("{"):line.index("}") + 1])
        placement = cell / f"v5-placement-epoch{ep:03d}.txt"
        if not placement.exists():
            continue
        slices, ran = [], {}
        for i, line in enumerate(placement.read_text().splitlines()):
            cpu = float(re.search(r"cpu=([\d.]+)", line).group(1))
            disk = float(re.search(r"disco=([\d.]+)", line).group(1))
            slices.append({"cpu": cpu, "disk": disk, "saturated": "SAT" in line,
                           "name": line.split("MB/s", 1)[-1].split(" | ")[0].strip()})
            ran[i] = re.search(r"ran-on (\S+)", line).group(1)
        out.append((ep, offered, reward, telemetry, slices, ran))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cells", nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--local-weight", type=float, default=0.5)
    ap.add_argument("--contention-cut", type=float, default=1.2)
    ap.add_argument("--stall-cut", type=float, default=0.05)
    ap.add_argument("--throttle-cut", type=float, default=0.10)
    ap.add_argument("--contention-fallback", action="store_true",
                    help="for epochs without throttling data, judge CPU contention by busy/CPU")
    args = ap.parse_args()

    data = [e for cell in args.cells for e in epochs(cell)]
    if not data:
        print("ERROR: no se encontraron épocas", file=sys.stderr)
        return 1
    tms = sorted(data[0][3])
    # Slots per TaskManager as the fork's slices file gave them during the campaigns.
    slots = {tm: {"slow": 6, "medium": 4, "fast": 2}[tm.split("-")[-1]] for tm in tms}
    short = {tm: tm.split("-")[-1] for tm in tms}

    agent, machines = v5.LinUCBPlacer(alpha=0.0), v5.MachineModel()
    operators = v5.OperatorProfile()
    for k, (ep, offered, reward, telemetry, slices, ran) in enumerate(data):
        for s in slices:
            operators.observe(s["name"], s["cpu"], s["disk"], offered)
        measured = slices
        slices = [dict(s, **operators.demand(s["name"], offered, s)) for s in slices]
        local = {}
        for tm in tms:
            s = telemetry[tm]
            stalled = s["stall"] > args.stall_cut
            if "throttled" in s:             # logged since 2026-10-12: the kernel's CPU signal
                contended = s["throttled"] > args.throttle_cut
            elif args.contention_fallback:
                contended = s["contention"] > args.contention_cut and not stalled
            else:
                # Before throttling was logged, busy/CPU is the only CPU signal, and it misreads
                # I/O waits as contention (medium learned as 1.2 cores). Off by default: an
                # unknown capacity stays optimistic until the kernel reports throttling.
                contended = False
            machines.observe(tm, s["cpu"], contended, s["disk"], stalled)
            local[tm] = -min(1.0, s["stall"] + max(0.0, s["contention"] - args.contention_cut))
        seen = [machines.view(tm, slots[tm]) for tm in tms]
        idx = {tm: i for i, tm in enumerate(tms)}
        for x, m in v5.plan_features(slices, seen, {i: idx[tm] for i, tm in ran.items()}):
            agent.update(x, (1 - args.local_weight) * reward + args.local_weight * local[tms[m]])
        plan, _ = v5.decide(agent, slices, seen, explore=False)
        joins = [i for i, s in enumerate(slices) if "join" in s["name"]]
        print(f"{k + 1:3d} epoca {ep:3d} pedido {offered:6.0f} reward {reward:+.2f}  joins en "
              f"{[short[ran[i]] for i in joins]}  -> greedy ahora: "
              f"{[short[tms[plan[i]]] for i in joins]}   join medido "
              f"{measured[joins[0]]['disk']:.1f} MB/s, perfil {slices[joins[0]]['disk']:.1f}")

    print("\npesos:", {f: round(w, 3) for f, w in zip(v5.FEATURES, agent.theta())})
    for tm in tms:
        print(f"  {tm:12} {machines.view(tm, slots[tm])}")
    agent.alpha = 0.5
    seen = [machines.view(tm, slots[tm]) for tm in tms]
    explored = Counter()
    for _ep, offered, _r, _t, slices, _ran in data:
        slices = [dict(s, **operators.demand(s["name"], offered, s)) for s in slices]
        plan, _ = v5.decide(agent, slices, seen, explore=True)
        explored[tuple(sorted(short[tms[plan[i]]] for i, s in enumerate(slices)
                              if "join" in s["name"]))] += 1
    print("con exploración (alpha 0.5), joins ->", dict(explored))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "features": v5.FEATURES,
        "regression": {"A": agent.A.tolist(), "b": agent.b.tolist(), "n": agent.n},
        "weights": dict(zip(v5.FEATURES, agent.theta().round(4).tolist())),
        "machines": machines.to_json(),
        "operators": operators.to_json(),
        "provenance": "agent_v5_replay.py over " + ", ".join(str(c) for c in args.cells),
    }, indent=2))
    print(f"modelo -> {args.out}  (n = {agent.n})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
