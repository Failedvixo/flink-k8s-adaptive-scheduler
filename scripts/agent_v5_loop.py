#!/usr/bin/env python3
"""Run the v5 placement agent against the live job: measure, learn, decide, publish.

The decision model is scripts/agent_v5.py (linear value per (slice, machine) pair, learned
machine capacities, whole-plan search). This file is the loop around it: the same epochs and
measurement windows as characterizer_agent.py — whose helpers it reuses — with three differences
of substance.

1. EVERY MEASURED EPOCH IS A LEARNING SAMPLE, WHOEVER DECIDED IT. The features are computed for
   the placement that actually RAN (where each task was, from the REST API) under the loads
   measured in that same window, and paired with that window's outcome. The first repetition of
   every job, which runs LPT's fallback because no plan exists yet, used to be thrown away; here
   it teaches like any other. It also removes the bookkeeping that tied a credit to "the plan
   published before this epoch for this width", which is what mixed geometries used to break.

2. THE REWARD IS DELIVERED OVER OFFERED, not throughput over a running baseline. The agent knows
   the rate profile (DIST and RATE, the same arguments the job got), so it knows what the sources
   were asked for in the window; a plan that sustains the load scores 0 and one that delivers
   80% of it scores -0.2, at any rate. That is what makes training under a VARYING rate possible.

3. EACH DECISION ALSO PAYS FOR ITS MACHINE. Half of a slice's reward is how the machine it ran on
   coped: the share of the window its tasks spent stalled on RocksDB, and how far its tasks'
   busy time exceeded the CPU the machine actually gave them (contention). Both are measured per
   TaskManager, so a slice is not blamed for a jam on a machine it was not on.

Saturated slices: a task at its ceiling reports ~1 core whatever it needs. --estimate-saturated
takes its demand as measured x (offered / delivered); off by default, because once plans are
valued per machine the simulation reached the optimum without it and was slightly worse with it.

Usage (from the driver, later): python3 scripts/agent_v5_loop.py --jm-pod <pod> --rate 40000
    --dist CONSTANT --model results/characterizer/v5-model.json --out-dir <cell>
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
import agent_v5 as v5                                          # noqa: E402

ORIGIN_MS = 1_767_225_600_000      # NexmarkEpochs.Schedule.ORIGIN_MS: the profile's fixed origin


def offered_multiplier(dist, rate, t0, t1):
    """Mean of (offered rate / RATE) over [t0, t1] (epoch seconds), mirroring the sources."""
    if not dist or dist.upper() == "CONSTANT":
        return 1.0
    kind, step_s, rates = dist.split(":")
    if kind.upper() == "SINE":
        # Same construction as NexmarkEpochs.Schedule.sine: one-second steps at the mid-second value.
        lo, hi = (float(r) for r in rates.split(","))
        n = int(step_s)
        step_ms = 1000
        levels = [(lo + hi) / 2 + (hi - lo) / 2 * math.sin(2 * math.pi * (k + 0.5) / n)
                  for k in range(n)]
    else:
        step_ms, levels = int(step_s) * 1000, [int(r) for r in rates.split(",")]
    samples = []
    t = t0
    while t <= t1:
        k = int(((t * 1000 - ORIGIN_MS) // step_ms) % len(levels))
        samples.append(levels[k] / rate)
        t += 1.0
    return statistics.fmean(samples) if samples else 1.0


GENERATOR_CPU_SUFFIX = "generatorCpuMsPerSecond"


def task_counters(base, jid, detail, suffix):
    """{(vertexId, subtask): cumulative value} for the operator-scope RocksDB counter `suffix`."""
    return ca.disk_bytes(base, jid, detail, ca.disk_metric_ids(base, jid, detail, suffix))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--jm-pod", default="")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--warmup", type=float, default=180.0)
    ap.add_argument("--window", type=float, default=240.0)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--saturation-cut", type=float, default=900.0)
    ap.add_argument("--only-parallelism", default=None)
    ap.add_argument("--rate", type=float, required=True, help="RATE given to the job")
    ap.add_argument("--dist", default="CONSTANT", help="DIST given to the job")
    ap.add_argument("--model", required=True, help="JSON with the regression and machine model")
    ap.add_argument("--freeze", action="store_true", help="evaluate: greedy, no updates")
    ap.add_argument("--alpha", type=float, default=0.5, help="LinUCB exploration bonus")
    ap.add_argument("--local-weight", type=float, default=0.5,
                    help="share of each decision's reward that comes from its own machine")
    ap.add_argument("--latency-weight", type=float, default=0.0,
                    help="penalty per unit of (p50 latency / its running mean - 1)")
    ap.add_argument("--contention-cut", type=float, default=1.2,
                    help="tasks' busy time over the CPU their machine gave them, above which the "
                         "machine counts as CPU-contended")
    ap.add_argument("--stall-cut", type=float, default=0.05,
                    help="share of the window stalled on RocksDB above which a machine's disk "
                         "counts as saturated")
    ap.add_argument("--estimate-saturated", action="store_true",
                    help="take a saturated slice's demand as measured x offered/delivered (off: "
                         "with per-machine values the simulation found it unnecessary and "
                         "slightly worse on Q3)")
    ap.add_argument("--out-dir", default="results/characterizer")
    ap.add_argument("--missing-tolerance", type=int, default=4)
    args = ap.parse_args()

    ac._JM_POD = args.jm_pod or None
    ac._NAMESPACE = args.namespace
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_path = Path(args.model)
    stored = json.loads(model_path.read_text()) if model_path.exists() else {}
    agent = v5.LinUCBPlacer(alpha=0.0 if args.freeze else args.alpha)
    if "regression" in stored:
        agent.A = v5.np.array(stored["regression"]["A"])
        agent.b = v5.np.array(stored["regression"]["b"])
        agent.n = stored["regression"].get("n", 0)
    machines = v5.MachineModel(stored.get("machines"))

    def save():
        if args.freeze:
            return
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(json.dumps({
            "features": v5.FEATURES,
            "regression": {"A": agent.A.tolist(), "b": agent.b.tolist(), "n": agent.n},
            "weights": dict(zip(v5.FEATURES, agent.theta().round(4).tolist())),
            "machines": machines.to_json(),
        }, indent=2))

    print("=" * 60)
    print(f"  Agente v5 — {'EVALUACION (congelado)' if args.freeze else 'ENTRENAMIENTO'}")
    print(f"  modelo: {model_path}   tasa {args.rate:.0f} ({args.dist})")
    print("=" * 60, flush=True)

    measured_widths = ({int(x) for x in str(args.only_parallelism).replace(",", " ").split()}
                       if args.only_parallelism else set())
    csv_path = out / f"agent-v5-episodes-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    fields = ["epoch", "time", "slices", "rps", "offered", "delivered_ratio", "latency_p50_ms",
              "reward", "placement_ran", "plan_next", "machines"]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        epoch, current_key, epoch_started, done = 0, None, 0.0, True
        missing, latency_base = 0, None
        plans = {}
        while True:
            jid = ac.running_job(args.rest)
            if jid is None:
                missing += 1
                if missing >= args.missing_tolerance:
                    current_key, done = None, True
                time.sleep(args.poll_interval)
                continue
            missing = 0
            key = ac.epoch_key(args.rest, jid)
            if key is not None and key != current_key:
                current_key, epoch_started = key, ac.epoch_started_at(key, time.time())
                epoch += 1
                width = max((par for _, par, _ in key), default=0)
                done = bool(measured_widths and width not in measured_widths)
                print(f"\n[epoca {epoch}] paralelismo {width}"
                      + (" — se omite" if done else f", calentando {args.warmup:.0f}s"),
                      flush=True)
            if done or time.time() - epoch_started < args.warmup:
                time.sleep(args.poll_interval)
                continue
            done = True

            layout = ca.read_layout(args.node)
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            if not layout or not detail:
                print("  (sin layout o sin detalle — epoca descartada)", flush=True)
                continue
            tms = [tm for tm, _free in layout["tms"]]
            # Sources: the CPU their thread burned, not the wall time of their emit loop, which
            # reads 1000 ms/s whenever backpressure blocks collect() (2026-10-11 test: auction
            # sources "saturated" on a machine giving them 0.1 cores). Older jars lack the gauge.
            gen_ids = (ca.generator_metric_ids(args.rest, jid, detail, GENERATOR_CPU_SUFFIX)
                       or ca.generator_metric_ids(args.rest, jid, detail))
            comp0 = task_counters(args.rest, jid, detail, ".rocksdb_compact_write_bytes")
            stall0 = task_counters(args.rest, jid, detail, ca.STALL_SUFFIX)
            cpu0 = ac.taskmanager_cpu_seconds(args.rest, tms)
            rec0, t0 = ac.source_records(args.rest, jid), time.time()
            samples = []
            for _ in range(max(1, args.samples)):
                samples.append(ca.subtask_loads(args.rest, jid, detail, gen_ids))
                time.sleep(args.window / max(1, args.samples))
            rec1, t1 = ac.source_records(args.rest, jid), time.time()
            if ac.epoch_key(args.rest, jid) != current_key or None in (rec0, rec1) or rec1 < rec0:
                print("  (la ventana cruzó un reescalado — epoca descartada)", flush=True)
                continue
            dt = max(1e-6, t1 - t0)
            comp1 = task_counters(args.rest, jid, detail, ".rocksdb_compact_write_bytes")
            stall1 = task_counters(args.rest, jid, detail, ca.STALL_SUFFIX)
            cpu1 = ac.taskmanager_cpu_seconds(args.rest, tms)

            rps = (rec1 - rec0) / dt
            offered = args.rate * offered_multiplier(args.dist, args.rate, t0, t1)
            ratio = min(1.0, rps / offered) if offered > 0 else 0.0

            # Per task: busy (ms/s), compaction (MB/s), stall share, and the machine it ran on.
            busy = {k: statistics.median(s[k] for s in samples if k in s) for k in samples[-1]}
            busy_op = ca.operator_profile(busy)
            comp_task = {k: max(0.0, comp1[k] - comp0[k]) / dt / 1e6 for k in comp1 if k in comp0}
            comp = ca.operator_profile(comp_task)
            stall = {k: max(0.0, stall1[k] - stall0[k]) / 1e6 / dt for k in stall1 if k in stall0}
            where = {}
            for v in detail.get("vertices", []):
                for sub, tm in ac.subtask_taskmanagers(args.rest, jid, v["id"]).items():
                    where[(v["id"], sub)] = tm

            slices, slice_tm = [], []
            for members in layout["slices"]:
                cost = sum(busy_op.get(m, 0.0) for m in members)
                saturated = any(busy.get(m, 0.0) >= args.saturation_cut for m in members)
                demand = cost / 1000.0
                if args.estimate_saturated and saturated and ratio > 0:
                    demand /= ratio          # the bottleneck needs what it shows x offered/delivered
                slices.append({"cpu": demand, "saturated": saturated,
                               "disk": sum(comp.get(m, 0.0) for m in members)})
                slice_tm.append(next((where[m] for m in members if m in where), None))

            # What each machine went through in the window: telemetry for the machine model and
            # the local term of the reward.
            local, tm_state = {}, {}
            for tm in tms:
                cpu_used = max(0.0, cpu1.get(tm, 0.0) - cpu0.get(tm, 0.0)) / dt
                tasks = [k for k, t in where.items() if t == tm]
                busy_sum = sum(busy.get(k, 0.0) for k in tasks) / 1000.0
                contention = busy_sum / cpu_used if cpu_used > 0.05 else 0.0
                # What THIS machine wrote: per task, not the operator profile, which gives every
                # subtask its operator's peak and counted the join's 9.7 MB/s on two machines.
                disk_w = sum(comp_task.get(k, 0.0) for k in tasks)
                stalled = max((stall.get(k, 0.0) for k in tasks), default=0.0)
                # Busy time includes waiting on RocksDB: a machine stalled on its disk looks
                # CPU-contended without being so, and fast was learned as 1.7 cores (it has 4).
                contended = contention > args.contention_cut and stalled <= args.stall_cut
                machines.observe(tm, cpu_used, contended, disk_w, stalled > args.stall_cut)
                local[tm] = -min(1.0, stalled + max(0.0, contention - args.contention_cut))
                tm_state[tm] = {"cpu": round(cpu_used, 2), "contention": round(contention, 2),
                                "disk": round(disk_w, 2), "stall": round(stalled, 3)}

            latency = ca.sink_record_latency_ms(args.rest, jid, detail)
            reward = ratio - 1.0
            if args.latency_weight and latency:
                if latency_base:
                    reward -= args.latency_weight * (latency / latency_base - 1.0)
                latency_base = latency if not latency_base else 0.8 * latency_base + 0.2 * latency

            # LEARN FROM WHAT RAN: features of the realised placement under the measured loads.
            free = dict(layout["tms"])
            # The fork's slices file gives each machine's slots available to this job's plan —
            # the capacity characterizer_agent.py uses too — not what is left after placing.
            seen = [machines.view(tm, free[tm]) for tm in tms]
            index = {tm: i for i, tm in enumerate(tms)}
            ran = {i: index[tm] for i, tm in enumerate(slice_tm) if tm in index}
            if not args.freeze and len(ran) == len(slices):
                # One sample per MACHINE the placement used (see agent_v5.FEATURES).
                for x, m in v5.plan_features(slices, seen, ran):
                    r = (1 - args.local_weight) * reward + args.local_weight * local[tms[m]]
                    agent.update(x, r)
                save()

            # DECIDE THE NEXT PLAN with what is now known about loads and machines.
            capacity = [machines.view(tm, free[tm]) for tm in tms]
            plan, _traj = v5.decide(agent, slices, capacity, explore=not args.freeze)
            if plan is not None:
                plans[len(slices)] = {f"{vid}:{sub}": tms[plan[i]]
                                      for i, members in enumerate(layout["slices"])
                                      for vid, sub in members}
                ok, error = ca.publish_plan(plans, args.node)
                if not ok:
                    print(f"  ! no se pudo publicar el plan — {error}", flush=True)
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            with (out / f"v5-placement-epoch{epoch:03d}.txt").open("w") as trace:
                for i, members in enumerate(layout["slices"]):
                    stages = sorted({names.get(vid, vid[:8]) for vid, _ in members})
                    nxt = tms[plan[i]] if plan else "-"
                    trace.write(f"slice {i} ran-on {slice_tm[i]} -> next {nxt}  "
                                f"cpu={slices[i]['cpu']:.2f}{' SAT' if slices[i]['saturated'] else ''}"
                                f"  disco={slices[i]['disk']:.2f}MB/s  {', '.join(stages)}\n")
            print(f"  slices={len(slices)} rps={rps:.0f} pedido={offered:.0f} "
                  f"({ratio:.0%}) lat={latency} reward={reward:+.3f}", flush=True)
            for tm in tms:
                v = machines.view(tm, 0)
                print(f"    {tm:14} {tm_state[tm]}  capacidad estimada: {v['cores']:.1f} núcleos, "
                      f"disco {('%.1f MB/s' % v['disk_mbps']) if v['disk_mbps'] else 'sin tope visto'}",
                      flush=True)
            writer.writerow({
                "epoch": epoch, "time": time.strftime("%H:%M:%S"), "slices": len(slices),
                "rps": round(rps, 1), "offered": round(offered, 1),
                "delivered_ratio": round(ratio, 4), "latency_p50_ms": latency,
                "reward": round(reward, 4),
                "placement_ran": json.dumps({i: t for i, t in enumerate(slice_tm)}),
                "plan_next": json.dumps({i: tms[m] for i, m in (plan or {}).items()}),
                "machines": json.dumps(tm_state)})
            handle.flush()


if __name__ == "__main__":
    sys.exit(main())
