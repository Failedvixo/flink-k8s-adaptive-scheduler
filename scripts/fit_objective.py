#!/usr/bin/env python3
"""Which cost function actually predicts the throughput we measured?

WHY THIS EXISTS (2026-09-02). The campaign of 2026-09-01 measured OPTIMAL beating
LPT by 1.6% where the cost model predicted 15.6%, which says the objective the
fork minimises is not the quantity that governs throughput. Building a better
objective by intuition costs two hours of cluster per attempt. This asks the same
question offline, against the ~30 campaigns already on disk: every one of them
logged the placement it chose AND the throughput that placement produced, so a
candidate objective can be scored on data that already exists.

WHAT IT DOES
    1. rebuilds each episode's placement instance — slice loads from the vertex
       costs published in driver.log, machine speeds from run.json, and the
       slice->TaskManager mapping from thesis-assign.log;
    2. VALIDATES the reconstruction before trusting it: LPT is a deterministic
       greedy, so replaying it on the rebuilt instance must reproduce the mapping
       the fork actually logged. The slice ordering is not documented anywhere, so
       the script tries every stage ordering and keeps the one LPT agrees with.
       A low agreement rate means the reconstruction is wrong and every number
       below it is meaningless — it is reported, not hidden;
    3. scores several candidate objectives on the placement each episode really
       ran, and correlates them (Spearman, so monotone-but-not-linear still
       counts) against the throughput that episode really produced.

THE CANDIDATES, and what each one believes about the machine:
    makespan        the busiest TaskManager's summed load / speed. What the fork
                    minimises today. Believes co-tenants split a machine in
                    proportion to what they ask for.
    dispersion      RMS deviation from each machine's capacity-proportional
                    share. The fork's other mode, and what the professor's note
                    about "load difference between machines" asks for.
    fair_share      time of the SLOWEST SLICE when every slice on a machine gets
                    an equal cut of it: load_g / (speed_t / slices_on_t).
    effective_cores time of the slowest slice when only BUSY co-tenants take a
                    cut — an idle co-tenant leaves its cores to whoever works.
                    This is the "a slot reserves memory, not CPU" hypothesis,
                    and the one no arm optimises.

Usage:
    python3 scripts/fit_objective.py                       # every campaign
    python3 scripts/fit_objective.py --campaigns 20260830-141910 20260830-204709
    python3 scripts/fit_objective.py --min-backpressure 280   # drop starved episodes
"""

import argparse
import csv
import glob
import itertools
import json
import os
import re
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# the job's PER_STAGE composition, read off ConfigurableGraphJob.group(...) calls
# ---------------------------------------------------------------------------
# Stage -> the vertices sharing it. A slice of a stage holds subtask i of every
# vertex in that stage whose parallelism reaches i, which is what makes slices
# inside one stage cost different amounts when a vertex is pinned narrower.
STAGE_OF_VERTEX = [
    ("Source:", "src"),
    ("Filter", "src"),
    ("CPU_Load", "cpu"),
    ("Latency_Tracker", "cpu"),
    ("Window", "win"),
    ("Sink", "snk"),
]
STAGES = ["src", "cpu", "win", "snk"]


def stage_of(vertex_name):
    for prefix, stage in STAGE_OF_VERTEX:
        if vertex_name.startswith(prefix):
            return stage
    return None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
LOAD_LINE = re.compile(r"^\s{2}(\S+)\s+par=(\d+)\s+([0-9.]+) ms/s per subtask\s*$")
ASSIGN_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[THESIS_ASSIGN\] "
    r"strategy=(\S+) slices=(\d+) freeSlots=(\d+) .*mapping=\[(.*)\]\s*$"
)
MAPPING_ENTRY = re.compile(r"slice#(\d+)->(\S+?)[,\]]?$")


