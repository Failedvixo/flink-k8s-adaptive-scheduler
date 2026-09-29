#!/usr/bin/env python3
"""Does running a job earlier or later in a session change its result?

THE DIRECT TEST. Deterministic arms, several cells in a row, nothing else varying: any
difference between the cells IS the position. Two forms:

    ARMS="LPT LPT LPT LPT LPT LPT"              does the environment decay in a session?
    ARMS="LPT ROUND_ROBIN LPT ROUND_ROBIN ..."  and does it hit one placement harder?

The second is the one that matters for a campaign: a decay that costs every arm the same
leaves the ratios between them intact. Point this at the run directory, or at nothing for
the newest one.

WHY IT MATTERS. Position, not placement, explained a result twice in this project: the
capacity experiment of 2026-09-03 measured both arms worse in the second slot of every
repetition (LPT 0.988 -> 0.861). The harness answered with a TaskManager restart and a
checkpoint wipe before every arm, and the campaigns of 2026-09-21/22 suggested the effect
was gone — LPT varied 0.9% and 3.9% across positions. But that was LPT inside campaigns
built for something else, with two jobs per position. This measures it on purpose.

Reports the slope in % per position and an EXACT permutation p-value over all orderings:
with six cells that is 720 permutations, so the test is exhaustive rather than approximate.

Usage:
    scripts/order-effect.py
    scripts/order-effect.py results/placement-experiment/<run>
"""
import csv
import glob
import itertools
import os
import statistics
import sys


def slope(values):
    """Least-squares slope against position 1..n, as a fraction of the mean."""
    n = len(values)
    xs = list(range(1, n + 1))
    mx, my = statistics.mean(xs), statistics.mean(values)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    return (num / den) / my if den and my else 0.0


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else max(
        glob.glob("results/placement-experiment/*/"), key=os.path.getmtime)
    cells = []
    for d in sorted(p for p in glob.glob(os.path.join(run, "*/")) if os.path.isdir(p)):
        vals = []
        for f in glob.glob(os.path.join(d, "episodes-*.csv")):
            for r in csv.DictReader(open(f)):
                if r["slices"] == "8" and r["creditable"] == "1":
                    vals.append(float(r["source_out_rps"]))
        if vals:
            cells.append((os.path.basename(d.rstrip("/")), sum(vals) / len(vals), len(vals)))
    if len(cells) < 3:
        print(f"ERROR: {run} tiene {len(cells)} celdas con episodios acreditables; "
              f"hacen falta al menos 3", file=sys.stderr)
        return 2

    # The driver writes the cells in the order it ran them, but the glob sorts by name, and
    # "LPT" sorts before "LPT#2". Directory mtime is the order that actually happened.
    cells.sort(key=lambda c: os.path.getmtime(os.path.join(run, c[0])))

    # NORMALISE BY ARM when the run mixes several (2026-09-22). One arm answers "does the
    # environment decay within a session"; what actually invalidates a campaign is narrower —
    # position hurting one arm MORE than another. Testing that needs a second deterministic
    # arm, and then the position slope has to be computed on each cell relative to its own
    # arm's mean, or the difference between the arms would swamp the trend. Both arms must be
    # deterministic: STOCK draws a new placement per job, so its spread would be read as
    # position.
    by_arm = {}
    for name, v, _ in cells:
        by_arm.setdefault(name.split("#")[0], []).append(v)
    arms = sorted(by_arm)
    print(f"{os.path.basename(run.rstrip('/'))}  —  {len(cells)} celdas en orden de ejecución"
          f"{'' if len(arms) == 1 else '  (' + ', '.join(arms) + ')'}")
    print()
    raw = [c[1] for c in cells]
    mean = statistics.mean(raw)
    arm_mean = {a: statistics.mean(v) for a, v in by_arm.items()}
    # Relative to its own arm when there is more than one; absolute otherwise.
    values = ([v / arm_mean[name.split("#")[0]] for name, v, _ in cells]
              if len(arms) > 1 else raw)
    for i, (name, v, n) in enumerate(cells, 1):
        base = arm_mean[name.split("#")[0]]
        bar = "█" * max(1, round(40 * v / max(raw)))
        print(f"  {i}. {name:14} {v:7.0f}  {v / base - 1:+6.1%}  {bar}")
    print()
    sd = statistics.stdev(raw)
    for a in arms:
        v = by_arm[a]
        print(f"  {a:14} media {statistics.mean(v):7.0f}   n={len(v)}")
    rel = [v / arm_mean[name.split("#")[0]] for name, v, _ in cells]
    print(f"  dispersión dentro del brazo: {statistics.stdev(rel):.1%}")

    observed = slope(values)
    perms = list(itertools.permutations(values))
    extreme = sum(1 for p in perms if abs(slope(list(p))) >= abs(observed) - 1e-12)
    p = extreme / len(perms)
    print(f"  pendiente {observed:+.2%} por posición   p={p:.4f}  "
          f"(permutación exacta, {len(perms)} órdenes)")
    print()
    if p < 0.05:
        direction = "cae" if observed < 0 else "sube"
        print(f"  HAY efecto de orden: el rendimiento {direction} {abs(observed):.1%} por")
        print("  posición. Las campañas necesitan el orden rotado, y comparar brazos")
        print("  medidos en posiciones distintas está sesgado.")
    else:
        detectable = 2 * statistics.stdev(rel) / len(values) ** 0.5
        print("  NO se detecta efecto de orden. Con esta dispersión, un efecto mayor que")
        print(f"  ~{detectable:.1%} por posición se habría visto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
