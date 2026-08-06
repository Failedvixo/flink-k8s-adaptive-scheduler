# Phase 3 — live arm signal and the external learner

Phase 2 proved the forked `ThesisSlotAssigner` really decides placement, but the
arm was an environment variable: changing strategy meant restarting the
JobManager, so a run could only ever exercise one arm. Phase 3 turns the arm
into a *signal* the external meta-scheduler republishes between rescales, and
closes the learning loop around it with Flink's own metrics.

```
  rescale ──► assigner applies the published arm ──► job runs
                                                       │
   publish next arm ◄── SARSA update ◄── reward ◄── measure (Flink REST)
```

Nothing learns inside the JobManager. The assigner stays a pure function of
(slices, slots, published arm), which is what keeps it safe against the adaptive
scheduler's speculative re-invocations.

## The arm channel

| piece | where |
|---|---|
| arm file | `/var/thesis/arm` on the `minikube` node, hostPath-mounted read-only into the JM |
| reader | `ThesisSlotAssigner.currentStrategy()` (fork), re-read at most once per second |
| writer | `scripts/publish-arm.sh ARM` (atomic: write temp, rename over) |

Precedence when resolving the arm: `thesis.assign.strategy` system property →
arm file → `THESIS_ASSIGN_STRATEGY` env var → `ROUND_ROBIN`. Anything
unparseable falls through to the next source, and a failed read keeps the last
good arm, so a half-written file can never fail a scheduling decision.

Two implementation details that are not free:

* The JobManager mounts the **directory**, not the file. A single-file bind
  mount pins the inode, so a renamed file would never be visible in the pod.
* The 1 s cache is not only about I/O on the JM main thread: without it the arm
  could change *within* one rescale evaluation, so the placement the scheduler
  decided on would differ from the one it executes.

## Reward and state — Flink metrics, not the machine's

Load is read per subtask (`busyTimeMsPerSecond`, `backPressuredTimeMsPerSecond`,
…) and joined to the TaskManager hosting it through the `taskmanager-id` field
of `/jobs/{jid}/vertices/{vid}`. That join is what converts subtask metrics into
the per-TM granularity the assigner decides at, and it keeps the signal
engine-level: no Metrics Server, no node exporter, nothing machine-specific.

* **Reward** (default) = `1 / (1 + cv)`, where `cv` is the coefficient of
  variation of per-TM busy time over **all registered** TaskManagers. Counting
  only the hosting TMs would give an arm that packs every slice onto one TM a
  perfect score. Dispersion is preferred over throughput/core because a rescale
  changes parallelism, and throughput cannot tell a better placement from a
  bigger job — the Phase-2 reward confound. `--reward throughput|blend` exist;
  every row carries the raw throughput either way, so any reward can be
  recomputed offline from the CSV.
* **State** = `saturation | imbalance | backpressure`, each cut LOW/MED/HIGH.
  Backpressure is state and deliberately *not* reward: a backpressured subtask
  says the bottleneck is downstream, which is not evidence that its placement is
  bad.

An epoch is **not credited** to its arm when either
`freeSlots == slices` (every arm would have produced that placement — see the
Phase-2 rule `slices = min(upperBound, availableSlots)`) or the job is below
`--min-busy-ms-s` busy (dispersion of near-zero load is noise). Both cases are
still recorded, with the reason, in `credit_note`.

The arm the reward is attributed to is the one the JobManager **logged**
(`[THESIS_ASSIGN] strategy=…`), not the one that was published: an arm published
while no rescale happens is never applied, and crediting it would poison the
Q-table.

## Running it

```bash
# once: JobManager on the fork, with the arm mount
scripts/deploy-thesis-fork.sh STOCK

# learn online (needs the REST port-forward)
kubectl port-forward -n flink svc/flink-jobmanager 8081:8081 &
scripts/arm_controller.py

scripts/arm_controller.py --fixed-arm ROUND_ROBIN   # per-arm baseline
scripts/arm_controller.py --observe                 # record only
scripts/publish-arm.sh --read                       # what is published now
```

Episodes land in `results/arm-controller/episodes-*.csv`, the Q-table in
`results/arm-controller/qtable.json`.

## Validated 2026-08-05

* **Hot arm switch, no restart.** Same JM pod (`restarts=0`) throughout:
  `STOCK` scored 1/3 balanced rescales, then `ROUND_ROBIN` — published while the
  JobManager kept running — scored 3/3, with
  `[THESIS_ARM] arm changed STOCK -> ROUND_ROBIN` in the log. Reproduces the
  Phase-2 result (STOCK 3/8, RR 8/8) through the new channel.
  CSVs in `results/assigner-spread-phase3/`. 3 TMs × 2 slots, p=6 → upperBound=3.
* **Metric join.** Per-subtask busy times aggregate to per-TM load correctly
  against a live job; `applied_arm` recovers the arm and `had_choice` from the
  JM log.
* **Closed loop.** Published `LEAST_LOADED` → the next rescale applied it
  (`choice=True`) → Q updated to `0.3` = α·r, the correct SARSA step.
* **`ACO` and `GA` run inside the real JobManager**, 3/3 balanced rescales each,
  no exception in the JM log — the classes reach the patched dist jar and the
  searches complete on the scheduler's main thread without a visible stall.

## The arm set

| arm | decides by | trade-off? |
|---|---|---|
| `STOCK` | whatever unpatched Flink would do (state locality at a rescale) | baseline |
| `FCFS` | next slot in iteration order (was called `DEFAULT`) | none |
| `ROUND_ROBIN` | cyclically over TaskManagers | balance only |
| `LEAST_LOADED` | TaskManager with most free capacity | balance only |
| `ACO` | ant colony over the whole instance | balance **vs** state locality |
| `GA` | genetic algorithm over the whole instance | balance **vs** state locality |