def published_loads(driver_log):
    """Per-vertex cost in ms/s per subtask, as publish-loads.sh measured it."""
    if not os.path.exists(driver_log):
        return {}
    out = {}
    for line in open(driver_log, errors="replace"):
        m = LOAD_LINE.match(line.rstrip("\n"))
        if m:
            out[m.group(1)] = (int(m.group(2)), float(m.group(3)))
    return out


def assignments(assign_log):
    """Every placement the fork logged, newest last."""
    if not os.path.exists(assign_log):
        return []
    out = []
    for line in open(assign_log, errors="replace"):
        m = ASSIGN_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        mapping = []
        for part in m.group(5).split(", "):
            e = MAPPING_ENTRY.match(part.strip())
            if e:
                mapping.append(e.group(2))
        out.append({"ts": ts, "arm": m.group(2), "slices": int(m.group(3)),
                    "free_slots": int(m.group(4)), "mapping": mapping})
    return out


def episodes(cell_dir, arm):
    out = []
    for path in glob.glob(os.path.join(cell_dir, "episodes-*.csv")):
        for r in csv.DictReader(open(path)):
            if r.get("creditable", "").strip().lower() not in ("1", "true", "yes"):
                continue
            try:
                out.append({
                    "ts": datetime.strptime(r["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
                    if "T" in r["timestamp"] else
                    datetime.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S"),
                    "arm": arm,
                    "slices": int(r["slices"]),
                    "rps": float(r["source_out_rps"]),
                    "bp": float(r["backpressure_mean_ms_s"]),
                    "e2e": float(r["e2e_delay_ms"]),
                })
            except (ValueError, KeyError):
                continue
    return out


# ---------------------------------------------------------------------------
# rebuilding the instance
# ---------------------------------------------------------------------------
def parallelisms_for(slice_count, loads, pin_parallelism):
    """The (per-vertex parallelism) that produces exactly this many slices.

    PER_STAGE needs max(src, filters) + max(cpu_pin, p) + p + sink slots, with
    src = max(1, p//2) and sink = max(1, p//4) — GraphConfig.slotsRequired().
    The pin only holds until the adaptive scheduler rescales, so both the pinned
    and the unpinned width are candidates.
    """
    for p in range(1, 33):
        for pin in {pin_parallelism, p}:
            if pin <= 0:
                continue
            src = max(1, p // 2)
            filters = p
            snk = max(1, p // 4)
            groups = {"src": max(src, filters), "cpu": max(pin, p), "win": p, "snk": snk}
            if sum(groups.values()) == slice_count:
                return {"p": p, "pin": pin, "src": src, "filters": filters,
                        "snk": snk, "groups": groups}
    return None


def slice_loads(shape, loads, order):
    """Cost of every slice, indexed the way the fork indexes them.

    THE INDEXING IS INTERLEAVED, NOT CONCATENATED, and getting this wrong is the
    difference between a reconstruction that reproduces LPT's logged choices and
    one that inverts them. Slice indices run round by round across the sharing
    groups — round i emits subtask i of every group still that wide — so the
    expensive `cpu` slices land at indices 0 and 4 of a 7-slice job, not 2 and 3.
    Recovered 2026-09-02 by replaying LPT: only this layout explains why the fork
    put slice#0 on the fast machine and slice#4 on the medium one.

    The published figure is ms/s PER SUBTASK at the parallelism it was measured
    at; the fork uses it as-is at any other parallelism, so this does too — the
    reconstruction has to match the fork, not be more correct than it.
    """
    per_stage = defaultdict(list)
    for name, (par, cost) in loads.items():
        s = stage_of(name)
        if s:
            per_stage[s].append((name, cost))

    widths = {
        "src": {"Source:": shape["src"], "Filter": shape["filters"]},
        "cpu": {"CPU_Load": shape["pin"], "Latency_Tracker": shape["p"]},
        "win": {"Window": shape["p"]},
        "snk": {"Sink": shape["snk"]},
    }
    out = []
    for i in range(max(shape["groups"].values())):
        for stage in order:
            if i >= shape["groups"][stage]:
                continue
            contents = {}
            for name, cost in per_stage.get(stage, []):
                for prefix, par in widths[stage].items():
                    if name.startswith(prefix) and i < par:
                        contents[name] = cost
            out.append(contents)
    return out


def totals(composition):
    """Slice loads, the way the fork's balance term sees them."""
    return [max(sum(c.values()), 1e-6) for c in composition]


def lpt(loads, speeds, slots_per_tm):
    """The fork's LPT, replayed: heaviest slice first, onto the machine that
    would finish it soonest. ThesisSlotAssigner lines 632-676."""
    order = sorted(range(len(loads)), key=lambda g: (-loads[g], g))
    assigned = [0.0] * len(speeds)
    used = [0] * len(speeds)
    out = [None] * len(loads)
    for g in order:
        best, best_finish = None, None
        for t in range(len(speeds)):
            if used[t] >= slots_per_tm[t]:
                continue
            finish = (assigned[t] + loads[g]) / speeds[t]
            if best_finish is None or finish < best_finish:
                best, best_finish = t, finish
        if best is None:
            return None
        out[g] = best
        assigned[best] += loads[g]
        used[best] += 1
    return out


# ---------------------------------------------------------------------------
# the candidate objectives
# ---------------------------------------------------------------------------
def objectives(mapping, composition, speeds, idle_fraction=0.15):
    """Every candidate, scored on one placement. All are LOWER-is-better.

    The last two come from the auto-scaling literature rather than from the
    scheduling one, and they have a different SHAPE from the first four. A
    makespan is a max over MACHINES of summed work. DS2's model (Kalavri et al.,
    OSDI'18) says a streaming query's rate is set by the weakest OPERATOR, whose
    capacity is the SUM over its subtasks of what each can process — so the
    quantity that bounds throughput is a max over operators of a ratio whose
    denominator is a sum across machines. No rearrangement turns one into the
    other, which is a concrete reason the makespan could fail to predict
    throughput while still being a sensible objective.
    """
    loads = totals(composition)
    n_tm = len(speeds)
    load_t = [0.0] * n_tm
    count_t = [0] * n_tm
    for g, t in enumerate(mapping):
        load_t[t] += loads[g]
        count_t[t] += 1
    total = sum(loads)
    total_speed = sum(speeds)

    makespan = max(load_t[t] / speeds[t] for t in range(n_tm))

    sq = sum((load_t[t] - total * speeds[t] / total_speed) ** 2 for t in range(n_tm))
    mean_ideal = total / max(1, n_tm)
    dispersion = (sq / n_tm) ** 0.5 / max(mean_ideal, 1e-9)

    # Equal cut of the machine for every slice that sits on it.
    fair = max(loads[g] / (speeds[mapping[g]] / count_t[mapping[g]])
               for g in range(len(loads)))

    # Only slices that actually work take a cut; an idle co-tenant leaves its
    # cores free. "Idle" is a fraction of the heaviest slice in the job.
    threshold = idle_fraction * max(loads)
    busy_t = [0] * n_tm
    for g, t in enumerate(mapping):
        if loads[g] >= threshold:
            busy_t[t] += 1
    effective = max(loads[g] / (speeds[mapping[g]] / max(1, busy_t[mapping[g]]))
                    for g in range(len(loads)))

    # Cores a slice really commands: its machine's, split only among the
    # co-tenants that are actually working.
    eff = [speeds[mapping[g]] / max(1, busy_t[mapping[g]]) for g in range(len(loads))]

    # DS2's bottleneck. An operator's capacity is the sum over its subtasks;
    # the job's rate is the weakest operator's. Lower is better, like the rest.
    per_vertex_cost = {}
    per_vertex_cores = defaultdict(float)
    for g, contents in enumerate(composition):
        for vertex, cost in contents.items():
            per_vertex_cost[vertex] = cost
            per_vertex_cores[vertex] += eff[g]
    bottleneck = max((per_vertex_cost[v] / max(per_vertex_cores[v], 1e-9)
                      for v in per_vertex_cost if per_vertex_cost[v] > 0),
                     default=0.0)

    # CAPS's compute cost (Wang et al., EuroSys'25, Eqs. 4-7): the bottleneck
    # worker's load, normalised between a perfectly-balanced allocation and the
    # worst case of co-locating the `slots` most intensive tasks on one machine.
    #
    # TWO THINGS TO KNOW BEFORE READING ITS NUMBER. First, CAPS assumes
    # HOMOGENEOUS workers — L_min divides the total by the worker COUNT, with no
    # notion of capacity — so on this cluster it is deliberately blind to the
    # 4/2/1-core split, and that blindness is the point of testing it. Second,
    # within one instance L_min and L_max are constants, so C_cpu is an affine
    # function of the bottleneck load: its Spearman here is exactly that of an
    # unweighted max-load-per-machine, and reading it tells us what CAPS's
    # compute dimension alone can and cannot order.
    slots_per_worker = max(count_t) if count_t else 1
    l_now = max(load_t)
    l_min = sum(loads) / max(1, n_tm)
    l_max = sum(sorted(loads, reverse=True)[:max(1, slots_per_worker)])
    caps_cpu = 0.0 if l_max <= l_min else (l_now - l_min) / (l_max - l_min)

    # Queueing: end-to-end delay is governed by the single most utilised subtask,
    # not by the machine that carries the most work in total.
    worst_subtask = max((cost / max(eff[g], 1e-9)
                         for g, contents in enumerate(composition)
                         for cost in contents.values() if cost > 0),
                        default=0.0)

    return {"makespan": makespan, "dispersion": dispersion,
            "fair_share": fair, "effective_cores": effective,
            "ds2_bottleneck": bottleneck, "worst_subtask": worst_subtask,
            "caps_cpu": caps_cpu}


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = mean_rank
        i = j + 1
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / dx / dy if dx and dy else float("nan")


def spearman(xs, ys):
    return pearson(ranks(xs), ranks(ys))


# ---------------------------------------------------------------------------
def clock_offset(placements, eps, tolerance_s):
    """Hours to add to a JobManager timestamp to reach the driver's clock.

    The assigner logs from inside the JobManager pod, which runs UTC, while the
    episode CSV is written by the driver on the host, which does not. Nothing
    records the offset, so it is recovered the only way that cannot be fooled:
    the whole-hour shift that lets the most episodes find a placement logged
    just before them.
    """
    best, best_hits = 0, -1
    for hours in range(-14, 15):
        shift = timedelta(hours=hours)
        hits = 0
        for ep in eps:
            for a in placements:
                delay = (ep["ts"] - (a["ts"] + shift)).total_seconds()
                if a["slices"] == ep["slices"] and 0 <= delay <= tolerance_s:
                    hits += 1
                    break
        if hits > best_hits:
            best, best_hits = hours, hits
    return timedelta(hours=best), best_hits


def load_campaign(run_dir, tolerance_s):
    run_json = os.path.join(run_dir, "run.json")
    if not os.path.exists(run_json):
        return None
    cfg = json.load(open(run_json))
    if cfg.get("slot_sharing") != "PER_STAGE":
        return None
    speeds_by_name = {}
    for entry in cfg.get("taskmanager_speeds", []):
        name, speed = entry.split()
        speeds_by_name[name] = float(speed)
    if not speeds_by_name:
        return None
    pin = int(cfg.get("pin_parallelism") or 0)

    rows = []
    loads = {}
    for cell in sorted(glob.glob(os.path.join(run_dir, "*"))):
        if not os.path.isdir(cell):
            continue
        arm = os.path.basename(cell)
        loads = loads or published_loads(os.path.join(cell, "driver.log"))
        placements = assignments(os.path.join(cell, "thesis-assign.log"))
        eps = episodes(cell, arm)
        if not placements or not eps:
            continue
        shift, _ = clock_offset(placements, eps, tolerance_s)
        for a in placements:
            a["ts"] = a["ts"] + shift
        for ep in eps:
            # The placement in force is the last one this arm logged before it.
            candidates = [a for a in placements
                          if a["arm"] == arm and a["slices"] == ep["slices"]
                          and a["ts"] <= ep["ts"]
                          and (ep["ts"] - a["ts"]).total_seconds() <= tolerance_s]
            if not candidates:
                continue
            ep["placement"] = max(candidates, key=lambda a: a["ts"])
            rows.append(ep)
    if not loads or not rows:
        return None
    capacity = defaultdict(int)
    for cell in sorted(glob.glob(os.path.join(run_dir, "*"))):
        if not os.path.isdir(cell):
            continue
        for a in assignments(os.path.join(cell, "thesis-assign.log")):
            counts = defaultdict(int)
            for tm in a["mapping"]:
                counts[tm] += 1
            for tm, c in counts.items():
                capacity[tm] = max(capacity[tm], c)
    return {"cfg": cfg, "speeds": speeds_by_name, "pin": pin, "rows": rows,
            "loads": loads, "capacity": dict(capacity), "name": os.path.basename(run_dir)}


def best_ordering(campaign):
    """The stage ordering LPT's own logged choices agree with."""
    speeds_by_name = campaign["speeds"]
    tm_names = sorted(speeds_by_name)
    speeds = [speeds_by_name[n] for n in tm_names]
    full_pool = sum(campaign["capacity"].values())
    lpt_rows = [r for r in campaign["rows"]
                if r["placement"]["arm"] == "LPT"
                and r["placement"]["free_slots"] >= full_pool]
    if not lpt_rows:
        return None, 0.0, 0
    best, best_rate = None, -1.0
    for order in itertools.permutations(STAGES):
        agree = total = 0
        for r in lpt_rows:
            shape = parallelisms_for(r["slices"], campaign["loads"], campaign["pin"])
            if not shape:
                continue
            loads = totals(slice_loads(shape, campaign["loads"], order))
            mapping = r["placement"]["mapping"]
            slots = [campaign["capacity"].get(n, 0) for n in tm_names]
            replay = lpt(loads, speeds, slots)
            if replay is None:
                continue
            actual = [tm_names.index(m) for m in mapping]
            total += 1
            # Per-slice agreement rather than all-or-nothing: one tie broken the
            # other way should not read the same as a reconstruction that inverts
            # the instance. Slices of identical cost are interchangeable, so a
            # slice counts as agreeing when the machine it landed on holds the
            # same load it would have under the replay.
            hits = 0
            for g in range(len(loads)):
                if replay[g] == actual[g]:
                    hits += 1
                else:
                    same_cost = [h for h in range(len(loads))
                                 if abs(loads[h] - loads[g]) < 1e-6 and replay[h] == actual[g]]
                    if same_cost:
                        hits += 1
            agree += hits / len(loads)
        rate = agree / total if total else 0.0
        if rate > best_rate:
            best, best_rate, best_n = order, rate, total
    return best, best_rate, best_n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results/placement-experiment")
    ap.add_argument("--campaigns", nargs="*", default=None)
    ap.add_argument("--min-backpressure", type=float, default=0.0,
                    help="drop starved episodes, as the headline analysis does")
    ap.add_argument("--target", choices=["rps", "e2e"], default="rps",
                    help="what the objectives are asked to predict; e2e is negated "
                         "so that a NEGATIVE correlation always means 'predicts'")
    ap.add_argument("--min-stratum", type=int, default=8,
                    help="episodes a (campaign, slices) stratum needs to be scored")
    ap.add_argument("--tolerance-s", type=float, default=600,
                    help="how stale a logged placement may be for an episode")
    args = ap.parse_args()

    dirs = ([os.path.join(args.root, c) for c in args.campaigns] if args.campaigns
            else sorted(glob.glob(os.path.join(args.root, "*"))))

    # Keyed by (campaign, slices): pooling operating points is exactly the mistake
    # that produced the discredited "+33%" in the headline analysis, and a
    # correlation is no more immune to it than a mean.
    strata = defaultdict(lambda: {"objs": defaultdict(list), "rps": [], "arms": [],
                                  "placements": []})
    print(f"{'campaña':>20} {'eps':>5} {'orden de slices':>26} {'LPT reproducido':>16}")
    print("-" * 74)
    for run_dir in dirs:
        campaign = load_campaign(run_dir, args.tolerance_s)
        if not campaign:
            continue
        order, rate, n = best_ordering(campaign)
        if order is None:
            print(f"{campaign['name']:>20} {len(campaign['rows']):>5} {'sin brazo LPT':>26} {'—':>16}")
            continue
        print(f"{campaign['name']:>20} {len(campaign['rows']):>5} "
              f"{'/'.join(order):>26} {f'{100*rate:.0f}% de {n}':>16}")
        if rate < 0.85:
            continue  # reconstruction not trustworthy for this campaign
        tm_names = sorted(campaign["speeds"])
        speeds = [campaign["speeds"][n] for n in tm_names]
        for r in campaign["rows"]:
            if r["bp"] < args.min_backpressure:
                continue
            shape = parallelisms_for(r["slices"], campaign["loads"], campaign["pin"])
            if not shape:
                continue
            composition = slice_loads(shape, campaign["loads"], order)
            mapping = [tm_names.index(m) for m in r["placement"]["mapping"]]
            if len(mapping) != len(composition):
                continue
            key = (campaign["name"], r["slices"])
            for name, value in objectives(mapping, composition, speeds).items():
                strata[key]["objs"][name].append(value)
            strata[key]["rps"].append(-r["e2e"] if args.target == "e2e" else r["rps"])
            strata[key]["arms"].append(r["arm"])
            strata[key]["placements"].append(tuple(mapping))

    usable = {k: v for k, v in strata.items()
              if len(v["rps"]) >= args.min_stratum
              and len(set(v["arms"])) >= 2}
    if not usable:
        print("\nNada que ajustar: ningún estrato pasó la validación.")
        return

    names = sorted(next(iter(usable.values()))["objs"])
    what = "menos throughput" if args.target == "rps" else "más latencia e2e"
    print(f"\nSpearman POR ESTRATO (campaña x slices) contra {args.target}.")
    print(f"Negativo = el objetivo predice: peor puntaje, {what}.\n")
    print(f"{'objetivo':>18} {'mediana':>9} {'predicen':>10} {'invierten':>10} {'nulos':>7}")
    print("-" * 58)
    for name in names:
        rhos = [spearman(v["objs"][name], v["rps"]) for v in usable.values()]
        rhos = [r for r in rhos if r == r]
        predict = sum(1 for r in rhos if r < -0.3)
        invert = sum(1 for r in rhos if r > 0.3)
        print(f"{name:>18} {st.median(rhos):>9.3f} {predict:>10} {invert:>10} "
              f"{len(rhos) - predict - invert:>7}")
    # THE DIAGNOSTIC THAT DECIDES HOW TO READ THE TABLE. A correlation over
    # episodes that all ran the SAME placement measures repetition noise, not the
    # objective. Two or three distinct placements in a stratum means the null is
    # "underpowered", not "the objective is wrong".
    distinct = [len(set(v["placements"])) for v in usable.values()]
    print(f"\nEmplazamientos DISTINTOS por estrato: mediana {st.median(distinct):.0f}, "
          f"rango {min(distinct)}-{max(distinct)}")
    total = sum(len(v["rps"]) for v in usable.values())
    print(f"\n{len(usable)} estratos, {total} episodios"
          + (f", filtrados a bp >= {args.min_backpressure:.0f}" if args.min_backpressure else "")
          + f"; mínimo {args.min_stratum} episodios y 2 brazos por estrato.")


if __name__ == "__main__":
    main()
