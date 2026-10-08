#!/usr/bin/env python3
"""Compare placement arms across two campaign passes run in opposite order.

THREE THINGS THIS DOES THAT THE PLAIN ANALYSER DOES NOT, each because the data
made them necessary on 2026-09-03:

  * DROPS THE FIRST EPISODE of every cell. The first measurement of a run came out
    at 0.658 of the requested rate where the other five in that arm sat between
    0.937 and 1.000 — a cold cluster, not a placement. Keeping it moved the arm's
    mean by five points and its standard deviation by a factor of five.

  * REPORTS EACH ARM PER PASS. An arm that wins in both passes won on merit; one
    that wins only in the pass where it goes first won on position. Position was
    worth 0.127 of ratio to LPT and 0.043 to STOCK in the capacity experiment,
    which is the size of the effect being looked for.

  * REPORTS BACKPRESSURE ALONGSIDE THROUGHPUT. Throughput saturates: an arm cannot
    emit more than it was asked for, so below capacity every arm reads 1.0 and
    differences are invisible. Source backpressure has no such ceiling, and it is
    what CAPSys reports for the same reason.

  * TESTS ON JOBS, NOT ON EPISODES (added 2026-09-15). Inside one job an arm repeats
    the SAME placement at every repetition — STOCK draws its placement once and state
    locality sends each rescale back to the same slots — so a cell's three episodes are
    one sample measured three times. The episode-level p pretends otherwise: the headline
    campaign of 2026-09-08 reported p=0.0038 on 11 vs 14 episodes, and on its 4 vs 4 jobs
    the same data give p=0.029; adding the reproduction of 2026-09-15 (same placement for
    LPT, two new draws for STOCK) gives p=0.058. The job line is the one to cite.

Usage:
    python3 scripts/analyse_paired_campaign.py <run-dir-A> <run-dir-B> [more run dirs...]
"""

import csv
import glob
import os
import random
import statistics as st
import sys
from collections import defaultdict

random.seed(0)
# THE MEDIAN OF THE WINDOW (2026-09-18). The mean of a ten-minute window lets a two-minute
# collapse drag the whole number down, and consecutive windows of the SAME job and placement
# differed by a median of 7% (up to 16%). The median of the 10-second bins ignores a short
# collapse without discarding anything by hand. Shown only when every episode carries it.
# e2e is OPTIONAL since 2026-10-05: it is measured from the sink's event-time watermark, and Q3
# has no watermarks at all (an unwindowed join on processing-time timers) — every Q3 episode
# carried an empty e2e and the whole first Q3 campaign was dropped as "sin episodios utilizables".
# The per-record latency (LatencyMeter in the reference sinks, 2026-10-08) is what the thesis calls
# latency; the watermark lag stays only for comparison with campaigns recorded before it.
OPTIONAL_METRICS = [("latency_p50_ms", "latencia por registro p50 ms", True),
                    ("latency_p99_ms", "latencia por registro p99 ms", True),
                    ("e2e_delay_ms", "atraso del sink (watermark) ms", True),
                    ("rps_median", "reg/s MEDIANA de la ventana", False)]
KEEP_FIRST = False
WANT_SLICES = None
METRICS = [("source_out_rps", "reg/s", False),
           ("backpressure_mean_ms_s", "bp ms/s", True),
           ("sink_in_rps", "resultados/s (sink)", False)]


