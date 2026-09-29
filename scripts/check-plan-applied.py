#!/usr/bin/env python3
"""Which stage ran on which machine, per RL cell of a run.

Crosses the layout the fork wrote at the measured decision (slices-repN.txt) with the mapping it
logged for that same decision (thesis-assign.log), and names each slice by its stage. A fixed
plan is only evidence if it is what ran; this is the check.

Usage: scripts/check-plan-applied.py results/placement-experiment/<run>
"""
import json, re, sys
from pathlib import Path

run = Path(sys.argv[1])
for cell in sorted(p for p in run.iterdir() if p.is_dir()):
    layouts = sorted(cell.glob("slices-rep*.txt"))
    log = cell / "thesis-assign.log"
    details = cell / "job-details.json"
    if not layouts or not log.exists():
        continue
    names = {}
    if details.exists():
        try:
            names = {v["id"]: v["name"] for v in json.load(open(details))["vertices"]}
        except Exception:
            pass
    stage_of = {}
    for line in layouts[-1].read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == "slice":
            anchors = [names.get(t.rpartition(":")[0], "") for t in parts[2:]]
            label = next((n for n in anchors if n.startswith(("Source", "Sink")) or "join" in n),
                         anchors[0] if anchors else "?")
            stage_of[int(parts[1])] = label.replace("Source: ", "").replace("Sink: ", "")
    width = len(stage_of)
    mappings = [l for l in log.read_text().splitlines() if f"slices={width} " in l]
    if not mappings:
        print(f"{cell.name}: sin decisión registrada a {width} slices")
        continue
    pairs = re.findall(r"slice#(\d+)->(\S+?)[,\]]", mappings[-1])
    by_machine = {}
    for index, tm in pairs:
        by_machine.setdefault(tm, []).append(stage_of.get(int(index), "?"))
    print(f"{cell.name}:")
    for tm in sorted(by_machine):
        print(f"   {tm:12} {', '.join(sorted(by_machine[tm]))}")
