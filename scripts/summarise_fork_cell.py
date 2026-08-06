#!/usr/bin/env python3
"""
Reduce one campaign cell to the few numbers training and comparison need, then
throw the bulk away.

A full grid is hundreds of cells, and what filled the disk in the earlier
campaign was raw logs nobody reads twice. What genuinely cannot be recomputed
later is kept:

  summary.json        the cell's configuration and its aggregates
  episodes-*.csv      one row per rescale — the training data itself
  thesis-assign.log   the assigner's own lines, i.e. ground truth for which arm
                      was applied and how the slices were spread

Everything else (autoscaler logs, the full job graph) is read once here, reduced
into summary.json, and deleted. `--keep all` skips the deletion for debugging.

Usage:
  scripts/summarise_fork_cell.py results/fork-campaign/q5-sine/ACO --arm ACO ...
"""
import argparse
import csv
import json
import math
import re
import statistics
import sys
from pathlib import Path

RX_ASSIGN = re.compile(
    r"\[THESIS_ASSIGN\] strategy=(?P<arm>[A-Z_]+)(?:\((?P<delegate>\w+)\))?"
    r" slices=(?P<slices>\d+) freeSlots=(?P<free>\d+)"
)

# Read for their content, then removed: everything here is either derivable from
# summary.json or too coarse to re-analyse. The controller's own log stays — it
# is a few KiB and it is the only place that says WHY an epoch went uncredited,
# which is the first question asked of any cell that looks empty.
PRUNABLE = ["job-details.json", "autoscaler.log", "autoscaler-stdout.log",
            "scale-events.log", "job-id.txt"]


def summarise_job(cell_dir):
    """Final shape of the job graph: state and the parallelism each vertex ended at."""
    path = cell_dir / "job-details.json"
    if not path.is_file():
        return {}
    try:
        details = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return {
        "state": details.get("state"),
        "duration_ms": details.get("duration"),
        "vertices": [
            {"name": v.get("name", "")[:48], "parallelism": v.get("parallelism")}
            for v in details.get("vertices", [])
        ],
    }


def summarise_assignments(cell_dir):
    """
    What the assigner did, from its own log.

    `decisions` counts only the rounds where the pool offered more slots than the
    job had slices — the others produce the same placement under every arm, so
    they say nothing about which arm is better.
    """
    path = cell_dir / "thesis-assign.log"
    if not path.is_file():
        return {}
    arms = {}
    rounds = decisions = 0
    for line in path.read_text(errors="replace").splitlines():
        match = RX_ASSIGN.search(line)
        if not match:
            continue
        rounds += 1
        arms[match["arm"]] = arms.get(match["arm"], 0) + 1
        if int(match["free"]) > int(match["slices"]):
            decisions += 1
    return {"rounds": rounds, "decisions": decisions, "arms_applied": arms}


def summarise_episodes(cell_dir):
    """Aggregate the controller's per-rescale rows, keeping only creditable ones."""
    rows = []
    for path in sorted(cell_dir.glob("episodes-*.csv")):
        with path.open() as handle:
            rows.extend(list(csv.DictReader(handle)))
    if not rows:
        return {"episodes": 0, "credited": 0}

    def values(field):
        out = []
        for row in rows:
            if row.get("creditable") != "1":
                continue
            try:
                value = float(row[field])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                out.append(value)
        return out

    def stdev(sample):
        """By hand: `statistics` raises on a non-finite sample, and a summary that
        crashes takes the cell's numbers with it."""
        if len(sample) < 2:
            return 0.0
        mean = sum(sample) / len(sample)
        return math.sqrt(sum((v - mean) ** 2 for v in sample) / len(sample))

    rewards = values("reward")
    summary = {"episodes": len(rows), "credited": len(rewards)}
    if rewards:
        summary.update({
            "reward_mean": round(statistics.fmean(rewards), 4),
            "reward_stdev": round(stdev(rewards), 4),
            "cv_busy_mean": round(statistics.fmean(values("cv_busy_all") or [0]), 4),
            "busy_mean_ms_s": round(statistics.fmean(values("busy_mean_ms_s") or [0]), 1),
            "backpressure_mean_ms_s": round(
                statistics.fmean(values("backpressure_mean_ms_s") or [0]), 1),
            "throughput_per_slot": round(
                statistics.fmean(values("throughput_per_slot") or [0]), 1),
        })
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cell_dir")
    parser.add_argument("--arm", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--dist", default="")
    parser.add_argument("--rate", default="")
    parser.add_argument("--duration", default="")
    parser.add_argument("--tms", default="")
    parser.add_argument("--warmup", default="")
    parser.add_argument("--window", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--keep", choices=["slim", "all"], default="slim")
    args = parser.parse_args()

    cell_dir = Path(args.cell_dir)
    if not cell_dir.is_dir():
        print(f"ERROR: no such cell {cell_dir}", file=sys.stderr)
        return 1

    summary = {
        "arm": args.arm,
        "query": args.query,
        "dist": args.dist,
        "rate": args.rate,
        "duration_s": args.duration,
        "tm_replicas": args.tms,
        "warmup_s": args.warmup,
        "window_s": args.window,
        "job_id": args.job_id,
        "job": summarise_job(cell_dir),
        "assignments": summarise_assignments(cell_dir),
        "episodes": summarise_episodes(cell_dir),
    }
    (cell_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    freed = 0
    if args.keep == "slim":
        for name in PRUNABLE:
            path = cell_dir / name
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()

    episodes = summary["episodes"]
    assignments = summary["assignments"]
    print(f"  summary: {episodes.get('credited', 0)}/{episodes.get('episodes', 0)} "
          f"episodes credited, {assignments.get('decisions', 0)}/"
          f"{assignments.get('rounds', 0)} assignment rounds had a choice"
          + (f", freed {freed // 1024} KiB" if freed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
