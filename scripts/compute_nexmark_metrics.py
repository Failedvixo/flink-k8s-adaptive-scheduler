#!/usr/bin/env python3
"""
Compute Nexmark canonical metrics (Cores, Time, Cost / Mevent) by
post-processing existing experiment runs.

Reads:
  results/.../autoscaler.log   →  per-snapshot per-node CPU in millicores
  results/.../metrics.csv      →  duration_sec, processed_events, throughput

Appends to METRICS-SUMMARY.txt a block:

  ========================================
    Nexmark canonical metrics
  ========================================
    Cores avg:         X.XX cores
    Cores p95:         X.XX cores
    Cores peak:        X.XX cores
    Time:              XXX.X s
    Cores × Time:      XXX.X core·s
    Events processed:  N,NNN,NNN
    Cost / Mevent:     XX.X core·s / Mevents     ← lower is better
    Throughput / core: N,NNN ev/s                ← higher is better
  ========================================

If the block already exists in the summary it is replaced (idempotent).

Usage:
  python3 scripts/compute_nexmark_metrics.py results/q5-const/BANDIT
  python3 scripts/compute_nexmark_metrics.py results/q5-const            # all subdirs
  python3 scripts/compute_nexmark_metrics.py results/q5-*                # via glob
"""
import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

# Matches lines like:
#   [18:39:02]   minikube       3044m        25%      1731Mi          21%
RX_NODE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+(\S+)\s+(\d+)m\s+(\d+)%\s+\S+\s+(\d+)%"
)


def parse_log(log_path: Path):
    """Return list of (ts_str, total_cores_used) per snapshot, sorted by time."""
    by_ts = {}
    with log_path.open(errors="replace") as fh:
        for line in fh:
            m = RX_NODE.match(line)
            if not m:
                continue
            h, mn, s, node, milli, _cpu_pct, _mem_pct = m.groups()
            ts = f"{h}:{mn}:{s}"
            by_ts.setdefault(ts, {})[node] = int(milli)
    snapshots = []
    for ts in sorted(by_ts):
        nodes = by_ts[ts]
        if nodes:
            total_cores = sum(nodes.values()) / 1000.0
            snapshots.append((ts, total_cores))
    return snapshots


def parse_metrics_csv(csv_path: Path):
    with csv_path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            return {
                "processed_events": int(row.get("processed_events") or 0),
                "duration_sec": float(row.get("duration_sec") or 0),
                "throughput_processed": int(row.get("throughput_processed") or 0),
            }
    return None


def compute_metrics(run_dir: Path):
    log_path = run_dir / "autoscaler.log"
    csv_path = run_dir / "metrics.csv"
    if not log_path.exists() or not csv_path.exists():
        return None
    snapshots = parse_log(log_path)
    if not snapshots:
        return None
    cores_series = [c for _, c in snapshots]

    m = parse_metrics_csv(csv_path)
    if m is None or m["processed_events"] <= 0:
        return None

    cores_avg = statistics.fmean(cores_series)
    cores_peak = max(cores_series)
    sorted_c = sorted(cores_series)
    p95_idx = min(len(sorted_c) - 1, int(round(len(sorted_c) * 0.95)) - 1)
    p95_idx = max(p95_idx, 0)
    cores_p95 = sorted_c[p95_idx]

    duration = m["duration_sec"]
    processed = m["processed_events"]
    cores_x_time = cores_avg * duration
    cost_per_mevent = (cores_x_time * 1_000_000.0) / processed if processed else 0.0
    tput_per_core = processed / cores_x_time if cores_x_time > 0 else 0.0

    return {
        "cores_avg":           cores_avg,
        "cores_p95":           cores_p95,
        "cores_peak":          cores_peak,
        "duration":            duration,
        "processed":           processed,
        "cores_x_time":        cores_x_time,
        "cost_per_mevent":     cost_per_mevent,
        "throughput_per_core": tput_per_core,
        "snapshots_used":      len(snapshots),
    }


def render_block(metrics) -> str:
    return (
        "\n========================================\n"
        "  Nexmark canonical metrics\n"
        "========================================\n"
        f"  Cores avg:         {metrics['cores_avg']:.2f} cores\n"
        f"  Cores p95:         {metrics['cores_p95']:.2f} cores\n"
        f"  Cores peak:        {metrics['cores_peak']:.2f} cores\n"
        f"  Time:              {metrics['duration']:.1f} s\n"
        f"  Cores × Time:      {metrics['cores_x_time']:.1f} core·s\n"
        f"  Events processed:  {metrics['processed']:,}\n"
        f"  Cost / Mevent:     {metrics['cost_per_mevent']:.2f} core·s / Mevents\n"
        f"  Throughput / core: {metrics['throughput_per_core']:,.0f} ev/s\n"
        f"  (snapshots used:   {metrics['snapshots_used']})\n"
        "========================================\n"
    )


# Strip any prior Nexmark block so re-runs stay idempotent.
RX_PRIOR_BLOCK = re.compile(
    r"\n=+\n\s+Nexmark canonical metrics\s*\n=+\n.*?=+\n",
    re.DOTALL,
)


def append_to_summary(summary_path: Path, block: str):
    text = summary_path.read_text() if summary_path.exists() else ""
    text = RX_PRIOR_BLOCK.sub("", text)
    summary_path.write_text(text.rstrip() + "\n" + block)


def collect_targets(paths):
    out = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"  [missing] {p}", file=sys.stderr)
            continue
        if (path / "autoscaler.log").exists():
            out.append(path)
        elif path.is_dir():
            for sub in path.iterdir():
                if sub.is_dir() and (sub / "autoscaler.log").exists():
                    out.append(sub)
    return sorted(set(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="Run dir(s) or parent dir(s)")
    args = parser.parse_args()

    targets = collect_targets(args.paths)
    if not targets:
        sys.exit("No usable run directories found.")

    for t in targets:
        metrics = compute_metrics(t)
        if metrics is None:
            print(f"  [skip] {t}  (missing autoscaler.log / metrics.csv / 0 events)")
            continue
        append_to_summary(t / "METRICS-SUMMARY.txt", render_block(metrics))
        print(f"  [ok]   {t.name:<22} "
              f"cores={metrics['cores_avg']:.2f}  "
              f"time={metrics['duration']:.0f}s  "
              f"cost/Mev={metrics['cost_per_mevent']:.1f}  "
              f"tput/core={metrics['throughput_per_core']:,.0f} ev/s")


if __name__ == "__main__":
    main()
