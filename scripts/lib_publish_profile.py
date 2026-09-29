#!/usr/bin/env python3
"""Publish profiled costs to /var/thesis/loads, scaled to a target rate.

SCALED BY THE RATIO OF RATES, not multiplied by the target. A vertex is priced per
record of ITS OWN input, and Q8's join sees 266 records/s where the filters ahead
of it see 5000 — it pairs persons with auctions in a window and most events match
nothing. Multiplying every vertex by the source's target rate would charge the join
about 37x the work it does. Every vertex's rate moves with the source's, so one
factor serves them all.
"""
import argparse, json, math, subprocess, sys

ap = argparse.ArgumentParser()
ap.add_argument("--profile", required=True)
ap.add_argument("--target", type=float, required=True)
ap.add_argument("--node", default="minikube")
args = ap.parse_args()

profile = json.load(open(args.profile))
factor = args.target / profile["profile_rate"]

lines = []
skipped = []
for vid, v in profile["vertices"].items():
    cost, rate = v["cpu_ms_per_record"], v.get("records_s")
    if math.isnan(cost) or not rate or math.isnan(rate):
        skipped.append(v["name"])
        continue
    lines.append(f"{vid} {round(cost * rate * factor, 3)}")

if not lines:
    sys.exit("ERROR: nothing to publish — no vertex produced a CPU cost")
if skipped:
    print("  ! sin costo, quedan fuera del archivo (peso 1.0 en el fork):")
    for name in skipped:
        print(f"      {name}")

content = "\n".join(lines) + "\n"
script = ("sudo mkdir -p /var/thesis && sudo tee /var/thesis/.loads.tmp >/dev/null"
          " && sudo mv -f /var/thesis/.loads.tmp /var/thesis/loads"
          " && sudo chmod 644 /var/thesis/loads")
subprocess.run(["minikube", "ssh", "-n", args.node, "--", script],
               input=content, text=True, check=True,
               stdout=subprocess.DEVNULL)
print(f"published {len(lines)} vertices -> /var/thesis/loads (next rescale)")
