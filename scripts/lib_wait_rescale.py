#!/usr/bin/env python3
"""Block until a requested rescale has actually landed.

WHY (measured 2026-09-07, and it explains a week of empty campaigns). A
`PUT /jobs/<id>/resource-requirements` returns HTTP 200 immediately and the job
keeps running at its old parallelism for a long time afterwards. The adaptive
scheduler walks Idling -> Stabilizing -> Stabilized -> Transitioning, and only in
the last step does it rebuild the execution graph and ask the assigner for a
placement. Timed on this cluster:

    20:05:43  requirements received (8 slots)
    20:06:46  Stabilizing -> Stabilized        (63 s)
    20:08:17  Stabilized  -> Transitioning     (+91 s)
    20:08:19  [THESIS_ASSIGN] slices=8         (156 s after the PUT)

The campaign held the measured width for warmup + window + 20 = 140 seconds, so it
moved on to the next step BEFORE the rescale it had asked for arrived. Every
"measured" episode therefore observed the submission's placement, and the
assignments that did show up were the ones whose timing happened to align — which
is why they appeared in two repetitions out of four, with inconsistent freeSlots.

Nothing was broken. The instrument was faster than the system.

Exit code 0 when every vertex with more than one subtask reached the target, 1 on
timeout. Vertices declared at parallelism 1 are semantic (Q5's global top-N, a
single sink) and are not expected to move.
"""

import argparse
import json
import subprocess
import sys
import time


def vertices(pod, namespace, jid):
    try:
        out = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod, "--", "curl", "-s", "-m", "15",
             f"http://localhost:8081/jobs/{jid}"],
            capture_output=True, text=True, timeout=45).stdout
        return json.loads(out).get("vertices", [])
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pod", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--target", type=int, required=True)
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    deadline = time.time() + args.timeout
    started = time.time()
    last = None
    while time.time() < deadline:
        vs = vertices(args.pod, args.namespace, args.job)
        if vs:
            # Only vertices that CAN move are evidence, and a vertex sitting at 1 while the
            # target is something else cannot: it is there on purpose, either because the
            # query declares it so (Q5's global top-N) or because the driver PINNED it to
            # build a slice geometry. The condition used to read `!= args.target`, which
            # INCLUDED exactly those vertices and so never confirmed — with a pin active the
            # job legitimately shows widths [1, 2] and the wait burnt its full 300 s every
            # step (2026-09-28). Q8 has no parallelism-1 vertex of its own, which is why this
            # only surfaced when random pinning started producing them.
            movable = [v for v in vs if int(v.get("parallelism", 1)) > 1 or
                       int(v.get("parallelism", 1)) == args.target]
            widths = {int(v.get("parallelism", 1)) for v in movable}
            if widths and widths == {args.target}:
                if not args.quiet:
                    print(f"    reescalado a {args.target} confirmado en "
                          f"{time.time() - started:.0f}s")
                return 0
            last = sorted(widths)
        time.sleep(5)

    if not args.quiet:
        print(f"    ! el reescalado a {args.target} no llegó en {args.timeout:.0f}s "
              f"(anchos actuales: {last}) — el paso se medirá en la configuración "
              f"que haya", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
