#!/usr/bin/env python3
"""One line per rate from a calibration sweep: is the cluster still keeping up?

Reads the LAST N runs of results/placement-experiment (N = however many rates were
swept, default 4) or the directories given as arguments.

READ THE BACKPRESSURE, NOT THE RATIO. The ratio of emitted to requested sits at
0.79-0.82 at EVERY rate, including ones the cluster absorbs comfortably: that is Beam's
generator delivering about four fifths of nominal, not the cluster falling behind, so it
discriminates nothing. Backpressure and the spread of the end-to-end delay are what move
at the knee.

AND CHECK `cred`. A row with creditable=0 is not a measurement of the placement — the
commonest cause was a window that straddled a rescale, which is why the whole sweep of
2026-09-21 had to be thrown away and repeated.

Usage:
    scripts/summarise-calibration.py
    scripts/summarise-calibration.py --last 5
    scripts/summarise-calibration.py results/placement-experiment/2026*-0*/
"""
import argparse
import csv
import glob
import json
import os
import sys

# argparse, not hand-rolled flag parsing: the first version treated the VALUE of
# --last as a directory name and silently printed an empty table (2026-09-21).
ap = argparse.ArgumentParser()
ap.add_argument("dirs", nargs="*", help="run directories; default: the most recent ones")
ap.add_argument("--last", type=int, default=4, help="how many recent runs to read")
opts = ap.parse_args()

dirs = opts.dirs or sorted(glob.glob("results/placement-experiment/*/"),
                           key=os.path.getmtime)[-opts.last:]

rows = []
for d in dirs:
    try:
        meta = json.load(open(os.path.join(d, "run.json")))
    except Exception:
        continue
    rate = meta.get("rate", 0)
    files = sorted(glob.glob(os.path.join(d, "*", "episodes-*.csv")))
    if not files:
        rows.append((rate, None, d))
        continue
    for f in files:
        for r in csv.DictReader(open(f)):
            rows.append((rate, r, d))

print(f"{'pedida':>7} {'emitida':>8} {'coc':>5} {'sl':>3} {'bp':>6} {'sink':>8} "
      f"{'tput/slot':>9} {'e2e sprd':>8} {'cred':>4}  nota")
for rate, r, d in sorted(rows, key=lambda x: x[0]):
    if r is None:
        print(f"{rate:7}  (sin episodios — {os.path.basename(d.rstrip('/'))})")
        continue
    print(f"{rate:7} {float(r['source_out_rps']):8.0f} "
          f"{float(r['source_out_rps'])/rate if rate else 0:5.2f} "
          f"{r['slices']:>3} "
          f"{float(r['backpressure_mean_ms_s']):6.1f} "
          f"{float(r['sink_in_rps']):8.0f} "
          f"{float(r['throughput_per_slot']):9.0f} "
          f"{float(r['e2e_delay_spread_ms']):8.1f} "
          f"{r['creditable']:>4}  {r['credit_note'][:44]}")
