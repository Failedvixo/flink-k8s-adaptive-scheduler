#!/usr/bin/env python3
"""Results of one query's evaluation session: every block, every arm, the pre-declared comparisons.

WHY (2026-10-21). Sessions run one query at a time (scripts/run-session.sh), each block a separate
campaign with its own analysis. This gathers a query's blocks into one report, so a session's
result reads in one place the day it finishes:

  - per block and arm: delivered/offered and p50 latency over the LAST 10 MIN (steady state, the
    CAPSys protocol) and over the whole run (which includes settling, restarts and the bandit's
    exploration), per run and on average; bandit moves; where the heavy operator ended up;
  - the comparisons declared BEFORE the results (exact permutation test over runs):
      V4 vs STOCK                     does the trained planner help?
      BANDIT vs V4                    does monitoring add to it?
      STOCK_BANDIT vs STOCK           does the bandit work over any base?
      BANDIT vs STOCK_BANDIT          does v4 help the bandit?
      V4_<Qn> vs V4_PRIOR             does a table trained elsewhere beat not learning? (overfitting)
      V4_<Qn> vs V4 (own)             the generalisation gap
    plus the Bonferroni-corrected threshold for the primary pair of each block;
  - written to results/sessions/<query>/report.md and printed.

Usage:
  scripts/report_session.py q5
  scripts/report_session.py q5 --block A=results/monitor-campaign/20261019-191345   # any campaign
"""
import argparse
import collections
import csv
import glob
import itertools
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK_TITLES = {"A": "constante, sin reescalado", "G": "constante, generalización",
                "V": "sinusoide, sin reescalado", "R": "sinusoide, con reescalado"}
PRIMARY = {"A": [("V4", "STOCK"), ("BANDIT", "V4"), ("STOCK_BANDIT", "STOCK"), ("BANDIT", "STOCK_BANDIT")],
           "V": [("V4", "STOCK"), ("BANDIT", "V4"), ("STOCK_BANDIT", "STOCK"), ("BANDIT", "STOCK_BANDIT")],
           "R": [("V4", "STOCK"), ("BANDIT", "V4"), ("BANDIT", "V4_PRIOR_BANDIT")]}
LAST = 600.0


def window(led, start, end):
    """(delivered/offered, mean p50 latency in s) between two elapsed times of a ledger."""
    a = min(led, key=lambda r: abs(float(r["elapsed_s"]) - start))
    b = min(led, key=lambda r: abs(float(r["elapsed_s"]) - end))
    off = float(b["offered_cum"]) - float(a["offered_cum"])
    lats = [float(r["p50_ms"]) for r in led if r["p50_ms"] not in ("", "None")
            and start <= float(r["elapsed_s"]) <= end]
    return ((float(b["delivered_cum"]) - float(a["delivered_cum"])) / off if off > 0 else None,
            st.mean(lats) / 1000 if lats else None)


def load_campaign(camp):
    runs = []
    for line in open(Path(camp) / "runs.txt"):
        p, arm, d = line.split()
        led_f = Path(d) / "ledger.csv"
        if not led_f.exists():
            continue
        led = list(csv.DictReader(open(led_f)))
        if len(led) < 10:
            continue
        t_end = float(led[-1]["elapsed_s"])
        w_ratio, w_lat = window(led, t_end - LAST, t_end)
        f_ratio, f_lat = window(led, 0, t_end)
        mon = [r for f in glob.glob(f"{d}/monitor2-*.csv") for r in csv.DictReader(open(f))]
        late = [r for r in mon if float(r.get("elapsed_s") or 0) >= t_end - LAST] or mon
        placed = collections.Counter(r.get("arm", "") for r in late).most_common(1)
        runs.append({"pass": int(p), "arm": arm, "w": w_ratio, "w_lat": w_lat, "f": f_ratio,
                     "f_lat": f_lat, "moves": sum(r.get("decision") == "mover" for r in mon),
                     "placed": placed[0][0] if placed else "", "dir": d})
    return runs


def perm_p(x, y):
    """Exact two-sided permutation test on the difference of means."""
    obs = abs(st.mean(x) - st.mean(y))
    pool, n, hits, tot = x + y, len(x), 0, 0
    for c in itertools.combinations(range(len(pool)), n):
        a = [pool[i] for i in c]
        b = [pool[i] for i in range(len(pool)) if i not in c]
        hits += abs(st.mean(a) - st.mean(b)) >= obs - 1e-12
        tot += 1
    return hits / tot


