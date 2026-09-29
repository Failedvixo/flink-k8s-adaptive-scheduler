#!/usr/bin/env python3
"""Summarise a capacity-per-arm experiment, with the order effect made visible.

The metric is the fraction of the requested rate each arm sustained. A permutation
test rather than a t-test: n is small and nothing here is normal.

The order column is not decoration. Twice in this project a placement result turned
out to be position rather than arm — the headline campaign's STOCK got WORSE running
last, and the 2026-09-02 pair had LPT always first. If an arm only wins from one
position, that is reported as no result rather than as a finding.
"""

import csv
import random
import statistics as st
import sys
from collections import defaultdict

random.seed(0)


def permutation_p(a, b, iterations=20000):
    observed = abs(st.mean(a) - st.mean(b))
    pool = list(a) + list(b)
    hits = 0
    for _ in range(iterations):
        random.shuffle(pool)
        if abs(st.mean(pool[:len(a)]) - st.mean(pool[len(a):])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (iterations + 1)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "measurements.csv"
    by_arm = defaultdict(list)
    by_arm_pos = defaultdict(list)
    for row in csv.DictReader(open(path)):
        try:
            ratio = float(row["ratio"])
        except (KeyError, ValueError):
            continue
        by_arm[row["arm"]].append(ratio)
        by_arm_pos[(row["arm"], row["position"])].append(ratio)

    if not by_arm:
        print("sin mediciones utilizables")
        return

    print(f"{'brazo':14}{'n':>4}{'cociente medio':>16}{'sd':>9}{'min':>8}{'max':>8}")
    print("-" * 59)
    for arm in sorted(by_arm):
        vals = by_arm[arm]
        sd = st.stdev(vals) if len(vals) > 1 else 0.0
        print(f"{arm:14}{len(vals):>4}{st.mean(vals):>16.3f}{sd:>9.3f}"
              f"{min(vals):>8.3f}{max(vals):>8.3f}")

    arms = sorted(by_arm)
    if len(arms) == 2 and all(len(by_arm[a]) >= 3 for a in arms):
        a, b = arms
        diff = 100 * (st.mean(by_arm[b]) - st.mean(by_arm[a])) / max(st.mean(by_arm[a]), 1e-9)
        print(f"\n{b} vs {a}: {diff:+.1f}%  p={permutation_p(by_arm[a], by_arm[b]):.4f}")

    print(f"\n{'brazo':14}{'posición':>10}{'n':>4}{'cociente medio':>16}")
    print("-" * 44)
    for (arm, pos) in sorted(by_arm_pos):
        vals = by_arm_pos[(arm, pos)]
        print(f"{arm:14}{pos:>10}{len(vals):>4}{st.mean(vals):>16.3f}")
    print("\nSi un brazo solo gana desde una posición, es orden y no emplazamiento.")


if __name__ == "__main__":
    main()
