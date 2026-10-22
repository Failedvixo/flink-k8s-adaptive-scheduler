#!/usr/bin/env python3
"""Pick the ONE v4 table used in blocks A, V and R of every query, from the G blocks of all queries.

WHY (2026-10-21). The design runs in two phases: G (every trained table plus the untrained one,
constant load, 5 repetitions, on each of the five queries) SELECTS; A, V and R then EVALUATE with
the selected table on fresh runs, so the selection does not inflate their results. One table for
every query is the generalist v4 the cross-query protocol asks about: trained on one query,
deployed on all.

THE RULE, fixed before any G result is seen — the MOST STABLE table across queries, not the best
on average (Vicente, 2026-10-21: a table excellent on one query and poor on the rest must not win
on its mean):
  1. for each query and each metric — delivery over the last 10 min (higher is better), p50
     latency and the sources' backpressure (lower is better) — rank the trained tables by their
     mean over the query's G runs; a table's position in a query = its mean rank over the 3 metrics;
  2. its WORST position over the 5 queries;
  3. the best (lowest) worst case wins — minimax; ties go to the lower mean position over the
     queries, then to the higher mean delivery.
  Ranks, not raw values: the queries run at 4000 to 95000 ev/s, so raw numbers would let one query
  decide. The untrained table (V4_PRIOR) is the control, ranked and shown but never selected.

Writes results/sessions/selected-v4.txt ("<table> <arm> <worst position>") and prints the full ranking.

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
    hits = sorted(glob.glob(str(ROOT / f"results/characterizer/{q}-v4-cpu2105-r*-ucb.json")))
    return hits[0][len(str(ROOT)) + 1:] if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", default="q3 q5 q6 q8 q11")
    args = ap.parse_args()
    ranks, delivery, table = {}, {}, {}
    per_query = {}                  # arm -> [its mean rank over the 3 metrics, one per query]
    print(f"{'query':6s} {'métrica':14s} orden (mejor primero)")
    for q in args.queries.split():
        mark = ROOT / "results" / "sessions" / q / "block-G.done"
        if not mark.exists():
            sys.exit(f"falta el bloque G de {q} ({mark})")
        runs = rs.load_campaign(ROOT / mark.read_text().strip(), 600.0)
        arms = sorted({r["arm"] for r in runs if r["arm"].startswith("V4_Q") or r["arm"] == "V4_PRIOR"})
        q_ranks = {}
        for key, name, higher in METRICS:
            means = {}
            for a in arms:
                v = [r[key] for r in runs if r["arm"] == a and r[key] is not None]
                if v:
                    means[a] = st.mean(v)
            order = sorted(means, key=lambda a: -means[a] if higher else means[a])
            for i, a in enumerate(order, 1):
                ranks.setdefault(a, []).append(i)
                q_ranks.setdefault(a, []).append(i)
            print(f"{q:6s} {name:14s} " + "  ".join(f"{a}({means[a]:.3g})" for a in order))
        for a in arms:
            delivery.setdefault(a, []).extend(r["w"] for r in runs if r["arm"] == a and r["w"] is not None)
            if q_ranks.get(a):
                per_query.setdefault(a, []).append(st.mean(q_ranks[a]))
    def key(a):
        return (max(per_query[a]), st.mean(per_query[a]), -st.mean(delivery[a] or [0]))
    print("\nposición por query (rango medio en 3 métricas; menor es mejor), peor caso y promedio:")
    for a in sorted(per_query, key=key):
        print(f"  {a:10s} por query {' '.join(f'{x:4.2f}' for x in per_query[a])}   "
              f"PEOR {max(per_query[a]):4.2f}   promedio {st.mean(per_query[a]):4.2f}   "
              f"entrega media {st.mean(delivery[a] or [0]):.1%}"
              + ("   (control, no se selecciona)" if a == "V4_PRIOR" else ""))
    cands = [a for a in per_query if a != "V4_PRIOR"]
    if not cands:
        sys.exit("ninguna tabla entrenada en los bloques G")
    best = min(cands, key=key)
    line = f"{table_of(best)} {best} {max(per_query[best]):.2f}"
    (ROOT / "results" / "sessions").mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / "sessions" / "selected-v4.txt").write_text(line + "\n")
    print(f"\nSELECCIONADO: {line}  -> results/sessions/selected-v4.txt")


if __name__ == "__main__":
    main()
