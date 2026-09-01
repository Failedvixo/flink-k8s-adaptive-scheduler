---
name: project-next-session-pilot
description: "Hand-off for 2026-08-14: replicate the +38% placement effect with more arms. The result exists; it needs n."
metadata:
  node_type: memory
  type: project
  originSessionId: 0593a148-9705-4a45-b9c3-39034305d756
  modified: 2026-08-13T23:29:08.590Z
---

**Written 2026-08-13, for the session after a reboot.** The headline result and its causal chain are
in [[project_flink_core_fork]] under "THE RESULT (2026-08-13)".

**Where things stand:** placement DOES change throughput (+38%) and latency (−14%), but only once the
TaskManagers have a real CPU limit. Four earlier nulls are explained: the containers had
`resources: {}` on 12-core nodes, so two expensive slices sharing a TaskManager never contended.

**THE RUN TO DO** (~2 h) — same shape as the one that worked, more arms so the "concentrated"
condition stops being an accident of FCFS:

```
CPU_LOAD=2500 JOB_CLASS=com.thesis.benchmark.ConfigurableGraphJob \
PIN_VERTEX="CPU Load" PIN_PARALLELISM=2 \
TM_REPLICAS=3 SUBMIT_PAR=6 SCHEDULE="4* 6" REPS=10 \
ARMS="LEAST_LOADED ROUND_ROBIN FCFS ACO GA" \
  ./scripts/run-placement-experiment.sh
```

Then `python3 scripts/analyse_placement_experiment.py <dir> --by-placement` — that flag is the whole
analysis (pooled contrast, the within-arm contrast that kills the arm confound, and the sanity check
that arms agree where they placed alike). ACO and GA concentrated the pair in the six-arm run, so this
should give ~20 episodes per condition instead of 4.

**VERIFY BEFORE LAUNCHING — the CPU limit is what makes the effect exist at all:**
```
kubectl get deploy flink-taskmanager -n flink -o jsonpath='{.spec.template.spec.containers[0].resources}'
```
must show `{"limits":{"cpu":"1"}}`. If a reboot or a manifest re-apply wiped it, restore with
`kubectl set resources deployment/flink-taskmanager -n flink --limits=cpu=1`. Without it the run
reproduces the null.

**Cluster carries four deliberate operating points** — all to be reported in the write-up, none of
them accidental: `taskmanager.memory.managed.size: 16m`, `slot.idle.timeout: 300000`,
`pipeline.max-parallelism: 8`, `limits.cpu: 1`. Inspect with `scripts/patch-flink-property.sh --show
<key>` (the CPU limit is a k8s resource, not a Flink property, so check it with kubectl as above).

**Then, the experiment that turns this into a curve rather than a point:** redo the CPU_LOAD sweep
WITH the limit in place, two arms only, and plot "TaskManager utilisation vs the throughput gap
between spreading and concentrating". That answers *how much contention is needed before placement
matters*, which is a stronger contribution than a single percentage. The 2026-08-13 sweep without the
limit was void — `busy` read 105.1 ms/s at every load level.

**KNOWN DESIGN GAPS, still unfixed:** arm order is fixed so cumulative drift is confounded with the
arm (randomise it); a failed arm is abandoned rather than retried; `had_choice = freeSlots > slices`
is wrong when slices are heterogeneous, because the PERMUTATION matters even with no spare slots.

**STILL UNCOMMITTED in BOTH repos**, now including `run-placement-experiment.sh`,
`analyse_placement_experiment.py` (with `--by-placement`), `patch-flink-property.sh` and the
e2e-delay changes to `arm_controller.py`. The paper PDF is also still not in the repo.
