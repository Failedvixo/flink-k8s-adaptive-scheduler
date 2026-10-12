#!/usr/bin/env python3
"""Ask the fork to re-apply the placement plan at unchanged parallelism, and measure what it cost.

WHY (2026-10-12, part B). The fork can now restart a running job WITHOUT a rescale and apply
whatever plan /var/thesis/assignment holds (ThesisReplanTrigger in the fork, polled from the
Executing state). Before an agent decides WHEN to move, two facts are needed:

  1. that the trigger works on the cluster: one restart, the plan applied, only the slices the plan
     moves change machine;
  2. what a move costs: how long the sources stop, how long until they are back to the rate they
     had, and how many events that deficit amounts to. That is the price a "move" has to beat.

Works on the RUNNING job (submit one first, e.g. KEEP=1 scripts/profile-job.sh, with the RL arm
published so the fork applies plans). Publishes a plan built from the CURRENT placement with the
requested changes, writes a request token, and samples the sources' output once a second.

Usage:
  scripts/replan-test.py --same                       # re-apply the same placement: pure restart
  scripts/replan-test.py --move new-users-join=tm-1-slow
  scripts/replan-test.py --move new-users-join:1=tm-2-medium --when checkpoint
"""
import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac                                    # noqa: E402
import characterizer_agent as ca                               # noqa: E402

REPLAN_FILE = "/var/thesis/replan"
ARM_FILE = "/var/thesis/arm"


def jm_pod(namespace):
    return subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace, "-l", "component=jobmanager",
         "--field-selector=status.phase=Running", "-o", "jsonpath={.items[-1:].metadata.name}"],
        capture_output=True, text=True, timeout=30).stdout.strip()


def placement(base, jid, detail):
    """{(vertexId, subtask): taskmanager} for every subtask, and {vertexId: name}."""
    where, names = {}, {}
    for v in detail.get("vertices", []):
        names[v["id"]] = v.get("name", "")
        for sub, tm in ac.subtask_taskmanagers(base, jid, v["id"]).items():
            where[(v["id"], sub)] = tm
    return where, names


