---
name: project-flink-2x-migration
description: "Migration from the Flink 1.18 fork to Flink 2.3.0 — what breaks, and the finding that Flink 2.3 now ships load balancing but still assumes identical machines."
metadata:
  type: project
---

**Decision 2026-08-20 (Vicente): migrate the fork from Flink 1.18.0 to 2.3.0.** Reason: working on
1.18 risks the contribution, and one finding already turned out to be a known bug fixed upstream.
Flink 2.3.0 is the latest stable (released 25 June 2026).

**THE POSITIONING FINDING — Flink 2.3 now balances load itself, but still assumes identical
machines.** `org.apache.flink.runtime.scheduler.adaptive.allocator` gained, selectable via
`TaskManagerOptions.TaskManagerLoadBalanceMode`:
- `SlotsBalancedSlotMatchingResolver` — balances slot utilization per TM (≈ the thesis's LEAST_LOADED)
- `TasksBalancedSlotMatchingResolver` — balances `TaskExecutionLoad` per TM (closer to LPT)
- `TaskBalancedSlotSharingResolver` — balances tasks when FORMING the sharing groups

But `TaskExecutionLoad`'s own javadoc says it "is **not** a direct measurement of runtime resource
pressure (for example CPU or memory utilization) on a TaskExecutor", and `DefaultTaskExecutionLoad`
is a single declared `float loadValue`. No `ResourceProfile`, `cpuCores` or capacity anywhere in the
matching resolvers.

*So the thesis's contribution survives and sharpens:* Flink balances with a DECLARED weight over
EQUAL machines; the fork balances with MEASURED `busyTimeMsPerSecond` over machines of declared
different speed. That is exactly Li et al.'s premise, still unmodelled upstream. Consequences:
- **The baseline becomes Flink 2.3 with `TaskManagerLoadBalanceMode` ON** — a much harder and more
  credible comparison than 1.18's `DefaultSlotAssigner`.
- ROUND_ROBIN and LEAST_LOADED become redundant with upstream; keep them as a control that the
  implementation reproduces upstream behaviour.
- **LPT + the speeds vector is the novel part**, now cleanly isolated.

**PORT SPEC (fully determined from the 2.3.0 source, not javadoc).** The fork is almost purely
additive: 5 new files plus 14 changed lines in `SlotSharingSlotAllocator`. Changes needed:
1. `SlotAssigner.assignSlots` takes `Collection<PhysicalSlot>` (was `Collection<? extends SlotInfo>`).
   `PhysicalSlot extends SlotInfo`, so the internals survive.
2. `DefaultSlotAssigner.createExecutionSlotSharingGroups(...)` is GONE. Use
   `slotSharingResolver.getExecutionSlotSharingGroups(jobInformation, vertexParallelism)`, which
   returns `Collection<SlotSharingSlotAllocator.ExecutionSlotSharingGroup>` — same class, so
   everything downstream of it survives, and the fork's group-building loop collapses to one call.
3. `ThesisSlotAssigner` needs a constructor taking `executionTarget`,
   `minimalTaskManagerPreferred`, `SlotSharingResolver`, `SlotMatchingResolver` and
   `localRecoveryEnabled` — all available at the wiring point.
4. STOCK must mirror 2.3's REAL rule (FLINK-36201):
   `localRecoveryEnabled && !previousAllocations.isEmpty() ? new StateLocalitySlotAssigner(resolver)
   : new DefaultSlotAssigner(executionTarget, minimalTaskManagerPreferred, resolver, matchingResolver)`
5. `JobInformation` only GAINED methods (`getCoLocationGroups`, `getVertexName`,
   `getCoLocationGroup`, `getVertexParallelismStore`) — nothing the fork uses was removed, but the
   fork's own test doubles must implement the new abstract ones.

**THE REAL COST IS NOT THE FORK — IT IS THE BENCHMARK JOBS.** Flink 2.0 REMOVED `SourceFunction`,
`RichSourceFunction`, `SinkFunction`, `RichSinkFunction`, and `addSource()`/`addSink()`. All four
jobs use them: `ConfigurableGraphJob`, `NexmarkRealJob`, `NexmarkBenchmarkJob`, `NexmarkGenerator`.
They must be rewritten to Source V2 / Sink V2. Opportunity rather than pure cost: replacing the
hand-written `BidSourceFunction` with Flink's `DataGeneratorSource` + `RateLimiterStrategy` makes
the workload the framework's instrument instead of the student's own code; SINE/STEP/RAMP become a
small custom rate-limiter class.

**Estimate:** fork port ~2-3 h; job rewrite ~1 day+; pipeline/manifests ~1 h; revalidation ~1 day of
runs. Total 2-4 days. **Every measurement to date must be re-run** — they are not comparable across
engine versions.

**Repo strategy:** the fork is a SQUASHED import (`55903fde Apache Flink 1.18.0 (upstream import)`),
so there is no shared history with apache/flink and `git rebase` onto a 2.x tag is impossible. Create
a branch `flink-2.3` with a fresh import commit and LEAVE `main` on 1.18 so every existing result
stays reproducible.

**Environment:** Flink 2.x minimum is Java 11 (Java 8 dropped, 17 recommended) — the machine's
JDK 11 suffices to build. Source tarball downloaded to the session scratchpad (47 MB).

Related: [[project_flink_core_fork]], [[project_paper_cetsa]].

---

**PORT DONE AND GREEN (2026-08-20). 25/25 tests pass on Flink 2.3.0**, branch `flink-2.3` of
`~/projects/flink-custom-scheduler`; `main` still holds the 1.18 work at 8d33fcb7. Build: 4 min.

*What actually changed:* `ThesisSlotAssigner` gained the 5-arg constructor and takes
`Collection<PhysicalSlot>`; slices now come from `slotSharingResolver.getExecutionSlotSharingGroups`;
`PlacementInstance` holds `PhysicalSlot`; the test's `FixedLocationSlotInfo` rose from `SlotInfo` to
`PhysicalSlot` (adds `getTaskManagerGateway`, `tryAssignPayload` — `TestingSlot` could NOT be reused
because its `getPhysicalSlotNumber()` is hardcoded to 0 and the assigner's ordering depends on it).
`AntColonyPlacement` and `GeneticPlacement` needed NO changes — they only touch `PlacementInstance`.

**BUILD REQUIRES JDK 17.** Flink 2.x's RUNTIME minimum is Java 11, but the 2.3 BUILD targets 17
(`invalid target release: 17` on JDK 11). Installed openjdk-17; build with
`JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64`. `deploy-thesis-fork.sh` still hardcodes
`JAVA_HOME=.../java-11-openjdk-amd64` (line ~60) and must be updated.

**SEMANTIC CHANGE TO DECLARE — STOCK IS NOT THE SAME BASELINE.** With local recovery off (Flink's
default) 2.3 delegates to `DefaultSlotAssigner`, not `StateLocalitySlotAssigner`. So the
"preserves the inherited layout" arm measured on 1.18 no longer exists under default config. The
green tests do NOT contradict this — they prove the port compiles and the arms are self-consistent,
nothing about baseline equivalence.

**STILL TO DO:** (1) benchmark jobs → Source V2 / Sink V2 (the big one; `SourceFunction`,
`RichSinkFunction`, `addSource`, `addSink` all removed in 2.0); (2) pipeline: JAVA_HOME 17,
`flink-dist-2.3.0.jar`, `image: flink:2.3.0`, `flink-s3-fs-presto-2.3.0.jar` in
`deploy-thesis-fork.sh`, `setup-shared-checkpoints.sh`, `flink-manifests.yaml`,
`flink-taskmanager-classes.yaml`; (3) re-run every campaign.

---

**BENCHMARK JOBS MIGRATED (2026-08-20). All four compile and the uber-jar builds on 2.3.0**
(81.5 MB, was 63 MB).

**CORRECTION TO AN EARLIER CLAIM: `SourceFunction`/`SinkFunction` were MOVED, not deleted.** They
live in `org.apache.flink.streaming.api.functions.source.legacy` and `...sink.legacy` in 2.3 —
the release notes say "removed" because they vanished from their original package. So the Nexmark
jobs needed import changes, not a rewrite: ~30 min, not a day. The compiler proved it, the release
notes misled.

*What was actually done:*
- `ConfigurableGraphJob` — properly ported, because it is the workload every campaign uses. New
  `BidGeneratorSource.java` (FLIP-27 Source V2) **preserving drop-on-full semantics**: producer
  thread at the distribution's rate, bounded queue, events DROPPED and counted when the pipeline
  cannot keep up. `DataGeneratorSource` + `RateLimiterStrategy` was rejected deliberately — it
  limits by BLOCKING, which would couple the arrival process to pipeline capacity and change what
  every throughput number means. New `TrackedConsoleSink.java` (Sink V2); its periodic log moved off
  a daemon thread into the writing thread, since Sink V2 offers no lifecycle hook to stop one and a
  leaked thread outlives the rescale that killed its writer.
- All jobs: `windowing.time.Time` → `java.time.Duration` in window assigners.
- Nexmark jobs: legacy package imports only.
- `flink-nexmark-job/pom.xml`: `flink.version` 2.3.0, `maven.compiler.source/target` 11 → 17.

**JOB GRAPH CHANGED — 6 VERTICES BECOME 5.** `fromSource` takes the `WatermarkStrategy` directly, so
the separate "Watermark Assigner" vertex is folded into the source. Consequences: each slice now
holds one subtask of 5 vertices rather than 6, so the per-slice weights `publish-loads.sh` computes
differ, and no 1.18 campaign is comparable on vertex counts. `PIN_VERTEX="CPU Load"` still resolves —
that name is unchanged.

**REMAINING BEFORE A 2.3 CAMPAIGN:** apply the manifests, redeploy the fork from branch `flink-2.3`
(the deploy script's new version guard will refuse a `main` checkout), re-upload the jar, republish
speeds, and re-run every baseline. The cluster is still on 1.18 and untouched.

---

**CLUSTER MIGRATED TO STOCK FLINK 2.3.0 (2026-08-20).** JobManager and the three speed classes run
`flink:2.3.0`; `/config` reports `flink-version: 2.3.0`; `tm-1-slow` / `tm-2-medium` / `tm-3-fast`
registered with their declared resource ids, so the adverse ordering survived the image change.
`taskmanager.load-balance.mode = NONE`. **This is the baseline: standard Flink, no fork mounted.**

*Two repo↔cluster drifts that broke the first attempt, now fixed in the manifests:*
- `ENABLE_BUILT_IN_PLUGINS` was set LIVE by `setup-shared-checkpoints.sh` and absent from the
  manifest, so `kubectl apply` raised the image to 2.3.0 and left the 1.18 plugin name — every
  container died with "Plugin flink-s3-fs-presto-1.18.0.jar does not exist. Exiting."
- `flink-taskmanager` (the uniform pool) was declared `replicas: 5`, so every `apply` resurrected it
  and undid the class switch's scale-to-zero. Now declared `replicas: 0`.
- The JobManager is now pinned with `nodeSelector: kubernetes.io/hostname: minikube`. It had landed
  on m03, and `/var/thesis` (patched jar, arm file, loads, speeds) is a hostPath on the control-plane
  node — a JobManager elsewhere mounts an empty directory and the fork silently behaves like stock.

**HOW STOCK 2.3 PLACES SLICES (read from source).** With default config it is iteration order on
both sides and nothing else:
1. `DefaultSlotSharingResolver` groups ExecutionVertexIDs BY SUBTASK INDEX out of a `HashMap`, so
   slice i = subtask i of every vertex whose parallelism reaches i, in hash order.
2. `pickSlotsIfNeeded` is a no-op outside application mode with `minimalTaskManagerPreferred`.
3. `SimpleSlotMatchingResolver` does `iterator.next()` per slice over the free slots.
Free slots arrive grouped per TaskManager, so consecutive slices tend to land together — this IS the
thesis's FCFS arm, and it is exactly the "arbitrary placement" Li et al. describe. It is also
non-deterministic run to run (hash order + pool order), which is the variance source measured on 1.18.

**CORRECTION — an earlier claim in this session was WRONG.** I said the 60 s
`executing.resource-stabilization-timeout` would make half the transitions not happen. It does not
apply here: in `Stabilizing`, `onTrigger()` calls `transitionToSubSequentStateForDesiredResources()`
and rescales IMMEDIATELY when desired resources are met, which they are (slots already present, change
arrives by REST). The 60 s only schedules the fallback path to `Stabilized` for when merely
*sufficient* resources exist. And `Cooldown.onChange()` RECORDS the event, so a request landing inside
the cooldown is DELAYED, never lost.

*The real constraint is the 30 s cooldown* (`executing.cooldown-after-rescaling`), counted from the
previous rescale finishing. Effective floor between transitions ≈ 30 s + ~7.5 s restart ≈ 37.5 s.
`WIDE_HOLD=35` sits just inside it, so that transition arrives late every cycle and smears
`epoch_started`. Fix without touching Flink: **`WIDE_HOLD=45`**. Cycle 115 s → 125 s (+9%);
REPS=20 ≈ 43 min per arm.

---

**TWO STOCK-FLINK CONTROLS MEASURED (2026-08-20/21). The `slot.idle.timeout` knob is worth ~7%.**
Both unmodified Flink 2.3, `ALLOW_STOCK_JM=1`, REPS=28, WIDE_HOLD=45, n=28 creditable each.
- Control A `20260820-181344` — factory default (`slot.idle.timeout` inherits the 50 s heartbeat).
- Control B `20260821-145526` — `slot.idle.timeout: 300000` in the JOBMANAGER block.

| metric | A | B | change | p |
|---|---|---|---|---|
| throughput/slot | 3562 ± 435 | 3805 ± 414 | +6.8% | 0.037 |
| restart gap (s) | 9.0 ± 4.1 | 6.5 ± 3.2 | **−27.9%** | 0.012 |
| e2e (ms) | 22486 | 21772 | −3.2% | 0.41 |
| cv_busy | 0.70 | 0.73 | +4.2% | 0.11 |

*Mechanism, visible in the restart gap rather than in `free_slots`:* at the default the surplus slots
are returned to the ResourceManager mid-measurement, so the next rescale has to re-acquire them —
2.5 s more downtime per transition, and the throughput loss follows. With four tests, Bonferroni at
0.0125 keeps the gap result and leaves throughput just outside; report the gap as firm and the
throughput as consistent with it.

**THE BASELINE FOR EVERY ARM CAMPAIGN IS CONTROL B (3805 tput/slot)** — same scenario as the fork
runs. Control A stands as the characterisation of factory Flink and as the measured justification
for declaring the knob.

*Limitation to declare:* with a stock JobManager there are no `[THESIS_ASSIGN]` log lines, so
`arm_controller.py` rebuilds the episode record from REST (`assignment_from_rest`). `free_slots`
there is the cluster's TOTAL slot count and is constant by construction — it cannot show pool
shrinkage, and must not be read as evidence that none occurred.

*A repo trap hit twice now:* `jobmanager.scheduler: adaptive` appears in BOTH the JobManager and
TaskManager blocks of `flink-manifests.yaml`. Any edit anchored on it must target the first
occurrence — `slot.idle.timeout` and `taskmanager.load-balance.mode` are both JobManager-only.

---

**FIRST ARM CAMPAIGN ON FLINK 2.3 (2026-08-21, `20260821-162029` 4 arms REPS=20, plus
`20260821-192126` GA alone). The mechanism is now visible end to end.**

Slices 0 and 1 carry the pinned "CPU Load" vertex, so they are the expensive ones. The
slice→TaskManager mapping from `[THESIS_ASSIGN]` explains every number:

| arm | heavy slices land on | cv_busy | tput/slot | n | vs STOCK | p |
|---|---|---|---|---|---|---|
| STOCK | **both on medium** | 1.411 | 2983 | 20 | — | — |
| LEAST_LOADED | slow + medium | 0.704 | 3484 | 20 | +16.8% | 0.00004 |
| ACO | fast + medium | 0.707 | 3852 | 20 | +29.2% | 0.00002 |
| LPT | fast + medium | 0.707 | 3899 | 20 | **+30.7%** | 0.00002 |
| GA | medium + fast | 0.707 | 4028 | 11 | +35.1% | 0.00002 |

**LPT beats LEAST_LOADED by 11.9% (p=0.0015)** — the comparison that took three campaigns to settle
on 1.18 comes out solid first try. **The metaheuristics TIE with LPT**: ACO +1.2% (p=0.62), GA +3.3%
(p=0.27) — now from TWO independent searches, which is the measured form of the SP-Ant argument
(they lack the communication term and persistent pheromone). See [[project_paper_spant]].

**e2e adds a twist throughput hid:** LEAST_LOADED has the WORST e2e of all (23854, worse than
STOCK's 23499) despite beating it on throughput, because it puts a heavy slice on the SLOW machine
and e2e is set by the straggler. LPT avoids the slow TM and drops to 21029.

**REPORT THE BASELINE AS BIMODAL, NOT AS A NUMBER.** STOCK here = 2983 (heavy slices together);
control B, the same Flink code with no fork, = 3805 (heavy slices split). Stock Flink's placement is
fixed by the slot-pool order at job submission — stable within a run (20/20 and 28/28 identical),
different between runs. So the honest claim is *"stock Flink yields 2983 or 3805 depending on an
order nobody chose, a 27% swing; LPT yields 3899 deterministically"*, not "+30%". That is exactly
Li et al.'s "arbitrary placement", now measured with the mechanism identified.

*A trap this campaign exposed:* per-TaskManager COUNTS (`tms_used`, `spread`) are not enough —
STOCK and GA both show `{medium=2, fast=2}` yet differ by 35%, because the counts hide WHICH slices
went where. Always read the `mapping=` field. I misdiagnosed this mid-analysis as "the campaign
measured the environment" before looking at it.

*Minor:* GA reached only n=11, its job hit RESTARTING at rep 12, and it ran in a separate time
window — its edge over LPT is the least trustworthy row in the table.

---

**CORRECTION (2026-08-24): THE "+30.7% OVER STOCK" AND THE "BIMODAL BASELINE" ARE BOTH WRONG.**
Eight fresh draws of STOCK (JobManager restarted between each to force a new slot-pool order,
REPS=4 each, `20260824-150558` … `20260824-161802`) give:

- **Eight DIFFERENT placements, and in 8/8 the two heavy slices landed on DIFFERENT machines.**
  Never together. Per-run throughput 3680-3958, pooled n=32 mean **3788** (sd 370) — which matches
  control B's 3805.
- So there is no bimodality. The 2983 STOCK draw inside `20260821-162029` is a rare outlier that did
  not recur once in eight attempts.

*Every arm re-tested against the 32 pooled stock episodes:*

| arm | mean | vs real STOCK | p |
|---|---|---|---|
| LPT | 3899 | **+2.9%** | **0.27** |
| ACO | 3852 | +1.7% | 0.49 |
| LEAST_LOADED | 3484 | −8.0% | 0.010 |
| STOCK-in-campaign | 2983 | −21.3% | <0.0001 |

**LPT DOES NOT DEMONSTRABLY BEAT STOCK FLINK 2.3 at this problem size.** The methodological error was
measuring the baseline in ONE draw: stock's placement is redrawn at every job submission, so a single
run is a sample of size one, while the arms are deterministic and have no such variance. Always pool
many stock draws.

**What survives:** LEAST_LOADED is significantly WORSE than stock (−8.0%, p=0.010) — balancing slot
COUNTS actively hurts on unequal machines because it sends heavy work to the slow TM. And the
metaheuristics still tie with LPT.

**Why stock does well, against the offline bench's prediction of an ~80% gap for arbitrary placement:**
the slot pool hands out slots INTERLEAVED across TaskManagers, not grouped, so iteration order
happens to split the heavy slices. Flink's placement is arbitrary in principle but benign on this
cluster. The bench's figure assumes an adversarial ordering that this pool does not produce.

**Consistent with the bench all along:** at 4 slices / 3 TMs the instance is nearly trivial and LPT's
residual gap to optimum is 1.6-3.1% — the +2.9% measured sits exactly there. To show the effect the
instance must get harder: more slices (8-10), or a wider speed spread than 1 : 1.5 : 2.

---

**ROOT CAUSE FOUND (2026-08-24): A SUBTASK IS ONE THREAD, SO CPU-LIMIT HETEROGENEITY ONLY BINDS
WHEN A TASKMANAGER HOSTS MORE BUSY SUBTASKS THAN IT HAS CORES.**

Across the 8 stock draws, correlation between "combined cores of the machines hosting the two heavy
slices" and throughput = **−0.28**. Capacity where the heavy work lands does not predict throughput.

Because a subtask is single-threaded: giving one heavy subtask a 2-core TaskManager does not speed it
up. With `PIN_PARALLELISM=2` there are only TWO heavy slices over THREE TaskManagers, and every
observed placement put them on different machines — so no TM ever hosted two heavy subtasks, the
`limits.cpu` never bound, and placement COULD NOT matter. This single fact explains everything:

- LPT ties stock (+2.9%, n.s.) — moving a thread between machines changes nothing.
- The offline bench predicts 44.7% because it models speed as a per-slice DIVISOR, i.e. it assumes a
  faster machine accelerates a single slice. That assumption is false here.
- The 2983 outlier was the ONE draw with both heavy slices on `medium` — a single TM running two
  heavy subtasks on 1.5 cores. That is the only time the limit bound, and it cost 21%.

**AND IT RECONCILES WITH BOTH PAPERS.** SP-Ant: 5 executors per operator over 5 nodes. Li et al.:
parallelism 20 over 11 TaskManagers. Both have MORE PARALLEL WORK THAN NODES, so nodes host several
busy tasks and capacity binds. The thesis had fewer heavy subtasks than TaskManagers. The structural
gap is not scale, it is that ratio.

**THE FIX IS ONE PARAMETER:** raise `PIN_PARALLELISM` above the TaskManager count. At
`PIN_PARALLELISM=4` with 3 TMs, one machine MUST host two heavy subtasks, and which machine that is
becomes exactly the decision LPT makes and iteration order does not. Combine with the papers' speed
ratio **4 : 2 : 1** (instead of the current 2 : 1.5 : 1) and the cost of getting it wrong becomes 4x
rather than 1.3x.

*Also learned:* with the slot pool handing out slots INTERLEAVED, Flink's iteration order IS cyclic
over TaskManagers — so stock and the fork's count-based LEAST_LOADED are the same rule. The bench
confirms they produce identical placements in every simulated instance.

---

**THE RANK INVERSION, FOUND (2026-08-24, `20260824-164412`).** Binding regime: speeds 4:2:1,
`PIN_PARALLELISM=4` so all four slices are heavy and one TaskManager MUST host two. REPS=16.

| arm | heavy pair lands on | tput/slot | e2e |
|---|---|---|---|
| LEAST_LOADED | **slow (1 core)** | 3869 | 24465 |
| STOCK | medium (2 cores) | 7221 | 17410 |
| LPT | **fast (4 cores)** | 7223 | 17314 |
| ACO | fast | 7617 | 16061 |

LEAST_LOADED is −46.4% vs STOCK and vs LPT (p=0.00002) — its ties go to the first TaskManager in the
ordering, which is `tm-1-slow`, so it deterministically puts both heavy subtasks on the 1-core box.

**THE INVERSION — the same arm flips from second-best to worst, driven by an OBSERVABLE variable:**

| | loose regime (PIN=2, 2:1.5:1) | binding regime (PIN=4, 4:2:1) |
|---|---|---|
| LEAST_LOADED vs STOCK | **+16.8%** (p=0.00004) | **−46.4%** (p=0.00002) |

Significant in both directions. The context variable is the ratio of busy subtasks to cores per
TaskManager. **This is the precondition the meta-scheduler line needed**: which policy is best
depends on a measurable regime, so a learner has something no fixed table can reproduce. It is not
"LPT wins" — it is that the ranking is context-dependent.

*Caveats to carry:* LPT ties STOCK here (p=0.997) but only by luck — that draw put the heavy pair on
`medium` (2 cores, 2 threads, adequate); another pool order would have collapsed it like
LEAST_LOADED. The argument for a deterministic policy is variance elimination, not mean gain. And
ACO vs LPT (+5.2%, p=0.16) share an IDENTICAL mapping, so that difference is the environmental
oscillation, not policy.

**NEXT FOR THE RL LINE:** feed the busy-subtasks-per-core ratio into `arm_controller.py` as a state
feature, and run the meta-scheduler across both regimes against the best fixed policy. Beating it
requires switching arms with the regime, which is exactly what the table above shows is possible.

---

**CORRECTION (2026-08-25): THE "RANK INVERSION" DOES NOT SURVIVE ISOLATION. Do not cite it.**
The inversion recorded above (LEAST_LOADED +16.8% loose → −46.4% binding) compared campaigns with
DIFFERENT speed ratios (2:1.5:1 vs 4:2:1), so regime and heterogeneity moved together. Isolated —
speeds held at 4:2:1, only `PIN_PARALLELISM` changed (`20260824-200444` PIN=2 vs `20260824-164412`
PIN=4):

| | LEAST_LOADED vs STOCK |
|---|---|
| loose (PIN=2) | **−4.1%, p=0.47 (not significant)** |
| binding (PIN=4) | −46.4%, p=0.00002 |

The sign does not flip. What changes is MAGNITUDE: from nothing to 46%.

*What still holds:* in the loose regime no arm differs from stock (STOCK vs LPT −4.6%, p=0.46); in
the binding regime the choice is worth 46%. So the busy-subtasks-per-core ratio determines **whether
placement matters at all** — a real, measurable context variable. LPT also beats LEAST_LOADED even
in the loose regime (+8.5%, p=0.0064).

*The consequence for the RL line, stated honestly:* if LPT is never worse and sometimes much better,
**"always LPT" is an optimal fixed policy** and a learner cannot beat it on quality. The remaining
room for RL is COST — not paying for ACO/GA search when it does not pay off. For a learner to win on
quality, some arm must be better in one context and worse in another, and that is still missing. The
communication-cost term remains the candidate, since packing and balancing pull opposite ways.

*Loose-regime numbers (4:2:1, PIN=2, `20260824-200444`):* STOCK 3743 (n=15), LEAST_LOADED 3589
(n=16), LPT 3922 (n=13). **ACO never ran** — the JobManager died first.

**FLINK 2.3.0 BUG THAT KILLED THE RUN — worth reporting upstream.** JobManager exits with code 239
(`FatalExitExceptionHandler`, i.e. -17) from:
`IllegalArgumentException: The startOfTimeout should be in the past but is after the current time`
at `DefaultStateTransitionManager$Phase.scheduleRelativelyTo:228` ← `Stabilizing.<init>:359` ←
`progressToStabilizing:154` ← `Cooldown.finalizeCooldown:295`.
`Cooldown.onChange()` stores `firstChangeEventTimestamp = now()`; when the cooldown expires,
`scheduleRelativelyTo` requires that instant to be in the past. The class is `@NotThreadSafe` and its
javadoc says it relies on single-threaded access, but `onChange` and the scheduled callback read the
clock from different threads — on a VM over WSL two `Instant.now()` readings can invert by
microseconds. The precondition then throws inside a scheduled callback and **the whole JobManager
dies**. Triggered when a rescale request lands right as the cooldown expires, which a
rescale-heavy experiment does constantly.
*Mitigation:* the scale-up request arrives ~45 s after the previous one while the cooldown ends
~40 s after it (30 s + ~10 s restart) — only 5 s of margin. **Use `WIDE_HOLD=60`** for ~20 s of
margin, at +12% campaign time.

---

**STRUCTURAL CONSTRAINT FOUND (2026-08-25): THE SLOT ASSIGNER CAN ONLY CHOOSE AMONG THE SLOTS THE
RESOURCEMANAGER ALREADY GAVE THE JOB.** Campaign `20260824-230546` (4 slots/TM = 12 total,
`SUBMIT_PAR=8`) logged `slices=8 freeSlots=8 tmsAvailable=2 spread={tm-2-medium=4, tm-1-slow=4}` —
**the fast 4-core machine was never available to any arm.** All three TaskManagers were up and
registered the whole time.

The job's slot pool holds only what the RM handed over at SUBMISSION. Asking for 8 of 12 slots, the
RM filled it from slow(4)+medium(4) and never touched fast; rescaling to 6 does not add machines,
it only frees slots already in the pool. So placement policy is bounded by an upstream decision the
thesis does not control — worth stating as a limit on what any SlotAssigner can achieve in Flink.

*Why earlier campaigns were fine:* 6 total slots with `SUBMIT_PAR=6` forced the pool to contain all
three TaskManagers. **Rule: `SUBMIT_PAR` must equal the TOTAL slot count.** With 12 slots use
`SUBMIT_PAR=12`. The cost is zero headroom — a TaskManager restart wedges the job in `CREATED`,
which already happened once at `SUBMIT_PAR=10`. Fallback if that recurs: 3 slots/TM (9 total,
`SUBMIT_PAR=9`).

*Related knob to keep in mind:* `taskmanager.load-balance.mode` is read by `SlotManagerConfiguration`
too, so it also governs how the RM fills the pool — not just how the assigner places. At `NONE` the
RM packs, which is what produced the two-machine pool above.

**Campaign `20260824-230546` IS INVALID for the binding-regime question** — it measured a
two-machine cluster (medium+slow only), no fast. Numbers there (STOCK 3542, LEAST_LOADED 3177,
LPT 3846, ACO 3611) describe that accidental configuration, not the intended one.

---

**THE CLEAN RESULT (2026-08-25). 12 slots (4 per TM), speeds 4:2:1, TARGET_PAR=6, WIDE_HOLD=60,
REPS=12. Only `PIN_PARALLELISM` differs between the two campaigns — everything else identical.**
Binding `20260825-123155` (PIN=6), loose `20260825-142447` (PIN=2).

| | binding (6 heavy) | loose (2 heavy) |
|---|---|---|
| STOCK | 4123 | 2657 |
| LEAST_LOADED | 4228 (+2.5%, p=0.59) | 2503 (−5.8%, p=0.38) |
| **LPT** | **6139 (+48.9%, p=0.00002)** | 2579 (−2.9%, p=0.61) |
| ACO | 5870 (+42.4%, p=0.00002) | 2777 (+4.5%, p=0.46) |

LPT vs LEAST_LOADED in the binding regime: **+45.2%, p=0.00004**. LPT vs ACO: +4.6%, p=0.30 (tie).

**INTERACTION TEST — the value of the policy choice depends on the regime:**

| | binding | loose | p |
|---|---|---|---|
| LPT advantage over STOCK | **+2016** | −78 | **0.016** |
| ACO advantage over STOCK | +1748 | +120 | 0.043 |

*Mechanism, straight from the `mapping=` field:* LPT and ACO place **3 / 2 / 1** slices on the 4-, 2-
and 1-core machines — proportional to capacity. LEAST_LOADED splits 2/2/2 by COUNT and ties with
STOCK even in the binding regime, i.e. balancing counts without looking at capacity buys nothing.
STOCK put 3 on the 2-core box.

*What cannot be claimed:* this is NOT a rank inversion. LPT is never worse, it merely stops being
better, so **"always LPT" remains an optimal fixed policy on quality** and a learner cannot beat it
there. The RL value stays on COST — not paying for ACO/GA search when it does not pay off. With
multiplicity, LPT's interaction (p=0.016) survives Bonferroni over the three interaction tests;
ACO's (p=0.043) does not. n ≈ 10-12 per cell.

*Config that finally worked, after two wedged attempts:* `SUBMIT_PAR` must leave real headroom —
10 of 12 slots left only 2 spare and any TaskManager restart left the job stuck in CREATED, because
`slot.idle.timeout: 300000` reserves freed slots for five minutes. And `WIDE_HOLD=60` is required to
keep rescale requests clear of the 30 s cooldown boundary, which triggers the Flink 2.3.0
`DefaultStateTransitionManager` race that kills the JobManager.

---

**STOCK FLINK IS A LOTTERY, MEASURED (2026-08-25). Eight draws in the binding regime** (12 slots,
4:2:1, PIN=6, TARGET_PAR=6, REPS=4 each, JobManager restarted between draws to force a fresh slot
pool order).

| draw | fast/med/slow | capacity Σmin(cores,k) | tput/slot |
|---|---|---|---|
| 2 | 1/2/3 | 4 | 3020 |
| 5 | 3/0/3 | 4 | 3425 |
| 6 | 3/1/2 | 5 | 3882 |
| 1 | 3/3/0 | 5 | 3978 |
| 3 | 2/3/1 | 5 | 4372 |
| 8 | 2/3/1 | 5 | 4744 |
| 4 | 4/1/1 | 6 | 5739 |
| 7 | **3/2/1** | 6 | 5887 |

Seven distinct shapes in eight draws — NOT bimodal, a scatter. Range **3020-5887 = 95%**, mean 4381,
sd 1030 (CV 24%). **The capacity model correlates +0.95 with measured throughput**, validating the
"a subtask is one thread, the limit binds when threads exceed cores" mechanism end to end.

**LPT deterministically produces the max-capacity shape (3/2/1) and measures 6139: +40% over the
average draw, +103% over the worst.**

*This is the strongest form of the thesis claim and it does NOT rest on beating stock on average.*
It is Li et al.'s "arbitrary placement" measured directly, and it survives the obvious objection
("but sometimes Flink gets it right"): draw 7 did land on 3/2/1 and reached 5887 — but nothing
predicts when, and a quarter of the draws land at 3020.

---

**COMMUNICATION PROBE — a PACK arm, and what it found (2026-08-25).** Added `Strategy.PACK` to the
fork (fills TaskManagers one at a time, FASTEST FIRST, ~20 lines + 2 tests; 27/27 green). Purpose:
before spending a day on a full communication cost term, ask whether concentrating slices helps at
all. `MIN_RESOURCES` could not be used for this — Flink gates `minimalTaskManagerPreferred` on
`executionTarget == "embedded"`, i.e. application mode, and this is a session cluster.

*Loose regime (`20260825-181933`, PIN=2, 6 slices on 12 slots):* PACK put all six on TWO machines
(4 fast + 2 medium).

| metric | PACK vs LPT | p |
|---|---|---|
| throughput | +4.7% | 0.35 |
| **e2e** | **−11.1%** | **0.011** |
| backpressure | +0.7% | 0.73 |

**Packing does not change throughput but cuts latency 11%** — exactly where it should: throughput is
CPU-bound and packing does not touch that; latency is bound by how many network boundaries a record
crosses. This is the metric SP-Ant optimises ("topology response time"), which is why it reports 50%
there. Multiplicity: 9 comparisons across 3 metrics; p=0.011 survives Bonferroni over the three e2e
tests (0.017) but not over all nine (0.0056). n = 10-12.

*Binding regime (`20260825-194916`, PIN=6):* **the predicted trade-off did NOT appear.** PACK +4.3%
throughput (p=0.31), −3.5% e2e (p=0.68) — ties on both. Reason, from the capacity model: PACK gives
4/2/0 → min(4,4)+min(2,2)+0 = 6; LPT gives 3/2/1 → 3+2+1 = 6. **Both reach capacity 6.** Filling
fastest-first happens to be capacity-optimal at this cluster shape, because `fast` has 4 cores and
exactly 4 slots. Designing PACK fastest-first — done deliberately to avoid confounding network with
capacity — is what removed the tension.

**SO THERE IS STILL NO RANK INVERSION.** PACK weakly DOMINATES LPT: never worse, better on latency
when capacity does not bind. A fixed policy still covers every measured context, so a learner still
has nothing to decide on quality. To create a genuine trade-off the cluster shape would have to make
packing FORCE oversubscription — e.g. slots per TaskManager exceeding its cores, so filling the fast
machine overshoots it. Today 4 slots / 4 cores on `fast` is exactly the coincidence that erases it.

---

**COMMUNICATION TERM: IMPLEMENTED, AND IT HAS NO MEASURABLE PAYOFF HERE (2026-08-26,
`20260826-153816`).** Weight 1.0, loose regime, 12 slots, speeds 4:2:1, REPS=12.

*The term works mechanically.* ACO moved to `3 fast / 2 medium / 1 slow` where LPT and GA give
`4/1/1` — with speeds 4:2:1 the proportional share of 6 slices is 3.43/1.71/0.86, so 3/2/1 nearly
zeroes the balance term, and ACO traded worse communication for better balance. Both weights are 1.0
and pull opposite ways, exactly as designed.

*Nothing reaches performance.* LPT 2599, ACO 2634, GA 2594, PACK 2748 tput; e2e 27731 / 26204 /
27082 / 26398. **Twelve pairwise comparisons, every p ≥ 0.23.**

**CORRECTION — the PACK latency advantage does NOT replicate. Do not cite it.** Reported earlier as
−11.1% e2e with p=0.011; an independent replication of the same arm in the same regime gives −5.0%,
p=0.55. It had already been flagged as not surviving Bonferroni over the nine comparisons; the
replication settles it as a false positive.

*Conclusion:* inter-TaskManager traffic does not cost enough to matter in this testbed, as predicted
when the idea was first scoped — the three minikube "nodes" are containers on one kernel, so crossing
between them is veth and bridge, not a wire. The local-channel-vs-Netty difference is real but lost
in the noise. **This closes the fourth and last candidate for a rank inversion** (state locality,
rescale cost, capacity, communication). A capacity-aware greedy still covers every measured context.

*Infrastructure fixed along the way:* `deploy-thesis-fork.sh` now passes `THESIS_COST_BALANCE`,
`THESIS_COST_LOCALITY` and `THESIS_COST_COMMUNICATION` into the JobManager pod — previously only
three THESIS_* vars were patched in, so setting a weight in the campaign shell did NOTHING and the
run would have silently measured weight 0. `run.json` now records the weights read from the pod.

---

**WHY EVERY SEARCH FOUND NOTHING — THE DESIGN WAS SCORING THE EASY OBJECTIVE (2026-08-26).**
Vicente pushed back that placement is NP-hard so a heuristic cannot be optimal, and that something in
the design must be making it optimal. He was right, and all three of his fixes were right.

*The reason:* throughput of a data-parallel stage is a SUM of its subtasks' rates, and the objective
`Σ_t min(cores_t, load_t)` with total load fixed is separable-concave — greedy is optimal for that
class at any scale, which is what the 0.0% enumerations kept showing. **Latency is a MAX** (the
straggler sets it), and min-max IS the NP-hard problem. We were scoring the easy metric. SP-Ant
optimises response TIME, which is why its ACO has room and reports 50%.

*Measured LPT gap to the exact optimum on the MAKESPAN/latency objective:*

| slots per TM | demands | mean gap | worst |
|---|---|---|---|
| symmetric 4/4/4 | equal | 0.0% | 0.0% |
| symmetric 4/4/4 | three levels | 0.5% | 14.3% |
| **asymmetric 6/4/2** | equal | 0.0% | 0.0% |
| **asymmetric 6/4/2** | **three levels** | **7.3-8.8%** | **33.3%** |
| asymmetric 8/4/1 | three levels | 0.4% | 11.1% |

**All THREE ingredients are needed:** (1) latency, not throughput; (2) the FAST machine with the
FEWEST slots — with symmetric slots you can always spread and dodge the conflict, with fast capped at
2 slots you must CHOOSE which two slices get the good cores; (3) heterogeneous slice demands (a query
with more operators at staggered parallelisms). 8/4/1 does NOT work — one slot on fast leaves no
decision. The sweet spot is 6/4/2.

7-9% mean with 33% peaks sits comfortably above this testbed's ±5% noise floor, so it is measurable.
And `THESIS_COST_BALANCE_METRIC=MAKESPAN`, built weeks ago and never used, is exactly the switch that
makes the arms optimise this objective.

**Existing corollary that now reads differently:** in the binding campaign LEAST_LOADED's e2e was
24465 vs LPT's 17314 (**41% worse**), a much larger relative spread than their throughput gap —
because it put a heavy slice on the slow machine and the straggler set the latency. The signal was
already in the data under the latency metric.
