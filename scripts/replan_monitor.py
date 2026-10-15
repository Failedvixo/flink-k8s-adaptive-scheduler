#!/usr/bin/env python3
"""Monitor (option A): decide WHEN to re-place operators, with the v4 table deciding WHERE.

WHY (2026-10-12, part B). Until now a plan reached the job only at a rescale. The fork can now
re-apply the plan at unchanged parallelism (ThesisReplanTrigger: one restart), so something has
to decide when that restart is worth it. This is the rule-based version, the reference a learned
monitor (a bandit over stay / move now / move at the next checkpoint) will have to beat.

Every INTERVAL seconds, over a WINDOW of measurement:

  1. OBSERVE what v4 observes — per-slice busy time and RocksDB writes, from which v4 builds its
     state — plus what tells a struggling machine apart: the kernel's CPU throttling per
     TaskManager (cgroup cpu.stat) and RocksDB write stalls per TaskManager; and, for the job,
     delivered over offered (the offered rate follows DIST).
  2. ASK v4 (frozen, greedy) which plan it would choose NOW. Same as the running one: stay.
  3. ESTIMATE THE GAIN of switching: the table's value of the proposed plan minus its value of
     the RUNNING plan, both evaluated along v4's own decision order, so each slice's value is read
     in the state it would actually be decided in (backing off exactly as v4 does on rows it has
     seen too little).
  4. MOVE only if all hold:
       * the gain clears --min-gain;
       * something is actually wrong: delivered/offered under --ratio-cut, or a machine throttled
         above --throttle-cut or stalled above --stall-cut — a better-looking plan for a job that
         is keeping up does not pay for a restart;
       * the SAME proposal came out of --confirm consecutive checks (one window is noise);
       * at least --cooldown seconds since the last restart (it has to settle and be measured);
       * it is not a return to a plan left less than --tabu seconds ago (no ping-pong).

Moving = publish the plan to /var/thesis/assignment and write a request token to
/var/thesis/replan ("now", or "checkpoint" with --when checkpoint).

--observe-only measures and logs exactly the same and never moves after the initial placement:
the "no monitor" arm, with identical observation overhead.

The first decision is applied unconditionally (--initial, on by default): the job reaches p=2 by a
rescale with no plan published (the fork falls back to LPT), so both arms start from v4's plan.
"""
import argparse
import csv
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import characterizer_agent as ca                               # noqa: E402
import agent_v5_loop as v5loop                                 # noqa: E402

REPLAN_FILE = "/var/thesis/replan"


# ---------------------------------------------------------------------------
# the table's view of a whole plan
# ---------------------------------------------------------------------------
def q_estimate(agent, state, action):
    """The value v4 would act on for (state, action): the row itself when seen often enough,
    otherwise the same back-off chain SliceSarsa.choose uses. None if nothing is known."""
    if agent.visits(state, action) >= agent.min_visits:
        return agent.value(state, action)
    keeps = ([("load", "disk", "heavy")] if "|heavy=" in state else []) + \
        [("load", "disk"), ("disk",)]
    for keep in keeps:
        pooled = agent._pooled(state, [action], keep=keep)
        if action in pooled:
            return pooled[action]
    return None


def plan_value(agent, slices, capacity, actions, busy_cut, disk, disk_cut=0.5,
               disk_floor=1e6, machine_load=True):
    """Sum of q_estimate along v4's decision order with the given actions ({slice: rank}).

    Mirrors characterizer_agent.decide step by step (order, room mask, heavy counts), only the
    action is imposed instead of chosen. Returns (value, unknown decisions) or (None, n) if the
    plan does not fit the slots."""
    costs = [c for c, _ in slices]
    peak = max(costs) if costs and max(costs) > 0 else 1.0
    disk_peak = max(disk) if disk else 0.0
    room = list(capacity)
    heavy_on = [0] * len(capacity)
    total, unknown = 0.0, 0
    for index in sorted(range(len(slices)), key=lambda i: (not slices[i][1], -costs[i], i)):
        action = actions[index]
        if room[action] <= 0:
            return None, unknown
        mask = "".join(ca.room_bucket(left, capacity[r]) for r, left in enumerate(room))
        heavy = slices[index][1] or costs[index] / peak >= busy_cut
        writes = disk_peak > disk_floor and disk[index] / disk_peak >= disk_cut
        state = f"load={'HIGH' if heavy else 'LOW'}|disk={'Y' if writes else 'N'}|free={mask}"
        if machine_load:
            state += "|heavy=" + "".join(str(min(2, h)) for h in heavy_on)
        q = q_estimate(agent, state, action)
        if q is None:
            unknown += 1
        else:
            total += q
        room[action] -= 1
        if heavy:
            heavy_on[action] += 1
    return total, unknown