def apply_moves(where, names, moves, tms):
    """Copy of `where` with each "<name substring>[:subtask]=<tm>" applied."""
    plan = dict(where)
    for move in moves:
        target, tm = move.split("=", 1)
        name, _, sub = target.partition(":")
        if tm not in tms:
            sys.exit(f"ERROR: {tm} no es un TaskManager del clúster ({', '.join(tms)})")
        hits = [k for k in plan if name in names[k[0]] and (sub == "" or k[1] == int(sub))]
        if not hits:
            sys.exit(f"ERROR: ningún operador coincide con '{target}'")
        for k in hits:
            plan[k] = tm
    return plan


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--same", action="store_true", help="re-apply the current placement")
    group.add_argument("--move", action="append", default=[],
                       help="NAME[:SUBTASK]=TM, e.g. new-users-join=tm-1-slow (repeatable)")
    ap.add_argument("--when", choices=["now", "checkpoint"], default="now")
    ap.add_argument("--before", type=float, default=90.0, help="seconds of baseline before")
    ap.add_argument("--after", type=float, default=600.0, help="give up after this many seconds")
    ap.add_argument("--recovered", type=float, default=0.95,
                    help="share of the baseline rate that counts as recovered")
    ap.add_argument("--hold", type=float, default=30.0,
                    help="seconds the rate must stay recovered")
    ap.add_argument("--expect-parallelism", type=int, default=2,
                    help="refuse to run unless the job is at this parallelism (0: any)")
    ap.add_argument("--horizon", type=float, default=300.0,
                    help="seconds after the request over which the deficit is counted")
    args = ap.parse_args()

    ac._NAMESPACE = args.namespace
    ac._JM_POD = jm_pod(args.namespace) or None
    base = ac.DEFAULT_REST
    jid = ac.running_job(base)
    if not jid:
        sys.exit("ERROR: no hay job RUNNING")
    detail = ac.rest(base, f"/jobs/{jid}")
    layout = ca.read_layout(args.node)
    if not detail or not layout:
        sys.exit("ERROR: sin detalle del job o sin /var/thesis/slices")
    width = max((int(v.get("parallelism", 0)) for v in detail.get("vertices", [])), default=0)
    if args.expect_parallelism and width != args.expect_parallelism:
        sys.exit(f"ERROR: el job está a paralelismo {width}, no {args.expect_parallelism}: "
                 "profile-job.sh todavía no termina su reescalado. Espera y vuelve a intentar.")
    tms = [tm for tm, _free in layout["tms"]]
    where, names = placement(base, jid, detail)
    plan = where if args.same else apply_moves(where, names, args.move, tms)
    moved = sorted(k for k in plan if plan[k] != where.get(k))

    print(f"job {jid}, {len(layout['slices'])} slices, TaskManagers: {', '.join(tms)}")
    for k in moved:
        print(f"  mover {names[k[0]]}[{k[1]}]: {where[k]} -> {plan[k]}")
    if not moved:
        print("  (mismo emplazamiento: mide solo el costo del reinicio)")

    # The fork applies plans only on the RL arm; and the plan is keyed by task, per slice count.
    _, err = ca.node_command(args.node, ["sh", "-c", f"printf RL > {ARM_FILE}"])
    ok, err = ca.publish_plan({len(layout["slices"]): {f"{vid}:{sub}": tm
                                                       for (vid, sub), tm in plan.items()}},
                              args.node)
    if not ok:
        sys.exit(f"ERROR al publicar el plan: {err}")

    # MEASURED FROM THE JOB'S OWN CLOCK AND COUNTERS (2026-10-12, first run). The REST metrics
    # behind write-records refresh about every 10 s, so per-second differences are zeros and
    # spikes — the first version's baseline came out as 0 ev/s. Rates are taken between counter
    # CHANGES instead, and the restart's timing from the JobManager's timestamps.
    def snapshot():
        d = ac.rest(base, f"/jobs/{jid}")
        if not d:
            return None
        sources, _ = ac.graph_roles(d)
        total = 0.0
        for v in d.get("vertices", []):
            if v["id"] in sources:
                try:
                    total += float((v.get("metrics") or {}).get("write-records") or 0)
                except (TypeError, ValueError):
                    pass
        return {"now": d.get("now"), "status": d.get("state"),
                "all_running": all(v.get("status") == "RUNNING" for v in d.get("vertices", [])),
                "started": max((v.get("start-time", 0) for v in d.get("vertices", [])), default=0),
                "running_ts": (d.get("timestamps") or {}).get("RUNNING", 0),
                "records": total, "t": time.time()}

    first = snapshot()
    if not first or not first["all_running"] or first["now"] - first["started"] < 60_000:
        sys.exit("ERROR: el job no está estable (reinició hace menos de 60 s o tiene tareas sin "
                 "correr). Espera a que profile-job.sh termine y vuelve a intentar.")
    print(f"línea base {args.before:.0f} s ...", flush=True)
    changes = [first]
    end = time.time() + args.before
    while time.time() < end:
        time.sleep(1.0)
        snap = snapshot()
        if snap and snap["started"] != first["started"]:
            sys.exit("ERROR: el job se reinició durante la línea base (¿un reescalado en curso?)")
        if snap and snap["records"] != changes[-1]["records"]:
            changes.append(snap)
    if len(changes) < 3:
        sys.exit("ERROR: el contador de las fuentes casi no cambió en la línea base")
    baseline = (changes[-1]["records"] - changes[1]["records"]) / max(
        1e-6, changes[-1]["t"] - changes[1]["t"])
    print(f"  tasa base {baseline:.0f} ev/s (entre {len(changes) - 1} actualizaciones del contador)")

    token = str(int(time.time() * 1000))
    before = snapshot()
    _, err = ca.node_command(args.node, ["sh", "-c",
                                         f"printf '%s %s\\n' {token} {args.when} > {REPLAN_FILE}"])
    if err:
        sys.exit(f"ERROR al escribir {REPLAN_FILE}: {err}")
    t0 = time.time()
    jm_t0 = before["now"] + (t0 - before["t"]) * 1000.0     # the request, on the JobManager's clock
    print(f"pedido de replan {token} ({args.when}) a las {time.strftime('%H:%M:%S')}", flush=True)

    last_old = before["records"]            # counter of the attempt that was running
    new_attempt = None                      # first snapshot of the restarted attempt
    up_at = None                            # JM time all tasks of the new attempt were running
    prev, rates, recovered_at = None, [], None
    while time.time() - t0 < args.after:
        time.sleep(1.0)
        snap = snapshot()
        if not snap:
            continue
        if new_attempt is None:
            if snap["started"] != before["started"]:
                new_attempt = snap
                print(f"  t+{snap['t'] - t0:5.0f}s  reiniciado (grafo nuevo en RUNNING a "
                      f"t+{(snap['running_ts'] - jm_t0) / 1000:.1f}s)", flush=True)
            else:
                last_old = max(last_old, snap["records"])
                continue
        if up_at is None and snap["all_running"]:
            up_at = snap["started"]
            print(f"  t+{snap['t'] - t0:5.0f}s  todas las tareas corriendo (desplegadas a "
                  f"t+{(up_at - jm_t0) / 1000:.1f}s)", flush=True)
        if prev is None or snap["records"] != prev["records"]:
            if prev is not None:
                rate = (snap["records"] - prev["records"]) / max(1e-6, snap["t"] - prev["t"])
                rates.append((snap["t"], rate))
                print(f"  t+{snap['t'] - t0:5.0f}s  {rate:8.0f} ev/s  ({rate / baseline:4.0%} de la base)",
                      flush=True)
                recent = [r for t, r in rates if t >= snap["t"] - args.hold]
                if (recovered_at is None and up_at and len(recent) >= 2
                        and min(recent) >= args.recovered * baseline):
                    recovered_at = rates[-len(recent)][0]
            prev = snap
        if recovered_at and time.time() - t0 >= args.horizon:
            break
    # What the sources delivered over the horizon against what the baseline would have:
    # the old attempt up to its last reading, plus the new attempt from zero.
    horizon = min(args.horizon, time.time() - t0)
    delivered = max(0.0, last_old - before["records"]) + (prev["records"] if prev else 0.0)
    deficit = baseline * horizon - delivered
    restarted_at = new_attempt["t"] if new_attempt else None
    resumed_at = rates[0][0] if rates else None
    detail = ac.rest(base, f"/jobs/{jid}") or detail
    after, _ = placement(base, jid, detail)
    changed = sorted(k for k in after if after[k] != where.get(k))
    wrong = sorted(k for k in plan if after.get(k) != plan[k])
    print("\nRESULTADO")
    if not restarted_at:
        print("  ! no se vio reinicio: ¿fork sin el disparador, o brazo distinto de RL?")
    else:
        print(f"  reinicio visto a t+{restarted_at - t0:.0f} s (sondeo del JobManager cada 2 s"
              f"{'; esperó al checkpoint' if args.when == 'checkpoint' else ''})")
        if up_at:
            print(f"  tareas desplegadas de nuevo a t+{(up_at - jm_t0) / 1000:.1f} s (reloj del JobManager)")
        if resumed_at:
            print(f"  primera actualización con eventos a t+{resumed_at - t0:.0f} s "
                  f"(resolución ~10 s: así refresca Flink las métricas)")
    print(f"  recuperado (>= {args.recovered:.0%} de la base durante {args.hold:.0f} s) desde t+"
          f"{recovered_at - t0:.0f} s" if recovered_at else "  ! no se recuperó en la ventana")
    print(f"  déficit en {horizon:.0f} s: {deficit:.0f} eventos "
          f"(= {deficit / baseline:.1f} s a la tasa base; ±10 s de resolución)")
    print(f"  cambiaron de máquina: {len(changed)} subtareas "
          f"({', '.join(f'{names[k[0]]}[{k[1]}]' for k in changed) or 'ninguna'})")
    print(f"  plan aplicado: {'sí' if not wrong else 'NO — ' + str(len(wrong)) + ' fuera de plan'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
