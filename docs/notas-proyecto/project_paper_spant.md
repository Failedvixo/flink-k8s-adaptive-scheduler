---
name: project-paper-spant
description: "Farrokh et al., SP-Ant (ESWA 2022) — ACO scheduler on Storm that beats round-robin by 50%. Read from the PDF: why its ACO wins for reasons the thesis's ACO does not share, and why it is the citable precedent for the communication-cost term."
metadata:
  type: reference
---

**Farrokh, Hadian, Sharifi, Jafari — "SP-ant: An ant colony optimization based operator scheduler
for high performance distributed stream processing on heterogeneous clusters", Expert Systems With
Applications 191 (2022) 116322.** PDF supplied by Vicente 2026-08-21; not in the repo. Code at
github.com/SP-Ant/HPC. (Corrigendum 2024 fixes only the affiliation.)

**What it does.** Apache Storm 2.1.0, heterogeneous cluster (1 VM 4-core, 2 VMs 2-core, 2 VMs
1-core). Objective = topology response time, Eq. 5: `R = Σ processing time + Σ network latency
between connected executors`. A bin-packing pre-pass PINS the most communicative operators onto the
same worker node up to a CPU threshold `T_c`; only the remaining, less communicative operators enter
the ACO. Pheromone accumulates over runtime feedback. Result: ≥50% lower response time than Storm's
default round-robin and than R-Storm; converges from ~25 ms to ~7 ms over the first ~5 minutes
(Fig. 7). Bin-packing itself contributes only ~13% (Fig. 9) — most of the win is the ACO.
Convergence needs 18-27 assignments (Fig. 12).

**WHY ITS ACO WINS AND THE THESIS'S DOES NOT — three differences, all decisive:**
1. **Communication is half its objective.** The thesis's ACO optimises balance + state locality, and
   state locality is INERT at rescale (verified in Flink source). So it optimises a real term plus a
   distractor — which is exactly why it tied with LPT.
2. **Pheromone accumulates across scheduling rounds.** The thesis's ACO runs once per rescale and
   starts from a uniform matrix every time; without accumulation ACO is just a randomised search
   with more steps than LPT.
3. **Bin-packing pins the communicative operators first.**

**THE OBSTACLE SP-ANT DOES NOT HAVE, already documented in `AntColonyPlacement`'s javadoc:** Flink's
AdaptiveScheduler invokes the assigner SPECULATIVELY for rescalings it may never carry out, so
pheromone kept inside the assigner would be shaped by placements that never happened. Storm's Nimbus
schedules once per round and is free of this. The code already states the resolution: *"All
persistence lives in the external learner instead."*

**THE PORTABLE DESIGN (worked out 2026-08-21, not implemented):** the external controller already
reads from the JM log which placement ACTUALLY ran and measures its `e2e_delay_ms` — which is the
paper's `R`. It publishes a pheromone matrix to `/var/thesis/pheromone` exactly as it publishes
`loads` and `speeds`; `AntColonyPlacement` only READS it to seed its initial pheromone. Key detail:
index the matrix by **(subtask index, TaskManager resource id)**, not (slice, physical slot) —
slice/slot indices do not survive a rescale, but "subtask 2" does, and the TaskManagers now carry
declared ids (`tm-1-slow` / `tm-2-medium` / `tm-3-fast`). The resource-id change of 2026-08-19 turns
out to be a prerequisite for this.

**WHAT DOES NOT TRANSFER FROM STORM TO FLINK.** SP-Ant's bin-packing co-locates communicative
operators; in Flink slot sharing does that BY CONSTRUCTION — a slice already holds one subtask of
every vertex in the sharing group. What remains between slices is the all-to-all shuffle, where
traffic between any pair is roughly equal, so the only lever is how many slice PAIRS cross machines,
i.e. packing vs spreading. A real tension against balance, but poorer than Storm's.

**CONVERGENCE CAVEAT.** SP-Ant needs 18-27 assignments to converge; a thesis campaign has 20
rescales per arm. Same order, so feasible — but the campaign would measure mostly the UNCONVERGED
phase, while the paper reports the steady state after it. Showing SP-Ant-like gains needs longer
campaigns, or carrying pheromone between campaigns (which makes the arm stateful and muddies the
comparison with the others).

**RECOMMENDED SEQUENCE:** the communication term FIRST and alone. It is half of SP-Ant's objective,
does not depend on convergence, and is measurable with the campaigns already in place. If it shows
no signal in this testbed, persistent pheromone will not rescue it.

Related: [[project_paper_cetsa]], [[project_flink_core_fork]], [[project_flink_2x_migration]].