def fewest_moves(proposed, running, kinds):
    """The proposal re-labelled so it moves as few slices as possible.

    Slices of the same kind (same operators) are interchangeable, so a proposal that only swaps
    two of them between machines is the running placement, and one that changes where one of two
    joins goes should move ONE join, not both. Per kind: every slice keeps its machine if the
    proposal still has that machine for this kind; the rest take what is left."""
    out = list(proposed)
    for kind in set(kinds):
        idx = [i for i in range(len(kinds)) if kinds[i] == kind]
        pool = [proposed[i] for i in idx]
        keep = {}
        for i in idx:
            if running[i] in pool:
                pool.remove(running[i])
                keep[i] = running[i]
        for i in idx:
            out[i] = keep[i] if i in keep else pool.pop(0)
    return out


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------
def observe(args, jid, detail, layout):
    """One window of measurements. None if the window crossed a restart."""
    base = args.rest
    tms = [tm for tm, _free in layout["tms"]]
    gen_ids = ca.generator_metric_ids(base, jid, detail)
    disk_ids = ca.disk_metric_ids(base, jid, detail, args.disk_suffix)
    stall_ids = ca.disk_metric_ids(base, jid, detail, ca.STALL_SUFFIX)
    key0 = ac.epoch_key(base, jid)
    disk0, stall0 = ca.disk_bytes(base, jid, detail, disk_ids), ca.disk_bytes(base, jid, detail, stall_ids)
    cg0 = v5loop.cgroup_cpu(args.node, args.namespace, tms)
    # SOURCE RATE FROM THE SAME METER THE CAMPAIGNS USE (2026-10-12). The first version used the
    # cumulative write-records counter: it refreshes every ~10 s and restarts at zero with every
    # replan, and Q5 at 3000 ev/s read 92% while the sources were emitting 2993/s. The campaigns'
    # source_out_rps is numRecordsOutPerSecond (a 60 s meter per subtask), summed here over the
    # source subtasks with the aggregated endpoint, and averaged over the window's samples.
    sources, _sinks = ac.graph_roles(detail)
    src_rates, t0 = [], time.time()
    samples = []
    step = args.window / max(1, args.samples)
    for _ in range(max(1, args.samples)):
        t_sample = time.time()
        samples.append(ca.subtask_loads(base, jid, detail, gen_ids))
        total, seen = 0.0, False
        for vid in sources:
            got = ac.rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/metrics"
                                f"?get=numRecordsOutPerSecond&agg=sum") or []
            for m in got:
                try:
                    total += float(m["sum"])
                    seen = True
                except (KeyError, TypeError, ValueError):
                    pass
        if seen:
            src_rates.append(total)
        time.sleep(max(0.0, step - (time.time() - t_sample)))
    t1 = time.time()
    if ac.epoch_key(base, jid) != key0 or not src_rates:
        return None
    dt = max(1e-6, t1 - t0)
    disk1, stall1 = ca.disk_bytes(base, jid, detail, disk_ids), ca.disk_bytes(base, jid, detail, stall_ids)
    cg1 = v5loop.cgroup_cpu(args.node, args.namespace, tms)

    loads = ca.operator_profile({k: statistics.median(s[k] for s in samples if k in s)
                                 for k in samples[-1]})
    per_slice = ca.slice_loads(layout, loads, args.saturation_cut)
    rates = ca.operator_profile({m: (disk1[m] - disk0[m]) / dt for m in disk1
                                 if m in disk0 and disk1[m] >= disk0[m]})
    per_slice_disk = [sum(rates.get(m, 0.0) for m in members) for members in layout["slices"]]

    where = {}
    for v in detail.get("vertices", []):
        for sub, tm in ac.subtask_taskmanagers(base, jid, v["id"]).items():
            where[(v["id"], sub)] = tm
    stall_task = {t: max(0.0, stall1[t] - stall0[t]) / 1e6 / dt for t in stall1 if t in stall0}
    machines = {}
    for tm in tms:
        tasks = [k for k, t in where.items() if t == tm]
        throttled = None
        if tm in cg0 and tm in cg1 and cg1[tm][1] > cg0[tm][1]:
            throttled = (cg1[tm][2] - cg0[tm][2]) / (cg1[tm][1] - cg0[tm][1])
        machines[tm] = {"throttled": throttled,
                        "stall": max((stall_task.get(k, 0.0) for k in tasks), default=0.0)}
    rps = statistics.fmean(src_rates)
    # The meter averages the last 60 s, so the offered rate is averaged over the same span —
    # shifted back by --offered-lag (2026-10-15): fitted on the unsaturated windows of three Q5 runs
    # (sine of 30 min), what the sources deliver matches the schedule 25-45 s EARLIER than the
    # monitor's clock says (rms error 7.5% -> 2.2% on the V4 run), so without the shift a job
    # that keeps up reads ~92% on a rising sine and >100% on a falling one.
    lag = getattr(args, "offered_lag", 0.0)
    offered = args.rate * v5loop.offered_multiplier(args.dist, args.rate, t0 - 60.0 - lag, t1 - lag)
    return {"rps": rps, "offered": offered, "ratio": min(1.0, rps / offered) if offered else 0.0,
            "per_slice": per_slice, "per_slice_disk": per_slice_disk, "where": where,
            "machines": machines, "t": t1}


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--jm-pod", default="")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--qtable", required=True, help="v4 table (frozen)")
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--dist", default="CONSTANT")
    ap.add_argument("--duration", type=float, default=3600.0, help="stop after this many seconds")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between checks")
    ap.add_argument("--window", type=float, default=45.0, help="measurement window per check")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--warmup", type=float, default=120.0, help="settle after a restart")
    ap.add_argument("--observe-only", action="store_true", help="never move after the initial plan")
    ap.add_argument("--no-initial", action="store_true", help="do not apply v4's plan at start")
    ap.add_argument("--when", choices=["now", "checkpoint"], default="now")
    ap.add_argument("--min-gain", type=float, default=0.05)
    ap.add_argument("--ratio-cut", type=float, default=0.90)
    ap.add_argument("--throttle-cut", type=float, default=0.20)
    ap.add_argument("--stall-cut", type=float, default=0.05)
    ap.add_argument("--confirm", type=int, default=2)
    ap.add_argument("--cooldown", type=float, default=300.0)
    ap.add_argument("--tabu", type=float, default=900.0)
    # v4's own settings, as trained (AGENT_ARGS_RL_V4 of the campaigns)
    ap.add_argument("--busy-cut", type=float, default=0.5)
    ap.add_argument("--saturation-cut", type=float, default=900.0)
    ap.add_argument("--disk-suffix", default=".rocksdb_compact_write_bytes")
    ap.add_argument("--disk-floor", type=float, default=1e6)
    ap.add_argument("--min-visits", type=int, default=3)
    ap.add_argument("--out-dir", default="results/replan-monitor")
    args = ap.parse_args()

    ac._JM_POD = args.jm_pod or None
    ac._NAMESPACE = args.namespace
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    agent = ca.SliceSarsa(0.0, 0.0, Path(args.qtable), args.min_visits, ucb=0.0)
    mode = "SOLO OBSERVA (sin monitor)" if args.observe_only else "MONITOR (regla)"
    print("=" * 64)
    print(f"  Monitor de replanificación — {mode}")
    print(f"  tabla v4: {args.qtable}   tasa {args.rate:.0f} ({args.dist})")
    print(f"  cada {args.interval:.0f}s, ventana {args.window:.0f}s; mover si ganancia >= "
          f"{args.min_gain} y algo anda mal, {args.confirm} veces seguidas, "
          f"enfriamiento {args.cooldown:.0f}s")
    print("=" * 64, flush=True)

    fields = ["time", "elapsed_s", "offered", "rps", "ratio", "machines", "running", "proposed",
              "moved", "gain", "unknown", "distress", "streak", "decision", "reason"]
    csv_path = out / f"monitor-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    started = time.time()
    last_restart, last_key = 0.0, None
    streak, last_proposal = 0, None
    left = {}                                   # plan signature -> when we moved away from it
    initial_done = args.no_initial
    moves = 0

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while time.time() - started < args.duration:
            cycle_start = time.time()
            jid = ac.running_job(args.rest)
            if not jid:
                time.sleep(5)
                continue
            key = ac.epoch_key(args.rest, jid)
            if key is None:
                time.sleep(5)
                continue
            if key != last_key:
                # A restart we did or did not cause (rescale, failure): it has to settle.
                last_key = key
                last_restart = max(last_restart, ac.epoch_started_at(key, time.time()))
            if time.time() - last_restart < args.warmup:
                time.sleep(5)
                continue
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            layout = ca.read_layout(args.node)
            if not detail or not layout:
                time.sleep(5)
                continue
            obs = observe(args, jid, detail, layout)
            if obs is None:
                print(f"  [{time.strftime('%H:%M:%S')}] ventana cruzó un reinicio — se descarta",
                      flush=True)
                continue

            speeds = ca.read_speeds(args.node)
            ranked = sorted(layout["tms"], key=lambda t: (-speeds.get(t[0], 1.0), t[0]))
            rank_of = {tm: r for r, (tm, _free) in enumerate(ranked)}
            capacity = [free for _tm, free in ranked]
            plan, _traj = ca.decide(agent, obs["per_slice"], capacity, 0.0, args.busy_cut,
                                    disk=obs["per_slice_disk"], machine_load=True,
                                    disk_floor=args.disk_floor)
            running = [next((obs["where"][m] for m in members if m in obs["where"]), None)
                       for members in layout["slices"]]
            if plan is None or None in running:
                time.sleep(5)
                continue
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            kinds = [", ".join(sorted({names.get(vid, vid) for vid, _ in members}))
                     for members in layout["slices"]]
            proposed = fewest_moves([ranked[plan[i]][0] for i in range(len(running))],
                                    running, kinds)
            plan = {i: rank_of[proposed[i]] for i in range(len(proposed))}
            moved = [i for i in range(len(running)) if proposed[i] != running[i]]
            gain, unknown = 0.0, 0
            if moved:
                v_new, u_new = plan_value(agent, obs["per_slice"], capacity, plan, args.busy_cut,
                                          obs["per_slice_disk"], disk_floor=args.disk_floor)
                v_cur, u_cur = plan_value(agent, obs["per_slice"], capacity,
                                          {i: rank_of[running[i]] for i in range(len(running))},
                                          args.busy_cut, obs["per_slice_disk"],
                                          disk_floor=args.disk_floor)
                gain = (v_new - v_cur) if None not in (v_new, v_cur) else math.inf
                unknown = u_new + u_cur

            sick = [tm for tm, m in obs["machines"].items()
                    if (m["throttled"] or 0.0) > args.throttle_cut or m["stall"] > args.stall_cut]
            distress = obs["ratio"] < args.ratio_cut or bool(sick)
            signature = tuple(proposed)
            streak = streak + 1 if (moved and signature == last_proposal) else (1 if moved else 0)
            last_proposal = signature if moved else None

            decision, reason = "quedarse", ""
            if not initial_done:
                decision, reason = ("mover", "plan inicial de v4") if moved else \
                    ("quedarse", "plan inicial ya aplicado")
                initial_done = True
            elif not moved:
                reason = "v4 elegiría el mismo plan"
            elif args.observe_only:
                reason = "solo observa"
            elif gain < args.min_gain:
                reason = f"ganancia {gain:+.3f} < {args.min_gain}"
            elif not distress:
                reason = "nada anda mal: no paga un reinicio"
            elif streak < args.confirm:
                reason = f"propuesta vista {streak}/{args.confirm} veces"
            elif time.time() - last_restart < args.cooldown:
                reason = f"enfriamiento ({time.time() - last_restart:.0f}s < {args.cooldown:.0f}s)"
            elif time.time() - left.get(signature, -math.inf) < args.tabu:
                reason = "volvería a un plan dejado hace poco (tabú)"
            else:
                decision, reason = "mover", f"ganancia {gain:+.3f}, " + (
                    f"máquinas con problemas: {', '.join(sick)}" if sick else
                    f"entrega {obs['ratio']:.0%}")

            if decision == "mover":
                plans = {len(running): {f"{vid}:{sub}": proposed[i]
                                        for i, members in enumerate(layout["slices"])
                                        for vid, sub in members}}
                ok, error = ca.publish_plan(plans, args.node)
                token = str(int(time.time() * 1000))
                if ok:
                    _, error = ca.node_command(args.node, ["sh", "-c",
                                               f"printf '%s %s\\n' {token} {args.when} > {REPLAN_FILE}"])
                if not ok or error:
                    decision, reason = "error", f"no se pudo pedir el replan: {error}"
                else:
                    left[tuple(running)] = time.time()
                    last_restart = time.time()      # settle from now; the key change confirms it
                    moves += 1
                    streak, last_proposal = 0, None

            short = lambda tms_: " ".join(t.split("-")[-1][:1] for t in tms_)   # noqa: E731
            mach = {tm.split("-")[-1]: {"thr": None if m["throttled"] is None
                                        else round(m["throttled"], 2),
                                        "stall": round(m["stall"], 3)}
                    for tm, m in obs["machines"].items()}
            print(f"  [{time.strftime('%H:%M:%S')}] pedido {obs['offered']:6.0f} entrega "
                  f"{obs['ratio']:4.0%}  actual [{short(running)}]"
                  f"{'  propone [' + short(proposed) + f'] ganancia {gain:+.3f}' if moved else ''}"
                  f"  -> {decision.upper()} ({reason})", flush=True)
            writer.writerow({"time": time.strftime("%H:%M:%S"),
                             "elapsed_s": round(time.time() - started),
                             "offered": round(obs["offered"]), "rps": round(obs["rps"]),
                             "ratio": round(obs["ratio"], 4), "machines": json.dumps(mach),
                             "running": " ".join(running), "proposed": " ".join(proposed),
                             "moved": len(moved), "gain": "" if not moved else round(gain, 4),
                             "unknown": unknown, "distress": distress, "streak": streak,
                             "decision": decision, "reason": reason})
            handle.flush()
            time.sleep(max(0.0, args.interval - (time.time() - cycle_start)))

    print(f"\nfin: {moves} replanificaciones; registro en {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
