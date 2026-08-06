#!/usr/bin/env python3
"""
Find the scenario where the placement arms actually disagree.

A meta-scheduler can only be worth its complexity where the choice of arm changes
the outcome. This reads the per-arm campaign written by run-fork-campaign.sh and
ranks every (query, distribution) scenario by how much the arms spread apart,
which is the scenario to train the learners on — the same selection rule the
Kubernetes-side campaign used before, applied to the fork's arms.

Per cell it aggregates the episodes the controller recorded, keeping only the
ones it marked creditable: an epoch where the assigner had no choice (the pool
offered exactly as many slots as the job had slices) or where the job sat idle
carries no information about the arm, and averaging it in would flatten exactly
the differences this script is looking for.

Reported per scenario:
  spread_reward  = max - min of the arms' mean reward   (placement dispersion)
  spread_tput    = max - min of the arms' mean throughput per slot, relative
  cv_across_arms = dispersion of the arms' mean rewards, normalised

Usage:
  python3 scripts/analyse_fork_campaign.py [results/fork-campaign] [--metric reward]
"""
import argparse
import csv
import statistics
import sys
from pathlib import Path


def read_cell(cell_dir):
    """Aggregate one (scenario, arm) cell from its episode CSVs."""
    rows = []
    for csv_path in sorted(cell_dir.glob("episodes-*.csv")):
        with csv_path.open() as handle:
            rows.extend(list(csv.DictReader(handle)))
    if not rows:
        return None

    def numbers(field, only_creditable=True):
        values = []
        for row in rows:
            if only_creditable and row.get("creditable") not in ("1", ""):
                continue
            try:
                values.append(float(row[field]))
            except (KeyError, TypeError, ValueError):
                continue
        return values

    rewards = numbers("reward")
    if not rewards:
        return {"episodes": len(rows), "credited": 0}

    return {
        "episodes": len(rows),
        "credited": len(rewards),
        "reward": statistics.fmean(rewards),
        "cv_busy": statistics.fmean(numbers("cv_busy_all") or [0.0]),
        "throughput": statistics.fmean(numbers("throughput_per_slot") or [0.0]),
        "backpressure": statistics.fmean(numbers("backpressure_mean_ms_s") or [0.0]),
        # Which arms the learners actually settled on; constant for fixed-arm cells.
        "arms_applied": sorted({row.get("arm_applied", "") for row in rows}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default="results/fork-campaign")
    parser.add_argument("--metric", default="reward",
                        choices=["reward", "cv_busy", "throughput"],
                        help="which per-cell mean the scenarios are ranked by")
    parser.add_argument("--out", default=None, help="write the flat table here as CSV")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: no campaign at {root}", file=sys.stderr)
        return 1

    scenarios = {}
    for scenario_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        cells = {}
        for arm_dir in sorted(p for p in scenario_dir.iterdir() if p.is_dir()):
            cell = read_cell(arm_dir)
            if cell:
                cells[arm_dir.name] = cell
        if cells:
            scenarios[scenario_dir.name] = cells

    if not scenarios:
        print(f"No episode CSVs under {root} — has the campaign run?", file=sys.stderr)
        return 1

    flat = []
    ranking = []
    for scenario, cells in scenarios.items():
        print(f"\n=== {scenario} " + "=" * (46 - len(scenario)))
        print(f"  {'arm':<14}{'episodes':>9}{'credited':>9}{'reward':>9}"
              f"{'cv_busy':>9}{'tput/slot':>11}{'bp ms/s':>9}")
        usable = {}
        for arm, cell in sorted(cells.items()):
            if cell.get("credited"):
                print(f"  {arm:<14}{cell['episodes']:>9}{cell['credited']:>9}"
                      f"{cell['reward']:>9.3f}{cell['cv_busy']:>9.3f}"
                      f"{cell['throughput']:>11.0f}{cell['backpressure']:>9.0f}")
                usable[arm] = cell
            else:
                print(f"  {arm:<14}{cell['episodes']:>9}{cell['credited']:>9}"
                      f"{'—':>9}{'—':>9}{'—':>11}{'—':>9}   (no creditable epoch)")
            flat.append({"scenario": scenario, "arm": arm, **{
                k: v for k, v in cell.items() if k != "arms_applied"}})

        if len(usable) < 2:
            print("  (fewer than two comparable arms — cannot rank this scenario)")
            continue

        values = {arm: cell[args.metric] for arm, cell in usable.items()}
        best = max(values, key=values.get)
        worst = min(values, key=values.get)
        spread = values[best] - values[worst]
        mean = statistics.fmean(values.values())
        relative = spread / mean if mean else 0.0
        ranking.append((relative, spread, scenario, best, worst, len(usable)))
        print(f"  spread({args.metric}) = {spread:.4f}"
              f"  ({relative * 100:.1f}% of mean)   best={best}  worst={worst}")

    print("\n" + "=" * 60)
    print(f"  Scenarios ranked by how much the arms disagree ({args.metric})")
    print("=" * 60)
    for relative, spread, scenario, best, worst, arms in sorted(ranking, reverse=True):
        print(f"  {relative * 100:6.1f}%  {scenario:<18} "
              f"(abs {spread:.4f}, {arms} arms, best={best}, worst={worst})")
    if ranking:
        top = sorted(ranking, reverse=True)[0]
        print(f"\n  -> train the meta-schedulers on {top[2]}: the arms differ most there,")
        print("     so a policy that picks between them has something to gain.")
        flat_top = [r for r in ranking if r[0] < 0.02]
        if flat_top:
            print("  -> these scenarios are effectively flat, and a meta-scheduler cannot")
            print("     beat a fixed arm on them by more than noise: "
                  + ", ".join(r[2] for r in flat_top))

    if args.out:
        fields = ["scenario", "arm", "episodes", "credited", "reward", "cv_busy",
                  "throughput", "backpressure"]
        with Path(args.out).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat)
        print(f"\n  table -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
