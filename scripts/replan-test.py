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
    ap.add_argument("--before", type=float, default=60.0, help="seconds of baseline before")
    ap.add_argument("--after", type=float, default=600.0, help="give up after this many seconds")
    ap.add_argument("--recovered", type=float, default=0.95,
                    help="share of the baseline rate that counts as recovered")
    ap.add_argument("--hold", type=float, default=30.0,
                    help="seconds the rate must stay recovered")
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

    print(f"línea base {args.before:.0f} s ...", flush=True)
    rates, last, last_t = [], ac.source_records(base, jid), time.time()
    end = time.time() + args.before
    while time.time() < end:
        time.sleep(1.0)
        now, t = ac.source_records(base, jid), time.time()
        if None not in (now, last) and now >= last:
            rates.append((now - last) / (t - last_t))
        last, last_t = now, t
    baseline = statistics.median(rates) if rates else 0.0
    print(f"  tasa base {baseline:.0f} ev/s")

    key0 = ac.epoch_key(base, jid)
    token = str(int(time.time() * 1000))
    _, err = ca.node_command(args.node, ["sh", "-c",
                                         f"printf '%s %s\\n' {token} {args.when} > {REPLAN_FILE}"])
    if err:
        sys.exit(f"ERROR al escribir {REPLAN_FILE}: {err}")
    t0 = time.time()
    print(f"pedido de replan {token} ({args.when}) a las {time.strftime('%H:%M:%S')}", flush=True)

    restarted_at = resumed_at = recovered_at = None
    deficit, good_since = 0.0, None
    last, last_t = ac.source_records(base, jid), time.time()
    while time.time() - t0 < args.after:
        time.sleep(1.0)
        now, t = ac.source_records(base, jid), time.time()
        rate = 0.0
        if None not in (now, last) and now >= last:
            rate = (now - last) / (t - last_t)
        last, last_t = now, t
        deficit += max(0.0, baseline - rate) * 1.0
        key = ac.epoch_key(base, jid)
        if restarted_at is None and key is not None and key != key0:
            restarted_at = t
        if restarted_at and resumed_at is None and rate > 0.1 * baseline:
            resumed_at = t
        if resumed_at and rate >= args.recovered * baseline:
            good_since = good_since or t
            if t - good_since >= args.hold:
                recovered_at = good_since
                break
        else:
            good_since = None
        print(f"  t+{t - t0:5.0f}s  {rate:8.0f} ev/s{'  (reiniciado)' if restarted_at else ''}",
              flush=True)

    detail = ac.rest(base, f"/jobs/{jid}") or detail
    after, _ = placement(base, jid, detail)
    changed = sorted(k for k in after if after[k] != where.get(k))
    wrong = sorted(k for k in plan if after.get(k) != plan[k])
    print("\nRESULTADO")
    print(f"  reinicio visto a los {restarted_at - t0:.0f} s" if restarted_at else
          "  ! no se vio reinicio: ¿fork sin el disparador, o brazo distinto de RL?")
    if resumed_at:
        print(f"  fuentes detenidas hasta t+{resumed_at - t0:.0f} s")
    print(f"  recuperado (>= {args.recovered:.0%} de la base por {args.hold:.0f} s) a t+"
          f"{recovered_at - t0:.0f} s" if recovered_at else "  ! no se recuperó en la ventana")
    print(f"  déficit: {deficit:.0f} eventos  (= {deficit / baseline:.0f} s a la tasa base)"
          if baseline else "")
    print(f"  cambiaron de máquina: {len(changed)} subtareas "
          f"({', '.join(f'{names[k[0]]}[{k[1]}]' for k in changed) or 'ninguna'})")
    print(f"  plan aplicado: {'sí' if not wrong else 'NO — ' + str(len(wrong)) + ' fuera de plan'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
