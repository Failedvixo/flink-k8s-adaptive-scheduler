#!/usr/bin/env python3
"""Analyse a controlled placement experiment (scripts/run-placement-experiment.sh).

The campaign's own analyser ranks scenarios by the raw spread of arm means, which is
what made it report "spread 9.5%, best=GA" for a run whose arms had simply been
handed different rescale configurations. This one refuses to make that mistake:

  * it STRATIFIES by (slices, free_slots) and only ever compares arms inside one
    stratum, because the reward tracks the configuration far more strongly than the
    arm (measured 2026-08-06: 0.14 between strata, 0.005-0.025 between arms within
    one);
  * it reports the WITHIN-arm spread next to the BETWEEN-arm spread, since a
    difference smaller than the noise it sits on is not a finding;
  * it runs a permutation test, which is the honest thing to do at n=3 — no
    normality assumption, no scipy.

Usage:
    python3 scripts/analyse_placement_experiment.py results/placement-experiment/<stamp>
    python3 scripts/analyse_placement_experiment.py <dir> --metric throughput_per_slot
"""

import argparse
import csv
import glob
import json
import os
import random
import statistics as st
import sys
from collections import defaultdict

# Reported for every arm; the first is the one the significance test runs on unless
# --metric says otherwise.
METRICS = [
    ("reward", "reward", "%.3f"),
    ("throughput_per_slot", "tput/slot", "%.0f"),
    # End-to-end event-time delay at the sink, and how far apart the sink subtasks
    # are. Lower is better for both, which is the opposite direction to reward —
    # the table is read per column, not across it.
    ("e2e_delay_ms", "e2e ms", "%.0f"),
    ("e2e_delay_spread_ms", "e2e spr", "%.0f"),
    ("busy_mean_ms_s", "busy ms/s", "%.0f"),
    ("cv_busy_all", "cv_busy", "%.3f"),
    ("backpressure_mean_ms_s", "bp ms/s", "%.0f"),
]

# Metrics where a SMALLER value is better; the permutation test reports "best" by
# the right end for each.
LOWER_IS_BETTER = {"e2e_delay_ms", "e2e_delay_spread_ms", "cv_busy_all",
                   "backpressure_mean_ms_s"}


def load(run_dir):
    """Every creditable episode, tagged with the arm whose directory it came from."""
    episodes = []
    for cell in sorted(glob.glob(os.path.join(run_dir, "*"))):
        if not os.path.isdir(cell):
            continue
        arm = os.path.basename(cell)
        for path in glob.glob(os.path.join(cell, "episodes-*.csv")):
            with open(path) as fh:
                for row in csv.DictReader(fh):
                    if row.get("creditable") != "1":
                        continue
                    # The arm the JobManager LOGGED is the ground truth: a published
                    # arm that no rescale ever applied is not the arm that ran.
                    row["arm"] = row.get("arm_applied") or arm
                    row["cell"] = arm
                    episodes.append(row)
    return episodes


def fnum(row, key):
    try:
        return float(row[key])
    except (KeyError, ValueError, TypeError):
        return None


def pstdev(xs):
    return st.pstdev(xs) if len(xs) > 1 else 0.0


def permutation_p(groups, observed, iters=20000, seed=12345):
    """P(range of group means >= observed | arm labels are meaningless).

    Shuffles the arm labels across the pooled episodes and recomputes the spread.
    Appropriate at n=3, where a t-test's assumptions are decoration.
    """
    pooled = [v for vs in groups.values() for v in vs]
    sizes = [len(vs) for vs in groups.values()]
    if len(pooled) < 3 or len([s for s in sizes if s]) < 2:
        return None
    rng = random.Random(seed)
    hits = 0
    for _ in range(iters):
        rng.shuffle(pooled)
        i, means = 0, []
        for size in sizes:
            if size:
                means.append(st.mean(pooled[i:i + size]))
            i += size
        if max(means) - min(means) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (iters + 1)


