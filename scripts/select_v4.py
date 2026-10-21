#!/usr/bin/env python3
"""Pick the ONE v4 table used in blocks A, V and R of every query, from the G blocks of all queries.

WHY (2026-10-21). The design runs in two phases: G (every trained table plus the untrained one,
constant load, 5 repetitions, on each of the five queries) SELECTS; A, V and R then EVALUATE with
the selected table on fresh runs, so the selection does not inflate their results. One table for
every query is the generalist v4 the cross-query protocol asks about: trained on one query,
deployed on all.

THE RULE, fixed before any G result is seen:
  for each query and each metric — delivery over the last 10 min (higher is better), p50 latency
  and the sources' backpressure (lower is better) — rank the trained tables by their mean over the
  query's G runs; a table's score is its MEAN RANK over the 5 queries x 3 metrics; the lowest wins,
  ties broken by the higher mean delivery. Ranks, not raw values: the queries run at 4000 to 95000
  ev/s, so raw numbers would let one query decide. The untrained table (V4_PRIOR) is the control,
  ranked and shown but never selected.

Writes results/sessions/selected-v4.txt ("<table> <arm> <mean rank>") and prints the full ranking.

Usage:
  scripts/select_v4.py                 # needs results/sessions/<q>/block-G.done for every query
  scripts/select_v4.py --queries "q5 q8"   # a subset, for a look before all have run
"""
import argparse
import glob
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import report_session as rs                                    # noqa: E402

METRICS = (("w", "entrega", True), ("w_lat", "latencia p50", False), ("w_bp", "contrapresión", False))


def table_of(arm):
    q = arm[3:].lower()
    hits = sorted(glob.glob(str(ROOT / f"results/characterizer/{q}-v4-cpu2105-r*.json")))
    return hits[0][len(str(ROOT)) + 1:] if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", default="q3 q5 q6 q8 q11")
    args = ap.parse_args()
    ranks, delivery, table = {}, {}, {}
    print(f"{'query':6s} {'métrica':14s} orden (mejor primero)")
    for q in args.queries.split():
        mark = ROOT / "results" / "sessions" / q / "block-G.done"
        if not mark.exists():
            sys.exit(f"falta el bloque G de {q} ({mark})")
        runs = rs.load_campaign(ROOT / mark.read_text().strip(), 600.0)
        arms = sorted({r["arm"] for r in runs if r["arm"].startswith("V4_Q") or r["arm"] == "V4_PRIOR"})
        for key, name, higher in METRICS:
            means = {}
            for a in arms:
                v = [r[key] for r in runs if r["arm"] == a and r[key] is not None]
                if v:
                    means[a] = st.mean(v)
            order = sorted(means, key=lambda a: -means[a] if higher else means[a])
            for i, a in enumerate(order, 1):
                ranks.setdefault(a, []).append(i)
            print(f"{q:6s} {name:14s} " + "  ".join(f"{a}({means[a]:.3g})" for a in order))
        for a in arms:
            delivery.setdefault(a, []).extend(r["w"] for r in runs if r["arm"] == a and r["w"] is not None)
    print("\nrango medio (5 queries x 3 métricas; menor es mejor):")
    score = {a: st.mean(v) for a, v in ranks.items()}
    for a in sorted(score, key=lambda a: (score[a], -st.mean(delivery[a] or [0]))):
        print(f"  {a:10s} {score[a]:5.2f}   entrega media {st.mean(delivery[a] or [0]):.1%}"
              + ("   (control, no se selecciona)" if a == "V4_PRIOR" else ""))
    cands = [a for a in score if a != "V4_PRIOR"]
    best = min(cands, key=lambda a: (score[a], -st.mean(delivery[a] or [0])))
    line = f"{table_of(best)} {best} {score[best]:.2f}"
    (ROOT / "results" / "sessions").mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / "sessions" / "selected-v4.txt").write_text(line + "\n")
    print(f"\nSELECCIONADO: {line}  -> results/sessions/selected-v4.txt")


if __name__ == "__main__":
    main()
