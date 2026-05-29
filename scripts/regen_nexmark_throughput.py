#!/usr/bin/env python3
"""Rewrite processed_events + throughput_processed in metrics.csv from job-details.json.

For Nexmark runs that completed but whose metrics.csv was filled by the old
ConfigurableGraphJob-shaped extractor (which missed heavy-vertex matches and
left throughput=N/A, events=0). Reads the heavy vertex's read-records from
job-details.json and updates the CSV row in place. Also clears any leftover
stray-newline corruption by re-emitting the row clean.

Pattern is inferred from the run-dir prefix (q5- → hot-items-count,
q8- → new-users-join). Override with --pattern.

Usage:
  python3 scripts/regen_nexmark_throughput.py results/q5-const/FCFS \\
                                              results/q5-sine/FCFS  \\
                                              results/q5-step/FCFS
  python3 scripts/regen_nexmark_throughput.py results/q5-*/FCFS
"""
import argparse
import csv
import io
import json
import sys
from pathlib import Path


def infer_pattern(run_dir: Path):
    name = "/".join(run_dir.parts).lower()
    if "q5-" in name:
        return "hot-items-count"
    if "q8-" in name:
        return "new-users-join"
    return None


def read_csv_row(csv_path: Path):
    """Return (header, row) tolerating the stray-newline corruption."""
    raw = csv_path.read_text()
    lines = raw.splitlines()
    header = next(csv.reader([lines[0]]))
    # Re-join data lines into one logical row and re-split by comma.
    data_blob = ",".join(line for line in lines[1:] if line.strip())
    row = next(csv.reader([data_blob]))
    # If field count drifted, pad/truncate so we can still index by header.
    if len(row) < len(header):
        row += [""] * (len(header) - len(row))
    elif len(row) > len(header):
        row = row[: len(header)]
    return header, row


def regen(run_dir: Path, pattern: str) -> bool:
    jd_path = run_dir / "job-details.json"
    csv_path = run_dir / "metrics.csv"
    if not jd_path.exists():
        print(f"  [skip] {run_dir}: no job-details.json")
        return False
    if not csv_path.exists():
        print(f"  [skip] {run_dir}: no metrics.csv")
        return False

    job = json.loads(jd_path.read_text())
    duration_ms = int(job.get("duration") or 0)
    if duration_ms <= 0:
        print(f"  [skip] {run_dir}: duration=0")
        return False

    heavy_in = 0
    heavy_name = None
    for v in job.get("vertices", []):
        if pattern in v.get("name", ""):
            heavy_in = int((v.get("metrics") or {}).get("read-records") or 0)
            heavy_name = v["name"]
            break

    if heavy_in == 0:
        print(f"  [skip] {run_dir}: no vertex matching {pattern!r}")
        return False

    throughput = round(heavy_in * 1000 / duration_ms)

    header, row = read_csv_row(csv_path)
    try:
        i_evt = header.index("processed_events")
        i_tput = header.index("throughput_processed")
    except ValueError as e:
        print(f"  [skip] {run_dir}: missing column {e}")
        return False

    row[i_evt] = str(heavy_in)
    row[i_tput] = str(throughput)

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerow(row)
    csv_path.write_text(buf.getvalue())

    print(
        f"  [ok]   {run_dir}: {heavy_name}  "
        f"events={heavy_in:,}  throughput={throughput:,} ev/s"
    )
    return True


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("run_dirs", nargs="+", help="Run directories to patch")
    p.add_argument("--pattern", help="Override heavy vertex name pattern")
    args = p.parse_args()

    any_ok = False
    for d in args.run_dirs:
        run_dir = Path(d)
        if not run_dir.is_dir():
            print(f"  [skip] {run_dir}: not a directory")
            continue
        pat = args.pattern or infer_pattern(run_dir)
        if not pat:
            print(f"  [skip] {run_dir}: cannot infer pattern (use --pattern)")
            continue
        any_ok = regen(run_dir, pat) or any_ok

    sys.exit(0 if any_ok else 1)


if __name__ == "__main__":
    main()
