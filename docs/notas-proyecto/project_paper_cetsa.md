---
name: project-paper-cetsa
description: "Li et al. TBD 2023 (CETSA/LBA-CE) — what the algorithms actually read, and why a HOMOGENEOUS cluster makes them degenerate, which explains the thesis's null results."
metadata:
  node_type: memory
  type: project
  originSessionId: 0593a148-9705-4a45-b9c3-39034305d756
  modified: 2026-08-12T23:01:36.244Z
---

**Read from the actual PDF on 2026-08-12** (the user supplied it in-session; it is still NOT in the
repo — worth committing under `docs/`, since the whole thesis rests on it and nothing in the tree
even cites it). Supersedes the second-hand summary in [[project_flink_core_fork]].

Li, Xia, Luo, Fang — "Cost-Efficient Scheduling of Streaming Applications in Apache Flink on Cloud",
IEEE Trans. Big Data 9(4), Jul/Aug 2023, pp. 1086-1101.

**WHAT THE ALGORITHMS READ (the user's question: "does it look at cluster state?" — yes, and it is
LIVE MEASURED state, not declared capacity):**
- **Algorithm 1, CETSA:** `1: Get cluster runtime information JRI` → `3: parse JRI and get cost
  information NCIL of each node` → `4: sort the nodes by cost to get node priority list NPL`. Then per
  subtask: free slots meeting the resource requirement → highest-priority node → return its slot.
  `JRI == null` falls back to Round-Robin (line 20). Complexity O(n log n + m²).
- **Algorithm 2, LBA-CE:** cost evaluation `CE_Ni = Cost_Ni / Tuple_Ni` (Eq. 22, cost per tuple
  processed); busyness `B` = mean over X periods of `EP` = the fraction of time the node's CPU
  utilisation exceeds its OWN average over window DR (Eq. 23); fitness `F_Ni = 1/(CE_Ni · B_Ni)`
  (Eq. 24); nodes with `F ≥ AF` go in the high-fitness list and are served first. Resource threshold
  `RTH = RV · δ`, δ = 0.8. Complexity O(n log n + m·n·f).
- Load `Load_Ni = α·Ucpu + β·Umem` with `Ucpu = 1 − Timefree/Timetotal` (Eq. 26); α=0.8, β=0.2 in the
  experiments. Balance metric = standard deviation of node loads (Eq. 31).
- Architecture (Fig. 6): resource monitor + traffic monitor per TM → Database → Analysis Module →
  Improved Scheduler in the JM. **Same external-monitoring pattern as `arm_controller.py`** — that
  design choice of the thesis is faithful to the paper.

**THE FINDING THAT EXPLAINS THE THESIS'S NULL RESULTS — the paper's cluster is HETEROGENEOUS BY
DESIGN, and that heterogeneity is load-bearing, not scenery.** Table 4: Small (4 cores/8 GB/
$2.417e-3 per s, 1 JM + 3 TM), Medium (8/12/$4.861e-3, 4 TM), Large (12/16/$7.778e-3, 4 TM) — 12 VMs,
96 cores, 144 GB, three prices.
- On a HOMOGENEOUS cluster `CE_Ni` is identical for every node, so CETSA's priority list is arbitrary
  and LBA-CE's fitness collapses to `1/B` — **pure busyness**. Which is exactly what the thesis's
  dispersion reward measures, and exactly what came out indistinguishable across arms in four
  controlled runs.
- So the null is not "we failed to measure": it is what the paper's own algorithm PREDICTS when one of
  its two inputs is constant. This is the citable explanation, and it turns the heterogeneous-cluster
  work from a workaround into restoring the paper's premise.

**A TENSION IN THE PAPER'S OWN RESULTS THAT MIRRORS ONE THE THESIS FOUND:** CETSA (cost-optimising)
has LONGER execution time than RR (Tables 5-6) and a HIGHER SLA violation rate (Figs. 15, 22), because
it packs tasks densely. LBA-CE exists to manage that trade-off. The thesis's six arms have no cost
term at all, so they all sit on one side of it — another reason they resemble each other. Compare with
the thesis's own finding that better balance did not buy throughput.

**DEVIATIONS OF THE THESIS FROM THE PAPER, to declare explicitly in the write-up:**
1. **Scale.** Paper: 12 VMs, 96 cores, 144 GB, parallelism 20 over 11 TMs × 4 slots, 20k-60k records/s
   on Hibench Wordcount/Fixwindow. Thesis: 3 minikube nodes on one 7.7 GiB WSL, parallelism 4-8 over
   10 slots.
2. **Intervention point.** Paper uses Flink 1.11.1 + `DefaultScheduler` ⇒ placement happens ONCE at
   submission. The thesis uses `AdaptiveScheduler` ⇒ placement happens at every RESCALE. A deliberate
   and well-argued choice (it is what makes a meta-scheduler meaningful), but a deviation.
3. **No cost model.** The thesis's arms optimise balance and state locality; RC/CC/SC (resource rental,
   inter-node communication, scheduling cost, Eqs. 6-15) are absent. Communication cost in particular
   is a term the fork could compute — it knows which slices talk to each other.

See [[project_flink_core_fork]], [[project_architecture]].
