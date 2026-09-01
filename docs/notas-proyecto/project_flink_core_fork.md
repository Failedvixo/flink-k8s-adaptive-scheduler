---
name: project-flink-core-fork
description: "Flink 1.18 fork — moving scheduling into the JM. Phases 1-3 validated in-cluster: strategies rebalance at rescale, and the arm is now a live signal driven by an external SARSA learner on Flink metrics."
metadata: 
  node_type: memory
  type: project
  originSessionId: 371a54f6-f90d-4831-8c48-d797a135b6e2
  modified: 2026-08-13T23:28:45.690Z
---

**Direction as of 2026-08-03.** New brazo of the thesis: stop only influencing K8s pod placement externally; move the decision logic INTO Flink's task→slot assignment inside the JobManager. Requires forking + compiling Apache Flink. Based on Li et al., "Cost-Efficient Scheduling of Streaming Applications in Apache Flink on Cloud", IEEE TBD 9(4) 2023 (CETSA + LBA-CE). PDF supplied by user; not committed to repo (only `docs/sarsa_meta.md` exists there).

**Verified against code (this session):**
- Project Flink version = **1.18.0** (official `flink:1.18.0` image, JM+TM), `jobmanager.scheduler: adaptive` on both — [flink-manifests.yaml:73,92,124,139]. pom `flink.version=1.18.0`.
- External strategies operate purely on K8s abstractions: `SchedulingStrategy.selectNode(List<V1Node>, V1Pod, ClusterMetrics) → V1Node`; reward = node CPU%/mem% band from K8s Metrics Server (BanditStrategy.calculateReward). Placement = once, at pod creation, via `createNamespacedBinding` (AdaptiveScheduler.java:211,229). `periodicEvaluate` only re-picks the meta-arm for FUTURE bindings.
- autoscaler.sh depends on `PUT /jobs/{id}/resource-requirements` — an AdaptiveScheduler-only feature ("Requiere: jobmanager.scheduler: adaptive").

**What the paper actually did (primary source, corrects the original brief):**
- Paper used **Flink 1.11.1** (Table 4) — AdaptiveScheduler didn't exist until 1.13, so they modified the **DefaultScheduler** path.
- Intervention = the **slot-selection / task-assignment** phase (Alg.1/2): per subtask, get freeSlots meeting resource req, pick node by cost/fitness priority, return that node's slot; fallback Round-Robin.
- => Correct Flink extension point is **`SlotSelectionStrategy`** (`selectBestSlotForProfile`), NOT `SchedulingStrategy` (region ordering) as the brief guessed. `SlotInfo.getTaskManagerLocation()` gives TM/node identity at decision time, so V1Node-picking logic maps to slot-picking.
- Reward signal: paper reads from an EXTERNAL monitoring module + DB, not from inside Flink runtime → same pattern as the project's `ClusterMetrics`. Reward portability "solved" the way the paper did it.

**Three gaps to resolve before coding:**
1. Version gap 1.11.1→1.18.0: must reimplement against 1.18 internals, not port their patch.
2. Homogeneous cluster (Minikube 3×2CPU) vs paper's heterogeneous priced VMs → the "cost-efficient" (RC rental cost) half doesn't apply; the LBA-CE load-balancing half ports (aligns with FCFS/Balanced/LeastLoaded/Bandit/SARSA). Option: fake per-node prices to keep the cost model.
3. To follow the paper faithfully must switch to `jobmanager.scheduler: default` → LOSE reactive rescaling (autoscaler line). Consistent with paper (fixed parallelism).

**PLUGGABILITY VERDICT (2026-08-03, read against cloned release-1.18.0 at ~/projects/flink-custom-scheduler, shallow):** VIABLE and light.
- `SlotSelectionStrategy` (flink-runtime .../jobmaster/slotpool/SlotSelectionStrategy.java) = single method `Optional<SlotInfoAndLocality> selectBestSlotForProfile(FreeSlotInfoTracker, SlotProfile)`.
- Candidate slots via `freeSlotInfoTracker.getFreeSlotsInformation()` → `Collection<SlotInfo>`; each `SlotInfo.getTaskManagerLocation()` gives TM hostname/address + ResourceID → node/TM identity IS available at decision time. Also `getTaskExecutorUtilization(slotInfo)` gives per-TM utilization NATIVELY (LeastLoaded/Balanced may not need K8s metrics).
- NOT config-pluggable by classname: `SlotSelectionStrategyUtils.selectSlotSelectionStrategy()` hardcodes impl choice via 2 booleans (`cluster.evenly-spread-out-slots`, local-recovery); no Class.forName/ServiceLoader.
- BUT injection is a SINGLE choke point: patch that 1 method to read a custom config key (e.g. `thesis.scheduler.strategy`) and return a custom class; it flows into `PhysicalSlotProviderImpl` (DefaultSchedulerComponents.java:93-100) untouched. Rebuild flink-runtime + repackage image. Strategy selection = config + JM restart (same pattern as current FIXED_STRATEGY).
- Confirmed DefaultScheduler-only path (DefaultSchedulerComponents.java:42 comment). AdaptiveScheduler uses a different allocator → must set `jobmanager.scheduler: default`, losing reactive rescaling (consistent with paper).

**Nuances:** (1) natural placement unit = TM, not K8s node (5 TMs on 3 nodes); TaskManagerLocation gives the TM, TM→node needs extra lookup. (2) slot-sharing granularity = per parallel-pipeline-instance, not per operator (pipeline does disableChaining on CPU-Load; verify sharing groups).

**DECIDED ARCHITECTURE (2026-08-04):** meta-scheduler stays EXTERNAL (existing K8s controller) + Flink runs `jobmanager.scheduler: adaptive`. Rationale: rescales are the recurring placement-decision events → each parallelism up/down re-runs the SlotAssigner, which applies whatever arm the external meta currently selected. This resolves the "meta relevance under one-shot placement" question: the meta is dynamic over the stream of rescale events, not inside a stable job.
- Data flow: EXTERNAL meta observes metrics (REST/K8s) → picks arm {FCFS,RR,Balanced,Bandit,SARSA} → publishes it (config/endpoint) → learns reward (throughput/core) per rescale. Autoscaler triggers rescales (PUT resource-requirements). INSIDE fork: custom `ThesisSlotAssigner` (implements SlotAssigner) reads published arm, places slices, PURE (no state mutation — learning lives outside, avoids speculative-call corruption).
- Injection point CONFIRMED: SlotSharingSlotAllocator.java:133-136 hardcodes DefaultSlotAssigner/StateLocalitySlotAssigner ternary → patch to return ThesisSlotAssigner by config key. One new file + small wiring patch.
- 3 design constraints: (1) arm change is LATCHED to rescale events — no rescale, no strategy change applied (workload must trigger rescales; SINE/STEP autoscaler does). (2) REWARD CONFOUND: throughput/core after rescale reflects BOTH new parallelism AND new placement → to isolate placement effect, compare SAME parallelism transition under different arms (e.g. 4→8 under FCFS vs 4→8 under Bandit). Critical for thesis defensibility. (3) SlotAssigner must be pure (called speculatively during rescale eval). Also: placement granularity = pipeline slices, not per-operator, unless `.slotSharingGroup()` isolates CPU-Load.
- Metaheuristics (SA/GA/ACO/TS) fit the SlotAssigner well (it gets the whole tasks×slots instance in one call); run-fresh-per-call = pure/safe; ACO pheromones / warm GA populations = protect like learning state. Heavy search blocks JM main thread (ComponentMainThreadExecutor) → bound budget or compute externally + apply plan.

**FORK NOW HAS ITS OWN REPO (2026-08-04):** **https://github.com/Failedvixo/flink-custom-scheduler** — PUBLIC, default branch `main`. Remotes in the local clone: `origin` = this new repo (push here), `upstream` = apache/flink (fetch only, no push rights). Two commits: `55903fde` = verbatim Flink 1.18.0 tree, `e2a084b2` = the thesis SlotAssigner. **The diff between them IS the fork's whole contribution** (3 files, 703 lines) — handy for the defence.
- HAD TO RE-ROOT THE HISTORY: pushing the shallow (`--depth 1`) clone to a fresh remote fails with `remote unpack failed: index-pack failed` / `did not receive expected object`, because the pack references the missing parent. Un-shallowing means fetching Flink's whole history (GBs; WSL already saturated once on less). Fix used: `git checkout --orphan <br> a5548ccc` → commit the pristine tree as a root commit → cherry-pick the thesis commit on top. Tree verified byte-identical to upstream (`git diff --stat a5548ccc HEAD` empty); upstream SHA recorded in the root commit message for provenance.
- Flink's repo has a husky pre-commit hook needing `npm`; commits there fail with `npm: not found` unless made with `--no-verify` (or `-c core.hooksPath=/dev/null` for cherry-pick).
- Main repo `Failedvixo/flink-k8s-adaptive-scheduler` commit `9777672` carries the deploy/measurement scripts and the spread CSVs.

**ENV CONFIRMED (2026-08-04):** clone at `~/projects/flink-custom-scheduler` (shallow release-1.18.0). Smoke build `mvn clean install -pl flink-runtime -am -DskipTests -Dfast -T 1C` = BUILD SUCCESS in ~2min with **JDK 11.0.31 + Maven 3.8.7** (JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64). Incremental `mvn clean compile -pl flink-runtime -Dfast` ≈ 27s (deps now in ~/.m2). Maven 3.8.7 fine for runtime module (shading warning only affects flink-dist).