def compare(runs, a, b, key):
    x = [r[key] for r in runs if r["arm"] == a and r[key] is not None]
    y = [r[key] for r in runs if r["arm"] == b and r[key] is not None]
    if len(x) < 2 or len(y) < 2:
        return None
    return st.mean(x) / st.mean(y) - 1, perm_p(x, y), len(x), len(y)


def fmt_pct(v):
    return "—" if v is None else f"{v:.1%}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("query")
    ap.add_argument("--block", action="append", default=[],
                    help="NAME=campaign dir, overriding the session's marker (e.g. A=results/...)")
    args = ap.parse_args()
    sess = ROOT / "results" / "sessions" / args.query
    blocks = {}
    for b in "AGVR":
        mark = sess / f"block-{b}.done"
        if mark.exists():
            blocks[b] = ROOT / mark.read_text().strip()
    for o in args.block:
        name, _, camp = o.partition("=")
        blocks[name] = Path(camp) if Path(camp).is_absolute() else ROOT / camp
    if not blocks:
        sys.exit(f"sin bloques terminados en {sess} (ni --block)")

    out = [f"# Resultados de la sesión {args.query}", ""]
    own_v4_w = None
    for b in "AGVR":
        if b not in blocks:
            continue
        runs = load_campaign(blocks[b])
        arms = list(dict.fromkeys(r["arm"] for r in runs))
        out += [f"## Bloque {b} — {BLOCK_TITLES.get(b, b)}", f"campaña: `{blocks[b].relative_to(ROOT)}`", "",
                "| Brazo | n | Entrega últimos 10 min | Latencia p50 (10 min) | Entrega toda la corrida "
                "| Cambios | Por corrida (últimos 10 min) | Pesado al final |",
                "|---|---|---|---|---|---|---|---|"]
        for a in arms:
            rs = sorted((r for r in runs if r["arm"] == a), key=lambda r: r["pass"])
            ws = [r["w"] for r in rs if r["w"] is not None]
            ls_ = [r["w_lat"] for r in rs if r["w_lat"] is not None]
            fs = [r["f"] for r in rs if r["f"] is not None]
            places = collections.Counter(r["placed"] for r in rs).most_common(1)
            out.append(f"| {a} | {len(rs)} | {fmt_pct(st.mean(ws)) if ws else '—'} | "
                       f"{(f'{st.mean(ls_):.1f} s') if ls_ else '—'} | {fmt_pct(st.mean(fs)) if fs else '—'} | "
                       f"{st.mean(r['moves'] for r in rs):.1f} | {' · '.join(fmt_pct(r['w']) for r in rs)} | "
                       f"{places[0][0] if places else ''} |")
        if b == "A":
            own = [r["w"] for r in runs if r["arm"] == "V4" and r["w"] is not None]
            own_v4_w = own or None
        pairs = list(PRIMARY.get(b, []))
        if b == "G":
            pairs = [(a, "V4_PRIOR") for a in arms if a.startswith("V4_Q")]
        pairs = [(x, y) for x, y in pairs if x in arms and y in arms]
        if pairs:
            k = len(pairs)
            out += ["", f"Comparaciones declaradas (permutación exacta; con Bonferroni sobre {k} "
                        f"comparaciones el umbral es p < {0.05 / k:.4f}):", ""]
            for x, y in pairs:
                for key, label in (("w", "entrega 10 min"), ("w_lat", "latencia 10 min"), ("f", "entrega toda la corrida")):
                    c = compare(runs, x, y, key)
                    if c:
                        d, p, nx, ny = c
                        mark = " ✔" if p < 0.05 / k else (" (p<0,05 sin corregir)" if p < 0.05 else "")
                        out.append(f"- {x} vs {y}, {label}: {d:+.1%}, p={p:.3f} (n={nx} vs {ny}){mark}")
        if b == "G" and own_v4_w:
            out += ["", "Brecha de generalización (últimos 10 min, contra el V4 propio del bloque A):", ""]
            for a in arms:
                if a.startswith("V4_Q"):
                    x = [r["w"] for r in runs if r["arm"] == a and r["w"] is not None]
                    if len(x) >= 2:
                        out.append(f"- {a}: {st.mean(x):.1%} vs V4 propio {st.mean(own_v4_w):.1%} "
                                   f"({st.mean(x) / st.mean(own_v4_w) - 1:+.1%}, p={perm_p(x, own_v4_w):.3f})")
        out.append("")
    text = "\n".join(out)
    if sess.exists() or not args.block:
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