`ROUND_ROBIN` and `LEAST_LOADED` coincide on homogeneous TaskManagers — both
reach the optimum of the single objective they optimise. `ACO` and `GA` are what
give the meta-scheduler a real decision: they minimise
`w_balance · imbalance + w_locality · lost_state` ([PlacementInstance.java]), so
the better arm depends on how skewed and how stateful the workload is, which is
a property of the scenario rather than a constant.

Both searches are **stateless across calls and bounded by a fixed iteration
count, never a clock**. The assigner is re-invoked speculatively while one
rescale is evaluated, so a colony that remembered previous calls, or a search
that stopped when time ran out, would answer the same question differently on
different asks — and the placement the scheduler decided on would not be the one
it executes. They seed themselves from the problem instance instead.

## The campaign

`scripts/run-fork-campaign.sh` runs every arm alone over every query and every
arrival distribution (`CONSTANT`, `SINE`, `RAMP` — the last replaces `STEP` and
climbs linearly to the peak, so the load never comes back down and the
autoscaler cannot wait out a bad decision). `scripts/analyse_fork_campaign.py`
then ranks the scenarios by how far apart the arms end up: the widest scenario
is the one to train the meta-schedulers on, and a scenario where the arms tie is
one where no meta-scheduler can beat a fixed arm by more than noise.

The meta-schedulers are cells in the same grid (`ARMS="BANDIT SARSA"`), so they
are measured exactly like the fixed arms.

One script per query, `experiment-fork-q0.sh` … `experiment-fork-q12.sh`, each a
thin wrapper that names its query and its rescaled vertex:

```bash
PILOT=1 ./experiment-fork-q5.sh              # 5 min, SINE only — a shakedown
./experiment-fork-q5.sh                      # 6 arms x 3 dists
ARMS="BANDIT SARSA" ./experiment-fork-q5.sh  # the learners on the same grid
```

**What is kept.** Only what training and comparison read: `summary.json`, the
per-rescale `episodes-*.csv` (the training data itself), `thesis-assign.log`
(ground truth for the applied arm), and the controller log. The autoscaler logs
and the full job graph are summarised into `summary.json` and then deleted —
about 15 KiB per cell instead of megabytes, since a full grid is hundreds of
cells. `KEEP=all` disables the pruning while debugging.

**A rescale is not guaranteed.** A smoke run of 180 s produced *one* assignment
round and *zero* decisions. `PILOT=1` tightens the autoscaler's cadence; for the
real runs the cell must simply be long enough. `summary.json` reports
`assignments.decisions`, which is the number to check first on any cell that
looks empty. Note that `rounds` counts assigner *invocations*, most of which are
speculative evaluations the scheduler never executes — the controller's episodes
are the ground truth for placements actually applied.

## Two prerequisites discovered by running it

**Every parallel vertex must scale, not just the heavy one.** With slot sharing,
the width of the sharing group — and therefore how many slots the job requests —
is set by the *widest* vertex, not by the rescaled one. With the source pinned at
its initial parallelism, the pool holds exactly as many slots as the job needs at
every rescale (`freeSlots == slices`), so the assigner never has a decision and
every arm produces the same placement. `SCALE_ALL_VERTICES=1` (autoscaler.sh,
default on for the campaign) gives every vertex whose parallelism exceeds 1 the
same range; vertices declared at parallelism 1 stay there, because in several
queries that 1 is part of the semantics (Q5's global top-N). Measured: with the
flag off, `slices=8 freeSlots=8`, zero decisions; with it on, `slices=2
freeSlots=8` — a real decision.

The pipeline must also start **wide** and be narrowed by the autoscaler. Starting
at the floor throttles the source, starves the heavy vertex, and leaves the
autoscaler reading an idle signal it never scales up from; and it is the
scale-*downs* that create `freeSlots > slices` anyway.

**Checkpoints must live on shared storage.** With the stock
`state.checkpoints.dir: file:///tmp/checkpoints`, each TaskManager checkpoints to
its own local disk, and the first rescale that moves a subtask elsewhere fails:

```
FileNotFoundException: /tmp/checkpoints/<job>/chk-1/... (No such file or directory)
 -> BackendBuildingException: Failed when trying to restore heap backend
 -> the job restart-loops
```

This is not neutral between the arms. `STOCK` anchors each slice to the slot
already holding its state and mostly escapes it; `ROUND_ROBIN`, `LEAST_LOADED`,
`ACO` and `GA` move state deliberately and crash. A campaign run on local
checkpoint storage would rank the arms by an infrastructure artefact and conclude
that stock Flink wins. `scripts/setup-shared-checkpoints.sh` deploys MinIO and
repoints checkpoints at `s3://` using the S3 plugin already inside the Flink
image. Verified: same workload, job stays RUNNING through three rescales, zero
restore failures, `busy≈454 ms/s` measured on a real placement.

## Open items

1. **The rewards measured so far are not meaningful** — `TopSpeedWindowing` runs
   at ~2 ms/s busy. Differentiating arms needs the skewed Nexmark workload, which
   is what the campaign runs.
2. **Warm-up window is a free parameter.** `--warmup 60` is a placeholder; it
   changes the result and must be fixed and documented before the final runs.
3. **State locality is not yet fully measurable.** Checkpoints live on local
   `file:///tmp` paths, so the cost of moving state is not paid the way it would
   be with shared storage — `ACO`/`GA` will place differently from
   `ROUND_ROBIN`, but the *benefit* of their locality term is understated until
   checkpoint storage is shared.