def by_placement(rows):
    """Compare episodes by the placement that HAPPENED, not by the arm that was asked for.

    Measured 2026-08-13: a single arm does not always produce the same placement (FCFS
    separated the two expensive slices in 4 repetitions and concentrated them in 4).
    Comparing arms therefore dilutes the effect with the arm's own inconsistency — an
    intention-to-treat reading of something that is really about the treatment delivered.

    Two contrasts are printed:
      * pooled, every episode grouped by the placement it actually got;
      * WITHIN each arm that produced both, which is what rules out "that arm is simply
        worse" — same arm, same job, same configuration, only the placement differs.

    Placements are separated by the per-TM load dispersion, which is deterministic given
    the layout: with 4 slices of which 2 are expensive on 3 TaskManagers it takes exactly
    two values, sqrt(2)/2 when the expensive pair is split and sqrt(2) when it is not.
    """
    values = [v for v in (fnum(r, "cv_busy_all") for r in rows) if v is not None]
    if len(values) < 4:
        print("\nNot enough episodes with a dispersion value to split by placement.")
        return
    cut = (min(values) + max(values)) / 2.0
    # The split is only meaningful if the dispersions are genuinely bimodal. An absolute
    # epsilon is not enough: on 2026-08-17 every arm produced the same placement and the
    # values ranged 0.705-0.707, which cleared any epsilon and got cut at the midpoint,
    # inventing a "1 vs 39" contrast out of measurement jitter. Require the spread to be a
    # real fraction of the level before believing there are two placements to compare.
    span = max(values) - min(values)
    if span < 0.10 * abs(st.mean(values)):
        print(f"\n  Only ONE placement occurred: dispersion ranged {min(values):.3f}-"
              f"{max(values):.3f} across all {len(values)} episodes, which is jitter, not two "
              f"different layouts. Every arm made the same decision — there is nothing to "
              f"contrast, and any split here would be an artefact.")
        return

    balanced = [r for r in rows if (fnum(r, "cv_busy_all") or 0) < cut]
    packed = [r for r in rows if (fnum(r, "cv_busy_all") or 0) >= cut]

    print(f"\n{'=' * 72}")
    print(f"  By the placement that actually happened (dispersion split at {cut:.3f})")
    print("=" * 72)
    for name, group in (("load spread out", balanced), ("load concentrated", packed)):
        arms = defaultdict(int)
        for r in group:
            arms[r["arm"]] += 1
        detail = ", ".join(f"{a}×{n}" for a, n in sorted(arms.items()))
        print(f"  {name:<20} n={len(group):<3} {detail}")

    def contrast(a_rows, b_rows, title, indent="  "):
        print(f"\n{indent}{title}")
        for key, label, fmt in METRICS:
            a = [v for v in (fnum(r, key) for r in a_rows) if v is not None]
            b = [v for v in (fnum(r, key) for r in b_rows) if v is not None]
            if len(a) < 2 or len(b) < 2:
                continue
            groups = {"spread": a, "packed": b}
            diff = st.mean(a) - st.mean(b)
            p = permutation_p(groups, abs(diff))
            pct = 100.0 * diff / st.mean(b) if st.mean(b) else 0.0
            star = "  <-- " if p is not None and p <= 0.05 else ""
            print(f"{indent}  {label:<12} spread={fmt % st.mean(a):>9}  "
                  f"packed={fmt % st.mean(b):>9}  diff={pct:+6.1f}%  "
                  f"p={p:.4f}{star}" if p is not None else "")

    if balanced and packed:
        contrast(balanced, packed, "Pooled across arms:")

    for arm in sorted({r["arm"] for r in rows}):
        a = [r for r in balanced if r["arm"] == arm]
        b = [r for r in packed if r["arm"] == arm]
        if len(a) >= 2 and len(b) >= 2:
            contrast(a, b, f"WITHIN {arm} — same arm, only the placement differs:")

    spread_arms = {r["arm"] for r in balanced}
    if len(spread_arms) > 1:
        print(f"\n  Sanity check — arms compared only where they ALL spread the load "
              f"({', '.join(sorted(spread_arms))}):")
        base = sorted(spread_arms)[0]
        for other in sorted(spread_arms)[1:]:
            a = [v for v in (fnum(r, "throughput_per_slot")
                             for r in balanced if r["arm"] == base) if v is not None]
            b = [v for v in (fnum(r, "throughput_per_slot")
                             for r in balanced if r["arm"] == other) if v is not None]
            if len(a) >= 2 and len(b) >= 2:
                p = permutation_p({"a": a, "b": b}, abs(st.mean(a) - st.mean(b)))
                verdict = "no difference, as expected" if p and p > 0.10 else "DIFFERS — investigate"
                print(f"    {base} vs {other}: tput/slot p={p:.4f}  ({verdict})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--metric", default="reward",
                    help="metric the significance test runs on (default: reward)")
    ap.add_argument("--stratum", default=None,
                    help="force a stratum as 'slices/free_slots' instead of the largest")
    ap.add_argument("--by-placement", action="store_true",
                    help="compare by the placement that actually happened, not by which arm "
                         "was published (see by_placement() for why this is the causal contrast)")
    args = ap.parse_args()

    episodes = load(args.run_dir)
    if not episodes:
        print(f"No creditable episodes under {args.run_dir}", file=sys.stderr)
        return 1

    meta = {}
    meta_path = os.path.join(args.run_dir, "run.json")
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            meta = json.load(fh)

    print("=" * 72)
    print(f"  Controlled placement experiment — {args.run_dir}")
    if meta:
        print(f"  {meta.get('query')} / {meta.get('dist')} @ {meta.get('rate')} ev/s, "
              f"{meta.get('tm_replicas')} TMs, transition "
              f"{meta.get('submit_par')}->{meta.get('target_par')}, reps={meta.get('reps')}")
    print("=" * 72)

    # ---- strata -------------------------------------------------------------
    strata = defaultdict(list)
    for row in episodes:
        strata[(row.get("slices"), row.get("free_slots"))].append(row)

    print("\nEpisodes by rescale configuration (arms are only comparable WITHIN one):")
    print(f"  {'slices/free':<14}{'n':>4}   arms")
    for key in sorted(strata, key=lambda k: -len(strata[k])):
        rows = strata[key]
        arms = sorted({r["arm"] for r in rows})
        print(f"  {'/'.join(str(k) for k in key):<14}{len(rows):>4}   "
              f"{len(arms)} ({', '.join(arms)})")

    if args.stratum:
        want = tuple(args.stratum.split("/"))
    else:
        # A stratum holding a single arm proves nothing, and one where slices ==
        # freeSlots proves less than nothing: every arm is forced into the same
        # placement there, so its spread is a pure noise reading. Prefer the largest
        # stratum in which the assigner actually had slots to choose between.
        usable = [k for k in strata if len({r["arm"] for r in strata[k]}) > 1]
        if not usable:
            print("\nNo stratum contains more than one arm — nothing is comparable.",
                  file=sys.stderr)
            return 1

        def had_choice(key):
            try:
                return int(key[1]) > int(key[0])
            except (TypeError, ValueError):
                return False

        decisive = [k for k in usable if had_choice(k)]
        want = max(decisive or usable, key=lambda k: len(strata[k]))
        if not decisive:
            print("\n  ! No stratum in this run gave the assigner a choice "
                  "(freeSlots > slices).")

    rows = strata.get(want)
    if not rows:
        print(f"\nStratum {args.stratum} not present.", file=sys.stderr)
        return 1

    print(f"\nComparing inside stratum slices={want[0]} freeSlots={want[1]} "
          f"({len(rows)} episodes)")
    if want[0] == want[1]:
        print("  ! WARNING: slices == freeSlots, so the assigner had no real choice here.")
        print("    Every arm necessarily produced the same placement; any difference is noise.")

    # ---- per-arm table ------------------------------------------------------
    by_arm = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)

    header = f"  {'arm':<14}{'n':>3}"
    for _, label, _ in METRICS:
        header += f"{label:>14}"
    header += f"{'hosting':>10}"
    print()
    print(header)
    print("  " + "-" * (len(header) - 2))

    order = [a for a in (meta.get("arms") or sorted(by_arm)) if a in by_arm]
    for arm in order + [a for a in sorted(by_arm) if a not in order]:
        eps = by_arm[arm]
        line = f"  {arm:<14}{len(eps):>3}"
        for key, _, fmt in METRICS:
            vals = [v for v in (fnum(r, key) for r in eps) if v is not None]
            line += (f"{fmt % st.mean(vals):>14}" if vals else f"{'—':>14}")
        hosting = sorted({r.get("tms_hosting") for r in eps})
        line += f"{','.join(h for h in hosting if h):>10}"
        print(line)

    # ---- signal vs noise ----------------------------------------------------
    metric = args.metric
    groups = {}
    for arm, eps in by_arm.items():
        vals = [v for v in (fnum(r, metric) for r in eps) if v is not None]
        if vals:
            groups[arm] = vals
    if len(groups) < 2:
        print(f"\nNot enough arms with a '{metric}' value to compare.")
        return 0

    means = {a: st.mean(v) for a, v in groups.items()}
    between = pstdev(list(means.values()))
    within_each = {a: pstdev(v) for a, v in groups.items()}
    pooled_within = st.mean(list(within_each.values()))
    observed_range = max(means.values()) - min(means.values())

    print(f"\nSignal vs noise on '{metric}' inside this stratum")
    print(f"  spread BETWEEN arms (sd of arm means):     {between:.4f}")
    print(f"  spread WITHIN arms  (mean sd per arm):     {pooled_within:.4f}")
    print(f"  observed range (best - worst):             {observed_range:.4f}")
    if metric in LOWER_IS_BETTER:
        best, worst = min(means, key=means.get), max(means, key=means.get)
    else:
        best, worst = max(means, key=means.get), min(means, key=means.get)
    print(f"  best={best} ({means[best]:.4f})   worst={worst} ({means[worst]:.4f})"
          f"{'   (lower is better)' if metric in LOWER_IS_BETTER else ''}")

    if pooled_within > 0:
        n_min = min(len(v) for v in groups.values())
        se = pooled_within / (n_min ** 0.5)
        print(f"  approx SE of an arm mean at n={n_min}:        {se:.4f}  "
              f"(a real difference needs to clear roughly {2.8 * se:.4f})")

    if args.by_placement:
        by_placement(rows)
        return 0

    p = permutation_p(groups, observed_range)
    if p is not None:
        print(f"  permutation test on the range:             p = {p:.4f}")
        if p > 0.10:
            print("\n  => NOT DISTINGUISHABLE FROM NOISE. At this n, relabelling the arms at")
            print("     random reproduces this spread easily. Do not rank the arms from it,")
            print("     and do not train a meta-scheduler on it — raise REPS and re-run.")
        elif p > 0.05:
            print("\n  => SUGGESTIVE but not conclusive. Raise REPS before claiming anything.")
        else:
            print("\n  => THE ARMS DIFFER on this metric, at this transition. This is the")
            print("     evidence the meta-scheduler needs to have something to learn.")

    if pooled_within > 0 and between < pooled_within:
        print("\n  Note: the spread between arms is SMALLER than the spread within a single")
        print("  arm — repeated measurements of the SAME arm disagree more than the arms")
        print("  disagree with each other.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