def permutation_p(a, b, iterations=20000):
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    observed = abs(st.mean(a) - st.mean(b))
    pool = list(a) + list(b)
    hits = 0
    for _ in range(iterations):
        random.shuffle(pool)
        if abs(st.mean(pool[:len(a)]) - st.mean(pool[len(a):])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (iterations + 1)


def exact_permutation_p(a, b, iterations=20000):
    """Two-sided; enumerates every split when there are few, as there are with jobs."""
    from itertools import combinations
    from math import comb
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    pool = list(a) + list(b)
    if comb(len(pool), len(a)) > 200000:
        return permutation_p(a, b, iterations)
    observed = abs(st.mean(a) - st.mean(b))
    total, n, m = sum(pool), len(a), len(pool) - len(a)
    hits = splits = 0
    for idx in combinations(range(len(pool)), n):
        part = sum(pool[i] for i in idx)
        splits += 1
        hits += abs(part / n - (total - part) / m) >= observed - 1e-12
    return hits / splits


def load(run_dir, pass_label):
    """Creditable episodes, minus the first of each cell."""
    rows = []
    for cell in sorted(glob.glob(os.path.join(run_dir, "*"))):
        if not os.path.isdir(cell):
            continue
        arm = os.path.basename(cell)
        for path in glob.glob(os.path.join(cell, "episodes-*.csv")):
            episodes = []
            for r in csv.DictReader(open(path)):
                if r.get("creditable", "").strip().lower() not in ("1", "true", "yes"):
                    continue
                try:
                    episode = {
                        "arm": arm, "pass": pass_label,
                        "epoch": int(r.get("epoch", 0)),
                        "slices": int(r["slices"]),
                        **{m: float(r[m]) for m, _, _ in METRICS},
                    }
                except (KeyError, ValueError):
                    continue
                # Optional, so campaigns recorded before these columns existed still load.
                for m, _, _ in OPTIONAL_METRICS:
                    try:
                        episode[m] = float(r[m])
                    except (KeyError, ValueError, TypeError):
                        pass
                episodes.append(episode)
            episodes.sort(key=lambda e: e["epoch"])
            # --keep-first (2026-09-15): with the CAPSys protocol — a 6-minute warm-up and
            # ONE measured episode per job — the first episode is the only one, and the
            # warm-up already does what dropping it was for.
            rows.extend(episodes if KEEP_FIRST else episodes[1:])
            if episodes:
                print(f"  {pass_label}/{arm}: {len(episodes)} episodios, "
                      f"{'se conservan todos' if KEEP_FIRST else 'descartado el primero'}",
                      file=sys.stderr)
    return rows


def main():
    global KEEP_FIRST, WANT_SLICES
    argv = sys.argv[1:]
    if "--slices" in argv:
        i = argv.index("--slices")
        WANT_SLICES = int(argv[i + 1])
        del argv[i:i + 2]
    args = [a for a in argv if a != "--keep-first"]
    KEEP_FIRST = len(args) != len(argv)
    if not args:
        sys.exit(__doc__)
    rows = []
    for i, run_dir in enumerate(args):
        rows.extend(load(run_dir, chr(ord("A") + i)))
    if not rows:
        sys.exit("sin episodios utilizables")

    # Stratify by slices: the campaign alternates a measured width and a restore
    # width, and pooling the two is what produced a discredited result once before.
    by_slices = defaultdict(list)
    for r in rows:
        by_slices[r["slices"]].append(r)
    # --slices N pins the width (2026-09-17). The largest stratum is the right default, but a
    # smoke test with short windows measured the TRANSITION step too, and that stratum won on
    # count: at 12 slices the fixed plans (written for 8) do not apply and the fork falls back
    # to LPT, so the arms were compared while both ran the same placement.
    if WANT_SLICES is not None:
        if WANT_SLICES not in by_slices:
            sys.exit(f"no hay episodios a {WANT_SLICES} slices; hay: {sorted(by_slices)}")
        stratum = by_slices[WANT_SLICES]
    else:
        stratum = max(by_slices.values(), key=len)
    width = stratum[0]["slices"]
    print(f"\nEstrato analizado: slices={width}  ({len(stratum)} episodios de "
          f"{len(rows)} totales)")

    arms = sorted({r["arm"] for r in stratum})
    missing = sorted({r["arm"] for r in rows} - set(arms))
    if missing:
        print(f"  ! sin episodios en este estrato: {', '.join(missing)}", file=sys.stderr)
    for arm in arms:
        cells = {(r["pass"], r["arm"]) for r in stratum if r["arm"] == arm}
        if len(cells) < len({r["pass"] for r in rows}):
            print(f"  ! {arm} aparece en {len(cells)} de "
                  f"{len({r['pass'] for r in rows})} pasadas", file=sys.stderr)
    shown = list(METRICS) + [m for m in OPTIONAL_METRICS
                             if all(m[0] in r for r in stratum)]
    for metric, label, lower_better in shown:
        print(f"\n{label}  ({'menor' if lower_better else 'mayor'} es mejor)")
        # ONE COLUMN PER PASS, not a fixed A/B (2026-09-22). These two columns were written
        # for the two-pass paired campaign and were never generalised: with the six-pass
        # multi-arm campaign they showed passes A and B and silently dropped C through F, so
        # a reader comparing "pasada A" across arms was reading one pass out of six and
        # believing it was half the data. That misread a campaign where RL's first pass was
        # the only one that still used a previous plan.
        labels = sorted({r["pass"] for r in stratum})
        header = "".join(f"{('pas ' + l):>9}" for l in labels)
        print(f"  {'brazo':14}{'n':>4}{'media':>12}{'sd':>10}{header}")
        print("  " + "-" * (40 + 9 * len(labels)))
        for arm in arms:
            vals = [r[metric] for r in stratum if r["arm"] == arm]
            sd = st.stdev(vals) if len(vals) > 1 else 0.0
            cells = ""
            for l in labels:
                v = [r[metric] for r in stratum if r["arm"] == arm and r["pass"] == l]
                cells += f"{st.mean(v):>9.0f}" if v else f"{'—':>9}"
            print(f"  {arm:14}{len(vals):>4}{st.mean(vals):>12.0f}{sd:>10.0f}{cells}")
        # EVERY PAIR, not just two arms (2026-09-17). Comparing two arms at a time forced one
        # campaign per pair, and campaigns live in different sessions: the SAME LPT placement
        # measured 31200 one night and 24414 another, a 22% drift that makes cross-session
        # chaining ("RL beats LPT, LPT beats STOCK, therefore...") indefensible. Three arms in
        # one session give all three comparisons under identical conditions.
        per_job = defaultdict(list)
        for r in stratum:
            per_job[(r["pass"], r["arm"])].append(r[metric])
        for i, first in enumerate(arms):
            for second in arms[i + 1:]:
                x = [r[metric] for r in stratum if r["arm"] == first]
                y = [r[metric] for r in stratum if r["arm"] == second]
                if not x or not y:
                    continue
                delta = 100 * (st.mean(y) - st.mean(x)) / max(abs(st.mean(x)), 1e-9)
                print(f"  {second} vs {first}: {delta:+.1f}%  "
                      f"p={permutation_p(x, y):.4f}  (por episodio: NO citar)")
                # One number per job: its episodes share a placement, so they are not
                # independent draws of the arm.
                jx = [st.mean(v) for (_, arm), v in sorted(per_job.items()) if arm == first]
                jy = [st.mean(v) for (_, arm), v in sorted(per_job.items()) if arm == second]
                if jx and jy:
                    jdelta = 100 * (st.mean(jy) - st.mean(jx)) / max(abs(st.mean(jx)), 1e-9)
                    print(f"  POR JOB (n={len(jx)} vs {len(jy)}): {jdelta:+.1f}%  "
                          f"p={exact_permutation_p(jx, jy):.4f}  <- este es el que vale")

    print("\nLECTURA. Un brazo que gana en las DOS pasadas ganó por mérito.")
    print("Uno que solo gana en la pasada donde va primero ganó por posición.")


if __name__ == "__main__":
    main()