**PHASE 1 DONE (code, compiles) 2026-08-04:** plumbing-validation assigner written + wired.
- NEW `flink-runtime/.../scheduler/adaptive/allocator/ThesisSlotAssigner.java`: implements SlotAssigner, replicates DefaultSlotAssigner (pairs each ExecutionSlotSharingGroup with next free slot in order) + logs `[THESIS_ASSIGN] group#N -> TM ...`. Reuses `DefaultSlotAssigner.createExecutionSlotSharingGroups` + `SlotSharingSlotAllocator.ExecutionSlotSharingGroup` (both package-private, same package). Ignores previousAllocations for now (state locality deferred).
- Enabled via env `THESIS_SLOT_ASSIGNER` (or sysprop `thesis.slot.assigner`) = 1/true/yes; default OFF → default behaviour untouched.
- PATCHED `SlotSharingSlotAllocator.java` determineParallelismAndCalculateAssignment ternary → `if (ThesisSlotAssigner.isEnabled()) new ThesisSlotAssigner() else <default/stateLocality>`. (+10/−4.)
- IDE flagged a false-positive error (line 74 name clash) — it's a "non-project file" so IDE classpath is wrong; Maven compiles clean (2398 files). Trust Maven, not the IDE, for the fork.

**IMAGE BUILT + SATURATION LESSON (2026-08-04):** Custom image `flink:1.18.0-thesis` = official `flink:1.18.0` + patched `flink-dist-1.18.0.jar` (surgical: extract official dist jar from JM pod, `jar uf` in the 4 recompiled allocator classes — `jar uf` needs `-C dir` repeated PER file, and inner-class `$` single-quoted). Image lives in HOST docker (921MB), survives reboots. `minikube image build` is useless here (puts image in buildkit, not node dockerd; and `docker-env` is incompatible with multi-node). Correct path: build with HOST docker, then distribute.
- **`minikube image load` of the 921MB image to ALL 3 nodes SATURATED the WSL machine → forced reboot.** Do NOT load big images to all nodes. 
- Lighter deploy plan (decided): ThesisSlotAssigner runs in JM ONLY (TMs keep official image). So put the custom bits on ONE node + pin JM there. **Preferred = hostPath overlay**: `minikube cp` the patched 122MB dist jar to one node, hostPath-mount it over /opt/flink/lib/flink-dist-1.18.0.jar in the JM pod (nodeName/nodeSelector pin). No image rebuild/load — best for Phase 2/3 iteration (swap 122MB, not 921MB). Alt: `docker save flink:1.18.0-thesis | minikube ssh -n <node> -- docker load` (single node, 921MB once) + pin JM.

