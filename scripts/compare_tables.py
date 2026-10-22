#!/usr/bin/env python3
"""Compare two v4 tables of the same query: how much each explored, and what each would decide.

WHY (2026-10-21). The epsilon-greedy tables of Q8 and Q3 never tried medium for the SECOND join
(the state is reached late, with epsilon already low) and proposed fast+medium where medium x2 is
best by 10-13 points in the fixed-placement calibration. The tables were retrained with UCB; this
says whether that changed anything that matters, offline, before spending cluster time:

  1. COVERAGE: states, visits, and in how many visited states every allowed action was tried;
  2. THE HEAVY OPERATOR: for each state of a heavy slice (load=HIGH), the action each table picks,
     its value and its visits, side by side;
  3. THE OPENING PLAN for the heavy operator: walk the states the first and second heavy subtask
     meet (first on an empty cluster, second after the first is placed) and report where each
     table sends them, so it can be set against the calibration (which placement delivers what).

Usage:
  scripts/compare_tables.py results/characterizer/q8-v4-cpu2105-r20000.json \\
                            results/characterizer/q8-v4-cpu2105-r20000-ucb.json --disk Y
  (--disk Y for a heavy operator that writes state — Q3, Q8 —, N for a CPU one — Q5, Q6, Q11)
"""
import argparse
import json
import sys

M = {"0": "fast", "1": "medium", "2": "slow"}
FREE0 = (2, 4, 6)                  # slots per class: fast, medium, slow


def load(path):
    d = json.load(open(path))
    return d["q"], d.get("n", {})


def coverage(q, n):
    states = len(q)
    visits = sum(sum(c.values()) for c in n.values())
    full = sum(1 for s, c in n.items() if len(c) >= 3)
    single = sum(1 for s, c in n.items() if sum(c.values()) >= 3 and len(c) == 1)
    return states, visits, full, single


def pick(q, n, state):
    acts = q.get(state)
    if not acts:
        return None, "nunca visitado (usa la regla inicial: la más rápida con espacio)"
    best = max(acts, key=acts.get)
    detail = "  ".join(f"{M[a]} {acts[a]:+.2f}/{n.get(state, {}).get(a, 0)}" for a in sorted(acts))
    return best, detail


def state_of(disk, heavy):
    free = "".join(str(min(2, f - h)) for f, h in zip(FREE0, heavy))   # capped as the agent does
    return f"load=HIGH|disk={disk}|free={free}|heavy={''.join(str(h) for h in heavy)}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("eps")
    ap.add_argument("ucb")
    ap.add_argument("--disk", choices=["Y", "N"], default="Y")
    args = ap.parse_args()
    tables = {"ε-greedy": load(args.eps), "UCB": load(args.ucb)}

    print("1. COBERTURA")
    for name, (q, n) in tables.items():
        s, v, full, single = coverage(q, n)
        print(f"   {name:9s} estados {s:3d}  visitas {v:4d}  estados con las 3 máquinas probadas {full:3d}"
              f"  estados con >=3 visitas y UNA sola máquina probada {single:3d}")

    print("\n2. ESTADOS DE OPERADOR PESADO (máquina elegida; valor/visitas por máquina)")
    keys = sorted({s for q, _n in tables.values() for s in q if "load=HIGH" in s})
    for s in keys:
        row = []
        for name, (q, n) in tables.items():
            best, detail = pick(q, n, s)
            row.append(f"{name}: {M.get(best, '—'):6s} [{detail}]")
        print(f"   {s}\n      " + "\n      ".join(row))

    print(f"\n3. PLAN DE APERTURA del operador pesado (disk={args.disk}): primera y segunda subtarea")
    for name, (q, n) in tables.items():
        heavy = [0, 0, 0]
        plan = []
        for _ in range(2):
            st_ = state_of(args.disk, heavy)
            best, detail = pick(q, n, st_)
            if best is None:                     # the prior: fastest class with room
                best = next(str(i) for i in range(3) if heavy[i] < FREE0[i])
            heavy[int(best)] += 1
            plan.append(f"{M[best]} ({st_.split('|heavy=')[1]}: {detail})")
        print(f"   {name:9s} -> " + "  luego  ".join(plan))
        print(f"   {'':9s}    = {'+'.join(f'{M[str(i)]}x{h}' for i, h in enumerate(heavy) if h)}")


if __name__ == "__main__":
    sys.exit(main())
