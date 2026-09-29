#!/usr/bin/env python3
"""Why did a window's throughput move? Dips against checkpoints, per window.

WHY (2026-09-18). Consecutive ten-minute windows of the same job, same placement, differed by
a median of 7% and up to 16%, and one-minute windows by ~20% with some halving. The controller
now keeps each window's throughput in 10-second bins (window-epochNNN.csv) and the checkpoints
triggered inside it (checkpoints-epochNNN.csv). This asks the one question that decides the
fix: do the dips line up with checkpoints more than chance would put them there?

A bin is a DIP when it falls below 80% of its window's median. A bin is NEAR a checkpoint when
one was triggered in the 30 s before it or during it — an upload competes with the pipeline
for CPU and disk in the seconds after it starts. The comparison that matters is the share of
dip bins near a checkpoint against the share of ALL bins near one: if the first is much larger,
checkpoints are driving the dips; if they are about equal, they are not.

Usage: scripts/diagnose-windows.py results/placement-experiment/<run> [more runs...]
"""
import csv
import statistics as st
import sys
from pathlib import Path

DIP = 0.8
BEFORE, AFTER = 30.0, 10.0

total_bins = near_bins = dip_bins = dip_near = 0
print(f"{'celda':34} {'ép':>3} {'media':>7} {'mediana':>8} {'cv':>6} {'mín':>7} "
      f"{'ckpt':>4} {'ckpt ms':>8} {'caídas':>6}")
for run in sys.argv[1:]:
    for window in sorted(Path(run).glob("*/window-epoch*.csv")):
        epoch = window.stem.replace("window-epoch", "")
        rows = [r for r in csv.DictReader(open(window)) if r["rps_counter_bin"]]
        if not rows:
            continue
        rates = [float(r["rps_counter_bin"]) for r in rows]
        times = [float(r["t_s"]) for r in rows]
        median = st.median(rates)
        ckpt_file = window.with_name(f"checkpoints-epoch{epoch}.csv")
        ckpts = []
        if ckpt_file.exists():
            ckpts = [r for r in csv.DictReader(open(ckpt_file))]
        triggers = [float(c["t"]) for c in ckpts]
        durations = [float(c["duration_ms"]) for c in ckpts if c["duration_ms"]]
        dips = 0
        for t, rate in zip(times, rates):
            near = any(t - BEFORE <= c <= t + AFTER for c in triggers)
            total_bins += 1
            near_bins += near
            if rate < DIP * median:
                dips += 1
                dip_bins += 1
                dip_near += near
        cv = st.pstdev(rates) / st.mean(rates) if st.mean(rates) else 0
        cell = f"{Path(run).name}/{window.parent.name}"
        print(f"{cell:34} {epoch:>3} {st.mean(rates):7.0f} {median:8.0f} {cv:6.3f} "
              f"{min(rates):7.0f} {len(ckpts):>4} "
              f"{(st.mean(durations) if durations else 0):8.0f} {dips:>6}")

print()
if not total_bins:
    sys.exit("sin ventanas registradas — ¿la corrida es anterior al 2026-09-18?")
base = near_bins / total_bins
print(f"bins totales: {total_bins}   caídas (<{int(DIP*100)}% de la mediana): {dip_bins}")
print(f"cerca de un checkpoint: {100*base:.0f}% de TODOS los bins")
if dip_bins:
    share = dip_near / dip_bins
    print(f"cerca de un checkpoint: {100*share:.0f}% de las CAÍDAS")
    if share > base * 1.5 and dip_near >= 3:
        print("-> las caídas se concentran junto a los checkpoints: son la causa probable.")
    elif share < base * 1.2:
        print("-> las caídas NO se concentran junto a los checkpoints: la causa está en otra parte.")
    else:
        print("-> relación débil: harían falta más ventanas para decidir.")