**PHASE 1 VALIDATED END-TO-END (2026-08-04) ✅** — `[THESIS_ASSIGN]` logs confirmed in JM, submitted job RUNNING. ThesisSlotAssigner activated (did NOT fall back to default). Observed: both pipeline slices landed on the SAME TM (default's arbitrary/imbalanced placement) — the concrete "before" that Phase 2 improves.

**WORKING DEPLOY RECIPE (hostPath overlay — light, reversible, iterable):**
1. Recompile fork: `mvn clean compile -pl flink-runtime -Dfast` (JAVA_HOME=java-11), classes in flink-runtime/target/classes.
2. Regenerate patched jar: `kubectl cp -n flink <jmpod>:/opt/flink/lib/flink-dist-1.18.0.jar ./flink-dist-thesis.jar`, then `jar uf` the 4 allocator classes (`-C dir` per file; inner `$` single-quoted).
3. `minikube ssh -n minikube -- sudo mkdir -p /var/thesis`; `minikube cp flink-dist-thesis.jar minikube:/var/thesis/flink-dist-thesis.jar`; `minikube ssh -n minikube -- sudo chmod 644 /var/thesis/flink-dist-thesis.jar`.
4. Patch JM deployment (kubectl patch, strategic merge — does NOT touch versioned yaml): `nodeName: minikube`, env `THESIS_SLOT_ASSIGNER=true`, hostPath volume (type File → /var/thesis/flink-dist-thesis.jar) mounted at /opt/flink/lib/flink-dist-1.18.0.jar. JM has NO schedulerName so nodeName is safe. TMs keep official flink:1.18.0 (don't need custom code).
5. Trigger: `kubectl exec <jmpod> -- flink run -d /opt/flink/examples/streaming/TopSpeedWindowing.jar`; grep JM logs for `[THESIS_ASSIGN]`.
- REVERT: `kubectl apply -f kubernetes/flink-manifests.yaml` restores stock JM. Host image flink:1.18.0-thesis (921MB) also exists but the hostPath jar path avoids the saturating 3-node image load.
- For Phase 2/3: repeat steps 1-3 (swap 122MB jar) + `kubectl rollout restart deployment/flink-jobmanager` — no image rebuild.

**PHASE 2 DONE (2026-08-04) — code works, but the RESULT IS NEGATIVE and it redirects the thesis.**
- ThesisSlotAssigner now has 3 arms via env `THESIS_ASSIGN_STRATEGY` / sysprop `thesis.assign.strategy` (default ROUND_ROBIN): DEFAULT (iteration order baseline), ROUND_ROBIN (cyclic over TMs, exhausted TMs drop out), LEAST_LOADED (TM with most free capacity left). `currentStrategy()` is the seam for the Phase-3 external arm signal.
- Determinism was NOT free: groups come from a `HashMap.values()` and each `ExecutionSlotSharingGroup` gets a `UUID.randomUUID()` id, so slices+slots must be sorted (subtask index; TM resourceId then physicalSlotNumber) before deciding, or speculative rescale calls can disagree.
- Unit test `ThesisSlotAssignerTest` (9 tests, green; existing allocator tests 39/39 green). Deploy automated by `scripts/deploy-thesis-fork.sh [STRATEGY]` in the main repo.

**PHASE 2 VALIDATED IN-CLUSTER (2026-08-04) ✅ — the strategies DO change placement, but only at rescale.** Controlled experiment (3 TMs × 2 slots = 6 free slots; submit p=6 then PUT resource-requirements upperBound=3 → slices=3, freeSlots=6), 8 repetitions per arm, `scripts/measure-assigner-spread.sh`:
- **STOCK 3/8** (the real baseline), DEFAULT **3/8**, ROUND_ROBIN **8/8**, LEAST_LOADED **8/8**. Stock leaves one TM idle in 5/8 rescales while another carries 2 slices.
- **THE BASELINE MUST BE `STOCK`, NOT `DEFAULT`** (user's call, 2026-08-04, and correct): stock Flink picks its assigner per call — `DefaultSlotAssigner` only when `previousAllocations.isEmpty()` (first submission), `StateLocalitySlotAssigner` otherwise. A rescale ALWAYS has previous allocations, so unpatched Flink anchors slices to the slot already holding their state. Comparing against DEFAULT measures against something Flink never does at a rescale.
- Added `Strategy.STOCK`, which DELEGATES to those two real classes (not a reimplementation) and logs `strategy=STOCK(DefaultSlotAssigner|StateLocalitySlotAssigner)`. Verified per event: 8 submits → DefaultSlotAssigner, 8 rescales → StateLocalitySlotAssigner.
- STOCK and DEFAULT both scoring 3/8 is a coincidence of this workload (TopSpeedWindowing has near-zero state); do not read it as "state locality is equivalent to iteration order". n=8.
- OPEN CONFOUND for the reward: RR/LEAST_LOADED discard `previousAllocations` entirely, so they move state on every rescale. On a stateful job (Nexmark Q5/Q8) that costs state transfer right after the rescale and will depress throughput/core independently of placement quality. Balance-vs-state-locality is the genuine trade-off the arm set is missing — RR and LEAST_LOADED are indistinguishable (8/8 both) because neither considers state.
- CSVs in `results/assigner-spread/`. This is the headline Phase-2 evidence.
- CONFOUND FOUND AND CONTROLLED: an earlier DEFAULT run scored 7/8 only because the RM had spread the 6 slots over 5 TMs (~1 slot/TM ⇒ almost any pick is balanced), while ROUND_ROBIN was measured with 3 TMs (2 slots/TM = the hard case). ALWAYS pin the TM count (`kubectl scale deployment flink-taskmanager --replicas=N`) and check `tmsAvailable=` before comparing arms.
- DEFAULT is nondeterministic run-to-run (follows incidental free-slot iteration order) ⇒ single paired runs prove nothing; measure a distribution over repetitions.

**STILL TRUE — SlotAssigner is inert at job SUBMISSION.** Measured, same job/parallelism: ROUND_ROBIN → spread 2/2/2 over 3 TMs; DEFAULT → spread 2/2/2. Identical load, only the slice→TM permutation differs.
- Cause: JM declares exactly N slots (`Received resource requirements ... numberOfRequiredSlots=6`), the RM's `FineGrainedSlotManager` picks WHICH TMs fulfil it, and the pool ends up holding exactly N. So `freeSlots == slices` and every slot is used no matter what the assigner decides. Observed at p=2 (freeSlots=2, tms=1) and p=6 (freeSlots=6, tms=3).
- => The Phase-1 "both slices on the same TM" observation was NOT the assigner's arbitrary choice; the RM had already committed both slots on one TM. No SlotAssigner strategy can fix it.
- **The real "which TM" lever at submission is in the ResourceManager**: `slotmanager/DefaultResourceAllocationStrategy` + `SlotMatchingStrategy` (`AnyMatchingSlotMatchingStrategy` vs `LeastUtilizationSlotMatchingStrategy`), selected by `cluster.evenly-spread-out-slots` (ClusterOptions.java:88 → SlotManagerConfiguration). That is the true CETSA/LBA-CE analogue — stock Flink's own pack-vs-spread knob.
- **THE EXACT RULE (measured, 2026-08-04):** `SlotSharingSlotAllocator.determineVertexParallelism` computes `slices = Math.min(upperBound, availableSlots)` ([SlotSharingSlotAllocator.java:218]). So the assigner has a decision **iff `upperBound < slots the pool currently holds`** — the rescale DIRECTION is not the criterion.
  - The asymmetry: `upperBound` changes instantly (a declaration via `PUT resource-requirements`), the held slot set changes slowly (RM allocation, and surplus is only released later on slot idle timeout).
  - Scale-DOWN 6→3: `slices=3 freeSlots=6` ✅ decision. Plain scale-UP 3→6: `slices=6 freeSlots=6` ❌ no decision (parallelism expands to consume the arriving slots).
  - **CORRECTION to an earlier claim in this file: scale-UP CAN give a decision.** Measured down 6→2 then up 2→4 while the pool still held 6: `slices=4 freeSlots=6` ✅. Any rescale landing below the retained slot count qualifies, so learning events are more frequent than "scale-downs only".
- Second axis still owned by the assigner: WHICH slice (not how many) lands on a TM — matters only under subtask load asymmetry (key skew / Nexmark HEAVY_VERTEX_PATTERN), not for slot counts.

**PHASE 3 DONE + VALIDATED IN-CLUSTER (2026-08-05) ✅** — live arm signal + external SARSA learner.
Design decided by user this session: learner = Python controller on the host (NOT inside the Java
K8s scheduler); channel = hostPath arm file (NOT ConfigMap, whose kubelet sync ~60s would miss
rescales). Docs: `docs/phase3_arm_controller.md`.
- FORK: `currentStrategy()` now resolves sysprop → **arm file** (`THESIS_ARM_FILE`, default
  `/var/thesis/arm`) → env → default; re-read max 1×/s (cache), failed/garbled read KEEPS last good
  arm, logs `[THESIS_ARM] arm changed X -> Y`. Tests 16/16 green.
- **The JM must mount the DIRECTORY /var/thesis, not the file** — a single-file bind mount pins the
  inode, so the atomic write (temp + rename) in `scripts/publish-arm.sh` would never be seen inside
  the pod. The 1s cache also prevents the arm changing WITHIN one speculative rescale evaluation.
- NEW `scripts/publish-arm.sh ARM|--read` (atomic write via minikube ssh) and
  `scripts/arm_controller.py` (measure → reward → SARSA → publish; `--fixed-arm`, `--observe`).
  `scripts/measure-assigner-spread.sh` no longer needs a redeploy per arm — it publishes.
- MEASURED VALIDATION (3 TMs × 2 slots, p=6 → ub=3): same JM pod restarts=0, STOCK 1/3 balanced then
  ROUND_ROBIN (published hot) 3/3 → reproduces Phase 2 through the new channel. CSVs in
  `results/assigner-spread-phase3/`. Closed loop verified: published LEAST_LOADED → next rescale
  applied it (had_choice=True) → Q = 0.3 = α·r.
- CONTROLLER DESIGN POINTS THAT MATTER: reward = 1/(1+CV of per-TM busy) over ALL registered TMs
  (hosting-only would score a full-packing arm as perfect); arm credited = the one the JM LOGGED,
  not the one published (a published arm with no rescale is never applied); epochs NOT credited when
  freeSlots==slices or when busy < `--min-busy-ms-s` (dispersion of idle load is pure noise); epoch
  timing anchored to the vertices' own `start-time`, not to when the controller noticed.

**ARM SET DECIDED BY USER (2026-08-05), PACK DROPPED:** arms = FCFS, LEAST_LOADED, ROUND_ROBIN,
**ACO**, **GA**; metas = **BANDIT** + **SARSA**. PACK was only ever a workaround for RR ≡ LEAST_LOADED;
ACO/GA supply the real trade-off, so it was NOT implemented. All built + validated in-cluster same day:
- `DEFAULT` arm renamed **FCFS** (legacy name still parsed, so Phase-2 CSVs stay readable).
- NEW `PlacementInstance.java` (cost = `w_balance*imbalance + w_locality*lost_state`, weights via
  THESIS_COST_BALANCE / THESIS_COST_LOCALITY; locality = key-group intersection, same arithmetic as
  StateLocalitySlotAssigner, reimplemented because their score class is private), plus
  `AntColonyPlacement.java` + `GeneticPlacement.java`. Tests 21/21 (60 with existing allocator suite).
- **BOTH SEARCHES MUST BE STATELESS ACROSS CALLS AND BOUNDED BY ITERATION COUNT, NEVER A CLOCK** — the
  assigner is re-invoked speculatively per rescale; persistent pheromone or a time-boxed cutoff makes
  the same question get different answers. Seeded from the instance (TM ids, slot numbers, subtask
  indices, jobVertexId) — deliberately NOT ExecutionSlotSharingGroup.getId(), which is a fresh UUID
  per call. A test that builds a new JobVertexID per invocation will "fail" determinism — that is the
  harness, not the code.
- In-cluster: ACO 3/3 and GA 3/3 balanced rescales, no exception in the JM.
- NEW arrival distribution **RAMP** (linear climb 0.25x→stepHighFrac over the run, load never returns)
  in BOTH `GraphConfig` and `NexmarkGenerator` — replaces STEP in the new campaign.
- Controller gained `--meta bandit|sarsa` (UCB1 vs contextual SARSA, identical rewards).
- NEW `scripts/run-fork-campaign.sh` (arm x query x dist grid; autoscaler drives the rescales, the
  controller records per-rescale metrics) + `scripts/analyse_fork_campaign.py` (ranks scenarios by how
  far apart the arms end up → the widest scenario is the one to train the metas on; the flat ones are
  where no meta can beat a fixed arm). CAMPAIGN NOT YET RUN — 6 arms x 2 queries x 3 dists x 10 min
  ≈ 6 h, plus ~2 h for the metas.
- 13 per-query wrappers `experiment-fork-q0.sh`..`q12.sh` (heavy vertex per query taken from the old
  experiment-qN.sh: q0-passthrough, q1-currency, q2-selection, q3-state-join, q4-cat-avg,
  hot-items-count, q6-seller-avg, q7-max-bid, new-users-join, q9-winning-bid, q10-sink, q11-sessions,
  q12-proc-sessions). `PILOT=1` = 5 min, SINE only, warmup/window 30s + tightened autoscaler cadence.
- **SLIM RESULTS POLICY (user's ask, disk pressure):** `scripts/summarise_fork_cell.py` writes
  summary.json and DELETES job-details.json/autoscaler logs/scale-events/job-id; keeps summary.json +
  episodes-*.csv (the training data) + thesis-assign.log + arm-controller.log. ~15 KiB/cell. KEEP=all
  to debug.

**SMOKE-TEST FINDINGS 2026-08-05 (harness works, but read these before the pilot):**
1. **180s cell gave 1 assignment round and 0 decisions** — the autoscaler never rescaled, so there was
   NO training signal. Its default cadence (20s settle + 10s poll + 30s cooldown) barely fits one
   action into a short cell. Check `summary.json → assignments.decisions` FIRST on any empty-looking
   cell. PILOT=1 now tightens POLL/COOLDOWN/thresholds.
2. **Flink returns NaN metrics right after a restart**, and `statistics.pstdev`/`stdev` RAISE on a
   non-finite sample (AttributeError 'float' has no attribute 'numerator') — this killed the
   controller mid-cell. Fixed: non-finite values dropped at parse time, stdev computed by hand in both
   arm_controller.py and summarise_fork_cell.py. Do not reintroduce statistics.pstdev here.
3. The controller must be run with `python3 -u`: it is killed with the cell, and buffered stdout is
   lost unflushed (the log came out 0 bytes).
4. Two harness bugs found by running it, not by reading it: `RATE` was exported per cell and then
   re-read as the default for the next cell; and the job's `WINDOW` (window operator size) collided
   with the measurement window. Now RATE_OVERRIDE / MEASURE_WINDOW.

**TWO PREREQUISITES FOUND BY RUNNING THE HARNESS (2026-08-05) — both fixed and verified:**

**A. EVERY parallel vertex must scale, not just the heavy one.** With slot sharing the group's width
(= slots requested) is set by the WIDEST vertex. With the source pinned at its initial parallelism,
every rescale has `freeSlots == slices` → the assigner NEVER has a decision → all arms identical →
campaign yields zero signal. Measured: flag off `slices=8 freeSlots=8` 0 decisions; flag on
`slices=2 freeSlots=8` real decision. NEW `SCALE_ALL_VERTICES` in autoscaler.sh (default 0 = historic;
campaign exports 1). Vertices declared at parallelism 1 STAY at 1 — in several queries that 1 is
semantics (Q5 global top-N), not a performance choice. Also: the pipeline must START WIDE
(INITIAL_CPU_PAR=PARALLELISM=8) and be narrowed — starting at the floor throttles the source, starves
the heavy vertex, and the autoscaler then reads an idle signal it never scales up from. It is the
scale-DOWNS that create freeSlots > slices.

**B. CHECKPOINTS MUST BE ON SHARED STORAGE — this was silently invalidating the whole comparison.**
Stock config `state.checkpoints.dir: file:///tmp/checkpoints` = each TM checkpoints to its OWN local
disk. First rescale that moves a subtask → `FileNotFoundException: /tmp/checkpoints/<job>/chk-N/...`
→ `BackendBuildingException: Failed when trying to restore heap backend` → job restart-loops.
**NOT NEUTRAL BETWEEN ARMS:** STOCK (StateLocalitySlotAssigner) anchors slices to the slot holding
their state and escapes it; RR/LEAST_LOADED/ACO/GA move state on purpose and crash → a campaign on
local checkpoints would "prove" stock wins, as an infrastructure artefact.
FIX: `kubernetes/minio.yaml` + `scripts/setup-shared-checkpoints.sh` — MinIO (hostPath /var/thesis/
minio on the control-plane node, bucket = a dir created by an initContainer) + `s3://` checkpoints via
`ENABLE_BUILT_IN_PLUGINS=flink-s3-fs-presto-1.18.0.jar` (the jar already ships in the official image
under /opt/flink/opt — no rebuild). Chose MinIO over NFS (needs nfsd, unreliable in WSL) and over
pinning all TMs to one node (would flatten the multi-node topology). APPLIED TO THE CLUSTER and
verified: job stays RUNNING through 3 rescales, 0 restore failures, busy≈454 ms/s real load,
1 credited episode. A `rollout restart` KEEPS the fork patch; only re-applying flink-manifests.yaml
reverts both.

**STILL OPEN:** (1) `--warmup` is a free parameter that changes results; fix+document. (2) In the
verification run the autoscaler only ever scaled DOWN (8→6→4→2 then stuck at the floor) — watch this
in the pilot; may need SCALE_UP_THRESH tuning. (3) `assignments.rounds` in summary.json counts
speculative assigner invocations (258 in one 5-min cell), NOT applied placements — the controller's
episodes are the ground truth.

Phase 4 was never defined anywhere (the written sequence stops at Phase 3). Proposed, not recorded:
the experimental evaluation — Nexmark Q5/Q8 under SINE/STEP per arm and under the learned meta,
isolating placement per the reward confound.

**PILOTS RUN 2026-08-06 — the harness works; the WORKLOAD and the CLUSTER SIZING are what block the campaign.**

*q5 pilot (6 arms, SINE, 5 min):* `decisions` 3-5 per cell (prerequisite A confirmed fixed), 0 restore
failures (MinIO confirmed), but `credited` 0-2 of 3. Cause: `busy_mean_ms_s` 0-15 out of 1000 — the job
is 0-1.5% busy. Same root cause makes the autoscaler only ever scale DOWN (8→6→4→2, parks at the floor;
exactly one 2→4 in six cells): it reads busy% far under SCALE_DOWN_THRESH. **Dangerous artefact seen:
an idle job scores the PERFECT reward** (FCFS `busy=0, cv=0, reward=1.0`) — the `--min-busy-ms-s` guard
is the only thing rejecting it. Never lower that guard to "credit more episodes".

**THREE FINDINGS, each blocking a different escape route:**

1. **`NexmarkRealJob` IGNORES cpuLoad** — `args[4]` is parsed then dropped ("currently unused — kept for
   compatibility", NexmarkRealJob.java:34,57), while `run-fork-campaign.sh` still exports `CPU_LOAD=2500`
   copied from the synthetic scripts. The synthetic `ConfigurableGraphJob`/`GraphConfig` DOES spend it
   (2500 iterations/event) — that is why the whole K8s line had a usable busy%. Saturation = rate ×
   cost/event; the distributions (CONSTANT/SINE/RAMP) only shape the RATE. Real q5 costs ~nothing per
   event, so "high load" scenarios are high-rate/idle-machine. **Raising RATE cannot close it:** ~1% busy
   at 30k ev/s needs ~50x = ~1.5M ev/s, and the most expensive thing in the pipeline today is the
   generator itself (`Math.pow` per event in `zipfPick`) — more rate saturates the SOURCE, which is now
   also a scaled vertex, so the arms would be placing generator subtasks.

2. **q3/q8 joins never match.** `person.id`, `auction.seller`, `bid.bidder` are all
   `rng.nextLong() & 0x7FFFFFFFL` — independent over 2^31. Only `bid.auction` goes through `zipfPick`
   (bounded by hotPool). Join is `person.id == auction.seller` ⇒ ~0.24 matches per 10s window; VERIFIED
   in the committed results (`[Q8-Sink-N] received=1..2` for a whole run). The committed q8 numbers
   measure an empty join (both sides still get buffered, so there IS work and state, but the query's
   semantics are never exercised). Real Nexmark draws sellers from the EXISTING person population, so
   fixing this moves TOWARD the spec, not away — unlike cpuLoad, it carries no methodological debt.
   Cost of fixing = re-running the q8 cells. Alternative = scope q3/q8 out of the query set.
   Same root cause deserves a cardinality audit of all queries: q4 keys on `bid.auction % 16` = **16 keys
   total**, which over 8 subtasks is structural skew unrelated to the arms.

3. **THE REAL CONSTRAINT — TaskManager task heap is 25.6 MB.** `taskmanager.memory.process.size: 1024m`
   (flink-manifests.yaml:135) ⇒ `-Xmx161061270` and `taskmanager.memory.task.heap.size=26843542b` after
   metaspace 256m + JVM overhead 201m + framework heap 134m + managed 241m + network 67m. The container
   has NO k8s limits (`resources: {}`); the ceiling is Flink's own config. Nodes are ~8 GB each at
   **6-20% used** — enormous headroom, self-inflicted limit.
   - q11 (`keyBy(bidder)` + `EventTimeSessionWindows` gap 10s, bidder unique per event ⇒ ~300-450k live
     sessions) OOM'd every TM: 4/5 in CrashLoopBackOff, ~11 restarts, exit 239,
     `OutOfMemoryError ... "System Time Trigger for q11-sessions"`. Cells then read `job=CREATED/FAILED`,
     `decisions=0`, `credit_note: the assigner had no choice (freeSlots == slices)` — dead TMs, no slots.
   - **Any state-heavy path is blocked at this sizing, including the `HOT_POOL=100000` plan.** Also
     explains retroactively why state locality never showed up in ANY prior result (K8s line included):
     with 25 MB the state is too small for moving it to cost anything.
   - FIX (not yet applied): raise `process.size` 1024m → ~3072m (task heap ≈ 1.2 GB, ~47x). Patch
     FLINK_PROPERTIES the way `setup-shared-checkpoints.sh` does — rewrite only the memory line, or the
     fork mount and the s3 config get clobbered. Methodologically free: infrastructure, identical for all
     arms, same argument as MinIO.

*Before q11 OOM'd, its first episode read `busy=38.92 ms/s cv=0.739 creditable=1` vs q5's 1-15 and
uncredited — q11 IS ~4-30x heavier and worth retrying once the TMs have memory.*

**MEMORY FIX APPLIED AND IT WORKED (2026-08-06).** `taskmanager.memory.managed.size: 16m` patched onto
the live TM deployment (strategic merge rewriting only that property, so the fork mount and the s3
settings survived). Managed memory is for RocksDB/batch/Python and the job uses `HashMapStateBackend`,
so those 230 MB were reserved and idle. Result: `task.heap.size` **26843542b → 251658240b (25.6 MB →
240 MB)**, `-Xmx` 153 MB → 368 MB, WITHOUT extra RAM. Note the real memory ceiling, which corrects an
earlier reading of `kubectl top nodes`: the three "nodes" are docker containers on ONE WSL VM
(7.7 GiB total) and each is capped at `HostConfig.Memory = 2 GiB`, so raising `process.size` past
~1024m for 5 TMs does NOT fit. Next escalation if ever needed: 3 TMs (one per node) at `process.size:
1792m` ≈ 900 MB task heap.
- q11 rerun after the patch: **0 TM restarts, all jobs RUNNING, 17/17 episodes credited**, busy
  61-185 ms/s (vs q5's 1-15). The harness end-to-end is now proven.

**THE CAMPAIGN CANNOT ANSWER "DO THE ARMS DIFFER" — MEASURED, NOT SUSPECTED (2026-08-06).** Grouping
the q11 episodes by the rescale configuration they occurred in:

    slices/freeSlots   n   reward   sd      arms
    8/8                6   0.569    0.025   all six (and slices==freeSlots ⇒ NO choice)
    4/6                2   0.567    0.011   FCFS, GA
    2/6                3   0.438    0.005   FCFS, ROUND_ROBIN, LEAST_LOADED
    2/4                2   0.440    0.008   LEAST_LOADED, GA
    2/8                3   0.411    0.022   STOCK, STOCK, ACO

Between strata ≈ 0.14; between arms inside a stratum 0.005-0.025. Globally, within-arm sd 0.0715 is
**4.7x** the between-arm sd 0.0151. `analyse_fork_campaign.py` reported "spread 0.0469 (9.5%),
best=GA, worst=ACO" — that is the autoscaler having handed each arm a different MIX of configurations,
not a placement effect. **The reward tracks the configuration, not the arm.** This is the reward
confound already recorded above, now quantified.
- The `2/6` stratum is the cleanest datum: FCFS 0.438, ROUND_ROBIN 0.432, LEAST_LOADED 0.443 — three
  genuinely different policies, same configuration, **sd 0.005**.
- `analyse_fork_campaign.py` ALSO recommended "train on q5-sine (27.9% spread)" — computed from the
  broken idle run where 3 of 6 arms had ZERO credited episodes. It ranks scenarios higher the LESS
  valid data they have. Do not follow its recommendation without checking `credited` per arm first.

**NEW CONTROLLED EXPERIMENT BUILT (2026-08-06), not yet run:**
`scripts/run-placement-experiment.sh` + `scripts/analyse_placement_experiment.py`.
- One fixed transition for every arm (submit at SUBMIT_PAR=8, force parallelism=TARGET_PAR=4 with
  `lowerBound == upperBound` while the pool still holds 8 ⇒ same `slices`, same `freeSlots`, same
  decision), repeated REPS times. No autoscaler. `DIST=CONSTANT` because a shaped rate is a nuisance
  variable here.
- **ONE JOB PER ARM, repetitions inside it — the user pushed back on interleaving all arms in one
  shared job and was right:** the PREVIOUS placement is an INPUT to the assigner (`PlacementInstance`
  scores locality against `previousAllocations`; STOCK delegates to `StateLocalitySlotAssigner`, which
  anchors slices to the slot already holding their state). Sharing a job would have arm B deciding
  against the layout arm A left. Per-arm jobs keep the carryover inside an arm and give every arm the
  same initial condition. The cost — losing control of drift across the run — is the lesser evil.
- Measurement is NOT reimplemented: `arm_controller.py --observe` (records, publishes nothing, no
  Q-table) so the reward is the same code the metas use, crediting the arm the JM LOGGED.
- The analyser stratifies, prefers a stratum where `freeSlots > slices` (a stratum with
  `slices == freeSlots` forces every arm into the same placement and only measures noise), reports
  between-arm vs within-arm spread, and runs a **permutation test** (right call at n=3 — no normality
  assumption, no scipy). Validated against the q11 campaign data: correctly returns p=0.66,
  "not distinguishable from noise". Also reports throughput/slot, busy, cv_busy, backpressure per arm
  (user asked for throughput/core; slots ≈ cores here, `taskmanager.cpu.cores=2.0` with 2 slots/TM).
- REPS=3 ≈ 45 min (matches the earlier pilot's budget); **REPS=8 ≈ 2 h is the n that can actually
  separate arms** given a within-stratum sd of ~0.02.

**FIRST RUN OF THE CONTROLLED EXPERIMENT (2026-08-06, REPS=3, q11/CONSTANT/60k, 8->4): the arms DO
appear to differ — but the run also exposed a crediting bug, so re-run before believing it.**
- Result: inside the `4/8` stratum, between-arm sd 0.0650 vs within-arm 0.0155 (the campaign's ratio
  INVERTED), range 0.182, permutation p = 0.0281. Direction partly as predicted — the spreading arms
  (ROUND_ROBIN 0.580, ACO 0.571, LEAST_LOADED 0.555) above the anchoring ones (FCFS 0.486,
  STOCK 0.471) — but GA is the worst (0.398) and breaks the story.
- Mechanistic signal that is NOT affected by the bug (`tms_hosting` comes from the metrics): STOCK
  spreads over 2-3 TMs, FCFS 3, RR/LL/ACO/GA 4. The arms visibly do different things.
- **WHY NOT TO TRUST THE p YET:** n per arm came out 3,3,1,3,2,1 — the best (ROUND_ROBIN) and worst
  (GA) arms are both n=1, so the range driving the test rests on two single observations.
- **THE BUG (fixed 2026-08-06 in `arm_controller.py`):** the CSV row took `slices` from
  `measurement` (REST, the parallelism that actually ran) but `free_slots` and `had_choice` from
  `applied_arm()` (the last `THESIS_ASSIGN` log line). Because the adaptive scheduler re-invokes the
  assigner SPECULATIVELY, that last line is often a later what-if for a different width. Result:
  impossible rows (`slices=4 free=8 had_choice=False`, and `8/8 had_choice=True`) — and `had_choice`
  is the gate that decides crediting, so valid episodes were dropped and degenerate ones credited.
  FIX: `applied_arm(..., want_slices=measurement["slices"])` anchors on the last log line whose slice
  count matches what was measured (falling back to the last line), and the row now takes `slices` from
  that same record. Affects every episodes CSV written before this date.
- The campaign conclusion above still stands: its within-vs-between comparison (0.0715 vs 0.0151) is
  computed on rewards and does not depend on the stratum labels.

**SECOND CONTROLLED RUN (2026-08-11, REPS=8, q11/CONSTANT/60k, 8->4). The 6-way test is NEGATIVE and
trustworthy; the planned contrast is not.**
- 36 episodes, all six arms, one stratum. Six-way permutation p = 0.31 on reward (0.27 tput/slot,
  0.29 cv_busy, 0.43 busy). Within-arm sd 0.060 > between-arm sd 0.038 — repeated measurements of the
  SAME arm under the SAME configuration disagree more than the arms disagree. Refining the stratum by
  `tms_available` does not rescue it (p = 0.24 within `4/8/4`, n=32).
- **The 2026-08-06 p = 0.028 was a false positive** driven by the two extreme arms being n=1.
- **PLANNED CONTRAST — spreading (ROUND_ROBIN+LEAST_LOADED) vs anchoring (STOCK+FCFS), inside `4/8/4`:**
  reward 0.578 vs 0.493, **diff +0.085, p = 0.0033** one-tailed. The same ordering appeared in BOTH
  independent runs. **Caveat: the grouping was chosen AFTER seeing the ordering** — it is motivated by
  the mechanism, not fitted to the data, but it must be pre-declared and re-run before it counts.
- **AND THE TENSION THAT MATTERS MOST: throughput/slot goes the OTHER WAY** — anchoring 10116 vs
  spreading 9508 (-6%), p ≈ 0.09, i.e. NOT significant, so do not claim anchoring wins. What CAN be
  claimed is that better balance is not buying throughput. The reward the metas optimise
  (dispersion of per-TM busy) does not correlate with the only thing a Flink user cares about, and may
  trade against it — plausibly because anchoring preserves state locality. `arm_controller.py` already
  supports `--reward {dispersion,throughput,blend}`; this is now a live design question, not a detail.

**TWO ADDITIONS THE USER ASKED FOR (2026-08-11):**
1. **End-to-end delay**, in `arm_controller.py` and the analyser: `e2e_delay_ms` =
   `wall_clock - min(currentInputWatermark over sink subtasks)`, plus `e2e_delay_spread_ms` =
   max-min across those subtasks. The MINIMUM is deliberate — event time is only trustworthy up to the
   slowest subtask, and a mean would let a fast one mask the straggler an imbalance creates.
   `Long.MIN_VALUE` (no watermark yet) is filtered or it injects a ~2.9e11 s delay into every mean.
2. **Up AND down scaling in one run.** `SCHEDULE="4* 6* 2* 8"` (`*` = measured) makes each repetition
   walk down 8->4, **UP 4->6**, down 6->2, then restore. An up step gets a real decision as long as it
   lands below the retained slot count. Note the earlier design was never "only scaling up" — the
   measured event was always the scale-DOWN, with the up being the restore.

**`slot.idle.timeout` WAS UNSET (Flink default) AND IS PROBABLY A LARGE PART OF THE NOISE.** The
default is shorter than the 80 s measurement hold, so the pool was RELEASING surplus slots while the
measurement ran. That is what made `tmsAvailable` come out 3, 4 and 5 within a single arm and
`freeSlots` decay 8->6->4 in the campaign: the geometry was drifting underneath the window. Set to
**300000** on the JobManager (2026-08-11), sized to cover one measurement cycle and no more.
- **This one is NOT the same kind of change as MinIO, and the user was right to challenge it.** MinIO
  fixed a configuration Flink documents as unsupported. This one picks an OPERATING POINT: surplus
  slots are exactly what give the assigner a decision, so a longer timeout widens the window in which
  ANY placement policy can matter. It does not favour a particular arm (internal validity is fine, all
  six see it), but a reviewer can fairly say the scenario was built to suit the intervention.
- Agreed framing: two claims, two experiments. (1) *Does the policy matter when the assigner has a
  choice?* — needs the knob, it is experimental control. (2) *How often does that choice arise under
  defaults?* — that is the autoscaler campaign, already run. The honest headline is the product of the
  two. Every run now stamps `slot_idle_timeout_ms` into `run.json` so it is reported, not remembered.

**THE QUESTION TO ANSWER FIRST is not "does the meta beat the arms" but "do the arms
differ at all".** Everything downstream is conditional on it, and there is still ZERO evidence either
way — the campaign's apparent spread is the confound above. Two risks to raise with the advisor early: the placement space
is tiny (10 slots) so ACO/GA look oversized for it, and the episode budget (~4-8 per cell, one per
rescale) may only demonstrate the mechanism rather than train a policy.

**THE RESULT (2026-08-13). Placement matters — but ONLY when the TaskManagers are CPU-constrained,
and the four earlier nulls were caused by the platform having no scarce resource at all.**

*The root cause of every null before this:* `resources: {}` on the TaskManager deployment — **no
Kubernetes CPU limit** — on nodes with **12 CPUs each**. `taskmanager.cpu.cores=2.0` is only Flink's
internal accounting figure, not an enforced cap. So two expensive slices sharing one TaskManager got
two separate host cores and never contended. The cluster had 36 cores for 6-10 slots; nothing was ever
scarce, so no placement could ever change throughput. The dispersion reward still moved a lot, because
it counts WHERE the load sits, not whether that costs anything.

*The experiment that isolates it.* `ConfigurableGraphJob` with `PIN_VERTEX="CPU Load"
PIN_PARALLELISM=2 CPU_LOAD=2500`, 3 TMs, `SUBMIT_PAR=6 SCHEDULE="4* 6"`: 4 slices of which exactly 2
carry a CPU-load subtask. On 3 TaskManagers the per-TM dispersion then takes exactly two values —
`sqrt(2)/2 = 0.707` when the expensive pair is split, `sqrt(2) = 1.414` when it is not — so the
placement is readable straight off the metric with no inference.
- WITHOUT a CPU limit: dispersion differs hugely (reward 0.586 vs 0.414, within-arm sd 0.0003,
  p=0.0001) and throughput/e2e differ by NOTHING (p=0.89 / p=0.23).
- WITH `kubectl set resources deployment/flink-taskmanager -n flink --limits=cpu=1`:
  **spreading beats concentrating by +38% throughput/slot and −14% e2e delay, p=0.0007.**
- **The confound-free version:** FCFS happened to split the pair in 4 repetitions and concentrate it
  in 4. WITHIN FCFS — same arm, same job, same config — spreading gave +38.4% tput and −15.5% e2e,
  p=0.0292, which is the FLOOR of a 4-vs-4 permutation test, i.e. perfect separation. And FCFS *when
  it spreads* is statistically identical to LEAST_LOADED (tput p=0.96). So the effect is the
  PLACEMENT, not the arm's identity.

*The claim this supports* — stronger than "my scheduler wins" because it states a condition:
> Slot placement changes throughput exactly when TaskManagers are CPU-constrained relative to their
> slot count. Under that condition, concentrating two expensive slices on one TaskManager costs ~38%
> throughput and ~15% latency. Without the constraint the same decision has no measurable effect.

*Analysis tooling:* `scripts/analyse_placement_experiment.py --by-placement` now does this
automatically — splits episodes by the dispersion actually observed, contrasts pooled and WITHIN each
arm that produced both, and sanity-checks that arms agree wherever they made the same placement.
Comparing by ARM instead dilutes the effect with the arm's own inconsistency (intention-to-treat vs
treatment-delivered).

*Still to do:* n was 12 vs 4; the "concentrated" condition only arose in FCFS and only by accident.
Re-run with `ARMS="LEAST_LOADED ROUND_ROBIN FCFS ACO GA" REPS=10` (ACO and GA also concentrate) to get
~20 per condition. And redo the CPU_LOAD sweep WITH the limit in place — the 2026-08-13 sweep at
2500/10000/40000 was meaningless because `busy` sat at 105.1 ms/s in all three (the two CPU-load
subtasks were already saturated at 2500; 105 ≈ 2/22 × 1000 is the diluted mean over all subtasks).

See [[project_status]], [[project_architecture]], [[feedback_pitfalls]], [[project_nexmark_integration]],
[[project_paper_cetsa]].

**THE ANSWER, FOUND OFFLINE (2026-08-18) — `scripts/placement_bench.py`.** Enumerating the exact
optimum over random instances (graded slice costs from staggered parallelisms; makespan over
machines) gives the mean gap to optimal:

    IDENTICAL TaskManagers        FCFS    ROUND_ROBIN   LEAST_LOADED    LPT
      4 slices / 3 TMs           42.6%         10.1%           0.0%    0.0%
      6 slices / 3 TMs           24.6%          1.7%           0.0%    0.0%
      8 slices / 4 TMs           27.9%          4.4%           0.0%    0.0%

    HETEROGENEOUS (0.5/0.75/1.0)
      4 slices / 3 TMs           82.9%         40.6%          18.9%    2.3%
      6 slices / 3 TMs           39.5%         21.7%          13.6%    3.2%
      8 slices / 4 TMs           40.5%         26.2%          12.9%    2.1%

**On identical machines LEAST_LOADED is EXACTLY OPTIMAL (0.0%) at every size tested.** That is the
mathematical explanation of every null result in this project: the in-cluster instance (4 slices, 3
identical TMs) was already solved by a greedy rule, so no arm could beat another and stock matched
them all. Five controlled runs measured a solved problem.
- Heterogeneous machines open a real gap: LEAST_LOADED leaves 11-19% on the table. Heterogeneity is
  therefore a REQUIREMENT for the thesis to have an object, not an optional extension.
- **BUT LPT (sort by cost descending, then least-loaded) closes it to 0.4-3.4%** — and the fork has no
  such arm. The honest bar for ACO/GA is LPT, not LEAST_LOADED, and the remaining room is a few
  percent. Expect the question "why a metaheuristic when a classic greedy captures 95% of it".
- CHEAPEST NEXT STEP: implement LPT as an arm. Trivial now that per-slice loads are published, and
  likely the best policy the fork would have.
- The bench needs no cluster, which decouples the algorithmic contribution from the WSL rig that has
  been the bottleneck all project. Sizes beyond ~8 slices need pruning: enumeration is O(M^T).

**FIFTH IN-CLUSTER RUN (2026-08-18) — capacity drain also failed to induce the bad layout.** Draining
to 1 TM then to 2 TMs: stock DID inherit the compaction (tmsUsed=2 of 3 available) but paired
slice#0 with slice#2, never #0 with #1. Across five runs the heavy pair was never co-located except
by ACO/GA before per-slice loads existed. Free-slot ordering interleaves them too consistently. Also
worth noting: stock packing into 2 of 3 TMs cost NOTHING (tput 4097 vs 4024) because one heavy plus
one light per TM is balanced — what matters is never how many TMs are used, only whether the two
expensive slices share one.

**Bench confirmed at 200 instances/cell (2026-08-18), plus one refinement: at 10 slices / 4 identical
TMs the gap stops being zero — LEAST_LOADED and LPT both reach 1.6%.** So the identical-machine case
is not trivial by nature, only trivial AT THE SIZE THE CLUSTER RUNS. Size and heterogeneity are two
independent levers and heterogeneity is far the stronger: 9-14% for LEAST_LOADED at only 4-10 slices,
versus 1.6% from size alone. LPT stays at 1.6-3.1% throughout, so it remains the bar to beat.

**USER'S FRAMING DECISION (2026-08-18), reaffirmed: THE BASELINE IS ALWAYS STOCK.** The thesis
contribution is measured against Flink's default assigner, not against any reference heuristic. Do not
re-frame the goal as "close the gap to optimal" or "beat LPT" — those are diagnostics, not the claim.
- Keep the two roles separate: STOCK is the BASELINE the contribution must beat. LPT is only a
  REFERENCE showing how much of the available gap a three-line greedy already captures (1.6-3.1% left
  after it), i.e. the anticipated reviewer question "why a metaheuristic and not a simple rule".
  Implementing LPT as an arm is worth doing because it would likely be the fork's best policy — but it
  is an arm to offer, not the thing to be compared against.
- Practical consequence: every experiment must include STOCK in ARMS. It was missing from every run
  between 2026-08-13 and 2026-08-17 (the ones that produced the +45% figure), which is why that number
  compared fork arms to each other rather than to Flink.

---

**PHASE 4 — LPT ARM + HETEROGENEOUS CLUSTER (2026-08-18, implemented and deployed).**

The two levers the bench identified are now both real in the cluster.

*What was added to the fork* (`~/projects/flink-custom-scheduler`, compiled, 25/25 tests green):
- `Strategy.LPT` — longest-processing-time-first over the published slice loads, placing each
  slice on the TaskManager with the smallest `(load + cost) / speed`. First arm that is both
  load-aware AND speed-aware.
- `/var/thesis/speeds` (`THESIS_SPEEDS_FILE` / `thesis.speeds.file`), one
  `<taskManagerResourceId> <cores>` line each, read exactly like `/var/thesis/loads` (1s cache,
  last-good-copy on failure). Unlisted TaskManagers default to 1.0.
- `PlacementInstance` is speed-aware: the balance term now aims at a share PROPORTIONAL to speed,
  which reduces to the old formula exactly when speeds are equal, so Phase-2/3 runs stay
  comparable. `THESIS_COST_BALANCE_METRIC=MAKESPAN` switches it to scoring the busiest machine
  instead of dispersion (default stays dispersion).

*Trap worth remembering:* the fork's `LEAST_LOADED` balances FREE SLOT COUNT, not load — it is
NOT the bench's `h_least_loaded`, which balances `cost/speed`. The bench's 13.7% heterogeneous
figure therefore describes a rule the fork did not have until LPT. Do not read across.

**NEW BENCH FINDING — the greedy arms' heterogeneous gap is mostly a TIE-BREAK LOTTERY.** Same
machines (1.0 / 0.75 / 0.5), only their ORDER changed, 4 slices / 3 TMs / 6 slots:

| TM ordering | FCFS | ROUND_ROBIN | LEAST_LOADED | LPT |
|---|---|---|---|---|
| fastest first | 29.5% | 4.1% | 1.3% | 1.3% |
| fastest in the middle | 65.1% | 29.4% | 8.8% | 1.3% |
| fastest last | 147.6% | 90.8% | 43.2% | 1.3% |

At zero load every greedy ties and takes the first machine in the caller's order, and on unequal
machines that first choice is unrecoverable. LPT is flat because it commits the largest slice to
the machine that would FINISH it soonest. The fork orders TaskManagers by resource id, which is
`<podIP>:<port>-<hash>` — so on a heterogeneous cluster the quality of every greedy arm is decided
by which pod got which IP. That is most of the case for LPT, and it is a result in its own right.

**CLUSTER STATE (2026-08-18).** `flink-taskmanager` scaled to 0; three classes live:
`flink-tm-fast` (2 cores), `flink-tm-medium` (1500m), `flink-tm-slow` (1), one replica each, 2
slots each — six slots, same as before, so ONLY speed changed. Ratios are the bench's
(1 : 0.75 : 0.5) scaled so the slowest class equals the TaskManager every homogeneous run used.
Switch with `scripts/set-taskmanager-classes.sh --heterogeneous | --homogeneous | --status`.

**CAUTION FOR THE FIRST CAMPAIGN:** the pods came up as .24 (fast) / .25 (medium) / .26 (slow), so
the FASTEST machine sorts FIRST — the single most favourable ordering for the greedy arms, where
LEAST_LOADED sits at 1.3% instead of 43%. A campaign run only in this state will UNDERSTATE LPT.
Run both orderings (recreate the class deployments in reverse order to flip the IPs) or report the
ordering alongside the result; `run.json` now records `taskmanager_speeds` in resource-id order,
which is exactly the assigner's own ordering.

*New/changed scripts in the main repo:* `scripts/publish-speeds.sh` (derives speeds from the pods'
CPU limits, sorted by resource id), `scripts/set-taskmanager-classes.sh`,
`kubernetes/flink-taskmanager-classes.yaml`, plus `TM_DEPLOYMENTS` / `DRAIN_DEPLOYMENTS`
("<deployment>:<replicas>" pairs) in `run-placement-experiment.sh` so a campaign can hold a
multi-deployment cluster and drain to the slow machine only.

**FIRST HETEROGENEOUS CAMPAIGNS (2026-08-18) — the ordering prediction HELD in the cluster.**
Two campaigns, identical in everything but the pod IPs: `20260818-171233` (fast TM sorts first,
speeds 2.0/1.5/1.0) and `20260818-182749` (slow first, 1.0/1.5/2.0). 4 arms × 8 reps, q11/CONSTANT
60k, 6->4, no drain, ConfigurableGraphJob CPU_LOAD=2500, "CPU Load" pinned at parallelism 2.

Throughput/slot, and what happens to each arm when the ordering flips:

| arm | fast-first | slow-first | swing | p (perm) |
|---|---|---|---|---|
| STOCK | 3726 | 3915 | +5.1% | 0.21 |
| LEAST_LOADED | 4136 | 3739 | **-9.6%** | **0.030** |
| LPT | 4150 | 4076 | -1.8% | 0.62 |
| ACO | 4243 | 4217 | -0.6% | 0.87 |

Only the count-based greedy moves. Within the favourable order nothing separates
LEAST_LOADED/LPT/ACO (p=0.89 / 0.41) — the tie-break hands LEAST_LOADED the right answer for free,
which is why a campaign run only in that state would have concluded LPT adds nothing. Within the
adverse order ACO beats LEAST_LOADED by 12.8% (p=0.016) and LPT by 9.0% (p=0.10, suggestive only).
STOCK is 9.9% below LEAST_LOADED in the favourable order (p=0.008) and indistinguishable in the
adverse one.

CAVEATS to carry into the write-up: n=8 per cell; ~10 tests run with no multiplicity correction, so
only the STOCK deficit and the LEAST_LOADED swing are comfortable. ACO edges LPT in both orders
(+2.2%, +3.5%, not separated statistically) — consistent with the bench's claim that LPT still
leaves 1.6-3.1%. The `reward` column is saturated at 0.585-0.586 and discriminates nothing; use
`throughput_per_slot`. `analyse_placement_experiment.py` runs its permutation test on `reward`, so
its verdict line understates what the campaign actually shows.

FIXED: `run.json` stamped `tm_replicas: 5` on a 3-TaskManager cluster (TM_REPLICAS describes the
uniform pool only). It is now derived from TM_DEPLOYMENTS; both campaign records were corrected.

**LPT vs LEAST_LOADED CLOSED (2026-08-19, `20260819-123154`, adverse order, REPS=20).** The
comparison that sat at p=0.10 with n=8 is now decisive. Throughput/slot: LEAST_LOADED 3736,
**LPT 4270 (+14.3%, p<0.0001)**, ACO 4108 (+10.0%, p=0.005). Pooled with the 18-Aug adverse
campaign (n=28): LPT +12.8%, ACO +10.8%, both p<0.0002.

**ACO NO LONGER BEATS LPT.** Today ACO landed 3.8% BELOW LPT (p=0.26) after sitting 3.5% above it
yesterday — the sign flips between campaigns and neither is significant. The honest reading is that
they are indistinguishable at this problem size, which matters: LPT is a fifteen-line greedy and ACO
is a search. Their tie leaves the metaheuristic's cost unjustified here.

*A claim NOT to make:* ACO's sd is nearly double LPT's (527 vs 295), which looks like worse
consistency. It is not — a Brown-Forsythe permutation test (deviations from each group's own median)
gives p=0.77. The sd comes from ONE outlier episode: ACO's min is 2108 while its own first quartile
is 4124. Medians: LPT 4348, ACO 4211, LEAST_LOADED 3784.

**Harness reproducibility confirmed across a reboot:** LEAST_LOADED gave 3739 (18 Aug) and 3736
(19 Aug), p=0.99, despite new resource ids, a nodeSelector and a full restart.

**ORDERING IS NOW DECLARED, NOT DRAWN.** Each speed class carries an explicit
`taskmanager.resource-id` (`tm-1-slow` / `tm-2-medium` / `tm-3-fast`), so the experimental condition
survives reboots and is chosen with `set-taskmanager-classes.sh --heterogeneous --order
slow-first|fast-first`. This replaced an IP lottery that produced the WRONG condition three times in
a row on 2026-08-19 — once because "10.244.0.10" sorts before "10.244.0.9" in the assigner's TEXT
ordering. `publish-speeds.sh` now reads the pod IP from the RPC path, since the id no longer holds
an address. The class deployments are also pinned to the control-plane node
(`nodeSelector: kubernetes.io/hostname: minikube`): left free they spread over the three minikube
nodes, which changes network topology AND removes sibling CPU contention — and those three "nodes"
are one 12-core WSL host anyway, so spreading buys no real hardware heterogeneity.

**ROBUSTNESS CHECK PASSED (2026-08-19, `20260819-153336`, adverse order, TMs SPREAD over the three
minikube nodes, n=12).** LEAST_LOADED 4002, STOCK 4102, **LPT 4416**. LPT beats LEAST_LOADED by
+10.3% (p<0.0001) and **STOCK by +7.7% (p=0.0005)** — the baseline claim now holds under two
topologies. The headline advantage shrinks from +14.3% (pinned) to +10.3% (spread) but stays
decisive.

Every arm gained from spreading — LEAST_LOADED +7.1% (p=0.003), STOCK +4.8% (p=0.056), LPT +3.4%
(n.s.). Plausible mechanism (hypothesis, not proven here): co-located TaskManagers compete with
their siblings, and removing that contention returns the most to the arm whose placement was worst,
which is why the gap narrows. The confound I flagged in advance — the slow TM landed on `minikube`,
the node also hosting JM/MinIO — did not bite: it would have made the spread condition worse, and
all three arms improved.

STOCK vs LEAST_LOADED stayed +2.5% (p=0.13), same sign as the pinned adverse run (+4.7%, p=0.29):
the one rank inversion found so far is driven by TM ORDERING, not by topology.

**GOTCHA DISCOVERED: the custom `adaptive-scheduler` does NOT honour `nodeSelector`.** It binds pods
by its own strategy and the kubelet then rejects the mismatch with `Failed / NodeAffinity`; the
controller retries until the scheduler happens to pick the right node — 14 attempts for
`flink-tm-medium` on 2026-08-19. So pinning worked by attrition, not by construction, and leaves a
pile of Failed pods (`kubectl delete pods -n flink --field-selector status.phase=Failed`). The clean
fix, not applied yet to avoid changing a variable mid-experiment, is `schedulerName:
default-scheduler` on the three class deployments.

**META-SCHEDULER STATUS — the premise is only half-supported.** Arms demonstrably differ, but a
meta-scheduler needs the RANKING to change with context, and only ONE inversion exists: STOCK vs
LEAST_LOADED across TM orderings (interaction test p=0.010; LEAST_LOADED ahead by 411 with the fast
machine first, STOCK ahead by 176 with the slow machine first). It is between two arms that **LPT
dominates in every condition measured**, so a learner over {STOCK, LEAST_LOADED, LPT, ACO} would
just learn "always LPT". A real justification needs an inversion touching the frontier.

*The principled candidate, and it is the paper's own tension:* LPT ignores state locality entirely by
design, and the test job (ConfigurableGraphJob, synthetic CPU load) carries no real state, so
ignoring it is free. With a genuinely stateful job — Nexmark q5 (sliding window) or q8 (join) —
rebalancing means moving key groups, and LPT should lose to STOCK/ACO. ACO is the only arm with both
terms in its cost (`THESIS_COST_BALANCE` / `THESIS_COST_LOCALITY`), which would make it a frontier
competitor rather than a slower LPT. Li et al. report the same tension: CETSA has LONGER execution
time and HIGHER SLA violation than round-robin because it packs densely, and LBA-CE exists to manage
that trade-off — the paper itself says no single policy dominates. NEXT EXPERIMENT: repeat the
campaign with JOB_CLASS switched to a stateful Nexmark query, everything else held.

**RESCALE COST NOW MEASURED (2026-08-19).** `arm_controller.py` gained four episode columns —
`restart_gap_s`, `recovery_s`, `rescale_deficit_events`, `recovery_samples` — filled from a rolling
history of the source vertex's cumulative `write-records` (one REST call per sample via
`/jobs/{id}`, deliberately NOT `sample_once`, which costs ~30 calls and would perturb the very
recovery it observes). `RECOVERY_INTERVAL` (default 1s) in the campaign script sets the resolution.

First numbers (`20260819-175748`, LPT, n=3 creditable episodes): a rescale costs ~106k events
≈ **6.9 s of full production**. With LPT's measured 14% advantage that amortises in **~49 s**
(10% → 69 s, 5% → 137 s). ORDER OF MAGNITUDE ONLY at n=3; want 12-20 episodes before writing it.

*Reading:* the placement hold is 80 s, so a rescale pays for itself inside its own hold — a
cost/benefit GATE would only matter with rescales more often than about once a minute, which bounds
the meta-scheduler on that axis rather than extending it. The sharper form: Flink's `shouldRescale`
only fires on a PARALLELISM change, so "same parallelism, better placement" is not expressible —
fixing a bad layout costs a full restart. At 14% gain that is worth it whenever the job has >49 s
left, i.e. always in streaming. The design rule is the INVERSE of the restrictive gate we expected,
and it is now quantified.

*Three instrumentation bugs found and fixed in sequence, worth not repeating:* (1) integrating the
deficit past recovery absorbed the NEXT rescale's transient; (2) `measure()` blocks for the whole
window without polling `epoch_key`, so a rescale landing inside it was detected up to `window`
seconds late; (3) the trace was reset at DETECTION, so late detection discarded the transient —
precisely on the creditable epochs. Fix: one rolling `records_history` in absolute time, never
reset, sampled unconditionally, sliced by the epoch's own start time.

**Flink's rescale gate is `EnforceMinimalIncreaseRescalingController` and it is nearly a no-op.**
`parallelismChange < 0 || parallelismChange >= minParallelismIncrease` — every scale-DOWN is accepted
unconditionally, and with the default `min-parallelism-increase = 1` (what this cluster uses) so is
every scale-up. It also only ever sees two `VertexParallelism` maps: it cannot know where slices
would land. Both sides of a placement-aware decision ARE available at the call site
(`AdaptiveScheduler.shouldRescale` has `declarativeSlotPool.getAllSlotsInformation()` and the
`ExecutionGraph`, whose `ExecutionVertex.getCurrentAssignedResource()` gives the current placement),
so a cost-aware controller is implementable without restructuring Flink. This is the paper's SC
(scheduling cost) term, one of the declared deviations.

**STATE LOCALITY IS INERT AT RESCALE — verified in source, 2026-08-19.** `state.backend.local-recovery`
defaults to false (CheckpointingOptions.java:153) and is unset here; and even enabled it would not
help, because `PrioritizedOperatorSubtaskState` only approves a local alternative when its key group
range is EQUAL to the JobManager's (`eqStateApprover(KeyedStateHandle::getKeyGroupRange)`), and every
range changes at a rescale. So `StateLocalitySlotAssigner` (STOCK) maximises a quantity the runtime
cannot cash, and ACO's locality term is a distractor — which explains ACO ≈ LPT. A stateful-job
campaign at rescale would come out null for this platform reason, NOT a job reason.

**RESCALE COST, PROPERLY MEASURED (2026-08-19, `20260819-180710`, LPT, n=18 creditable episodes,
57-59 samples each).** Median downtime **7.5 s** (range 2.2-11.3), median lost production **5.2 s**
equivalent, median payback against LPT's 14.3% advantage **36.6 s**. The placement hold is 80 s, so
a rescale pays for itself with more than 2x margin — the restrictive gate has no case in this
regime; the quantified rule is its inverse: fixing a bad layout costs ~7.5 s of downtime and is
recovered in ~37 s, so a controller should force re-placement whenever the job has >~40 s left.

*Structure of the number:* deficit ≈ (restart_gap − 1.8 s) × rate, i.e. the cost of a rescale IS its
downtime; the 1.8 s offset is sampling granularity. `restart_gap_s == recovery_s` in all 18 episodes,
confirming the generator is fixed-rate: it stops and resumes at full rate with no ramp. The three
zero-deficit episodes are NOT free rescales — they are the three shortest gaps (2.2/3.3/3.6 s), where
the source makes up the shortfall inside one sampling interval. A finer RECOVERY_INTERVAL would
remove them.

*Open thread worth one look:* the gaps look BIMODAL (~2-5 s vs ~7-11 s) and it does not alternate
with the transition, since every creditable episode is the same 6->4. Candidate cause: some rescales
wait out `jobmanager.adaptive-scheduler.resource-stabilization-timeout` (10 s here) and others do
not. If that is it, the knob halves the cost of a rescale and is directly actionable.

**RESCALE COST DOES NOT DEPEND ON THE ARM (2026-08-20, `20260820-144438`, STOCK/LEAST_LOADED/LPT,
REPS=14, n=14 creditable each).** restart_gap differences of 0.23-0.58 s, all p >= 0.58, on a mean of
~7 s. Predicted from the source before running: `Executing.maybeRescale()` logs "Can change the
parallelism of job. Restarting job." and calls `goToRestarting(...)` — Flink 1.18 has NO partial
rescale, every task is cancelled and redeployed regardless of where the assigner puts it. So
"STOCK preserves the layout, therefore it is cheaper to apply" is FALSE here, and that objection can
now be answered with data. Honest phrasing: with n=14 and sd ~2.8 s this detects ~2.5 s, so the claim
is "no difference larger than ~2.5 s", not "no difference".

*Throughput replicated with a caveat:* LPT +6.7% over LEAST_LOADED (p=0.021) and +10.9% over STOCK
(p=0.0006) — same direction as the 14.3% of `20260819-123154` but noticeably smaller. The effect size
oscillates between campaigns; report it as a range, not a single figure.

*Limitation to declare:* the network-connection hypothesis (fewer TaskManagers used -> fewer Netty
connections -> faster restart) was NOT tested — all three arms used all 3 TaskManagers in all 42
episodes, so there was no variation. `TARGET_PAR=3` would create the contrast (LPT concentrates on 2
TMs, LEAST_LOADED spreads over 3, verified in the 2026-08-18 smoke test).

**THE BIMODAL RESTART GAPS ARE NOT `resource-stabilization-timeout` (checked 2026-08-20, read-only,
n=60 pooled).** The earlier hunch is WRONG and should not be repeated. The histogram does dip at
6-7 s between clusters at 3-6 s and 7-12 s, but three things rule out two distinct rescale paths:
the modes differ by ~5 s, not the 10 s of the timeout; the sequence within a run is a repeating
SAWTOOTH with the same shape and phase across independent jobs of different arms (LEAST_LOADED
4.2 4.0 10.6 10.0 9.3 7.5 5.7 3.9 9.0 8.0 6.9 5.2 4.1 3.3 vs STOCK 5.6 3.0 11.7 10.4 9.4 7.1 5.5 4.6
11.5 10.1 7.4 5.0 4.5 4.0); and the gap correlates **+0.97 / +0.93** with the throughput reached
afterwards for LEAST_LOADED / LPT (STOCK only +0.14, unexplained). So it is a slow environmental
oscillation being sampled, not a binary decision — there is no knob here that halves the rescale cost.

*Mechanism unidentified.* Plausible and unverified: periodic CPU contention on the WSL host, MinIO
checkpoint accumulation/cleanup cycles, GC cycles. NOT worth chasing — environment noise on a fragile
host, and the median stays usable.

*Consequences for the cost model, both to carry into the write-up:*
- `deficit_seconds = restart_gap - 2.08 s` (sd 0.52) — tight and reliable; the offset is sampling
  granularity.
- Payback does NOT reduce to a constant: it correlates +0.52 with the throughput regime, because the
  gap itself does. At a 10% gain the median is 41 s with a range of 10-94 s. **Report payback as a
  median with a range, never as a point estimate**, and declare that the cost of a rescale is not a
  system constant.
