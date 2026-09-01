---
name: project-status-jun-15-2026
description: "K8s-side meta-scheduler line (V1..V5 + SARSA_META). PAUSED since 2026-08-05 — the active thread is the Flink fork campaign, see project_flink_core_fork."
metadata: 
  node_type: memory
  type: project
  originSessionId: f97d07a6-511b-4264-9779-f97b646675d5
  modified: 2026-08-05T22:59:52.386Z
---

**READ FIRST (2026-08-05): this file describes the OLDER, K8s-pod-placement line, which is paused.**
The active work is the Flink fork's slot-placement arms + external learner — see
[[project_flink_core_fork]] for current state, and note that the cluster is UP again (the "cluster
down" blocker below is stale) and now runs MinIO-backed shared checkpoints. Everything below stands
as the record of the K8s-side line; its pending items were never closed.

**State as of 2026-06-15** (continuing the [[project_architecture]] meta-scheduler line; see prior arc in git + the V1..V5 handoff).

**Meta-scheduler lineage**: V1 OFFLINE_BANDIT {FCFS,BALANCED,SARSA} → V2 {BANDIT,LEAST_LOADED,BALANCED} → V3 ProPS+ (LLM, collapsed to constant) → V4 LinUCB 9-feat 4-arm (no SARSA) → V5 LinUCB 13-feat (adds mem_velocity/mem_imbalance/busy_inst/busy_velocity). All trained on q2-{const,sine,step} with reward throughput_per_core.

**Phase 5 — SARSA_META — BUILT this session** (SARSA as meta-optimizer, not as an arm; the arm-version failed by cold start, V4_WITH_SARSA −53% on q2-step):
- Trainer `scripts/train_sarsa_meta.py`: tabular SARSA TD(0) over fixed-arm trajectories (a'=a since arm constant per run). State = discretised subset of the 13-feat context via training-data terciles; action = base arm. Default state features `saturation,cpu_imbalance,mem_imbalance` (busy_inst excluded — ~0 in q2-sine logs). Reward shaping `per_step` (r_t=R each step, γ=0.9 → Q≈R/(1−γ), argmax unchanged). Flags: --arms --dists --state-features --bins --gamma --alpha --epochs --reward-shaping.
- Runtime `scheduler/.../strategy/SarsaMetaStrategy.java`: self-contained (deliberately does NOT refactor OfflineBanditStrategy, to keep V5 re-run risk-free). Builds same 13-feat context, discretises (bisect_right match: `v>=e`), argmax Q, unseen-state → trained marginal-best arm (`default_arm`). Same `[META_DECISION]` log schema + extra `state=`/`seen=` fields.
- Wired: enum `SARSA_META`, AdaptiveScheduler map + periodicEvaluate dispatch + stats block, `experiment-sarsa-meta.sh` (SKIP_SARSA_META_TRAIN=1, SARSA_STATE_FEATURES override), resource `sarsa_meta_weights.json`, plot_results.py STRATEGIES (also added V4/V5).
- **Trained result**: 18 states, contextual policy {FCFS:3,BALANCED:7,LEAST_LOADED:3,BANDIT:5} — NOT collapsed (the win vs ProPS+). default_arm=BALANCED. Compiles clean (`mvn -o compile`).
- **PENDING**: cluster eval — `SKIP_SARSA_META_TRAIN=1 ./experiment-sarsa-meta.sh` once cluster is up, compare vs V4/V5 on q2. Needs `./force-scheduler-change.sh` rebuild first so the new class + Q-table reach the image.

**Also this session**: applied the missing busy_inst timeout fix (2s→5s in both httpGetJson helpers in OfflineBanditStrategy.java) — the handoff claimed it but it wasn't in the tree. Env-var propagation + cache-invalidation fixes were already present.

**BLOCKER**: K8s cluster down (API 127.0.0.1:32786 connection refused). V5 busy_inst validation (re-run V5, check busy_inst≠0 in scheduler-logs.txt, backup old as OFFLINE_BANDIT_V5_NO_BUSY) is still pending and also needs the cluster.

**SARSA_META cluster eval DONE (q5+q8, 600s, zero-shot q2-trained Q-table)**:
- q5 (base arms ARE at 600s → valid head-to-head): SARSA_META beats ALL base arms on const (53,475 vs LEAST_LOADED 51,116, +4.6%) and sine (17,437 vs FCFS 17,023, +2.4%); 3rd on step (BANDIT 30,380 wins). Switching real: q5-sine 8 switches / 8 states, const 1, step 2.
- q8 (switching confirmed: sine 9 switches, const 4, step 2). Tput/core SARSA_META leads on all 3 (const 5,011 / sine 1,876 / step 2,492) INCLUDING beating V1/OFFLINE_BANDIT on the q8-sine anomaly (1,876 vs 1,842) — BUT q8 base arms only exist at 300s (`results/q8-*/*_300s/`), so NOT a valid head-to-head yet. To close: re-run q8 base arms (+V4/V5) at 600s.
- NOT collapsed to constant (the win vs ProPS+/V3).

**RESULTS BACKED UP TO GITHUB**: all q5/q8 SARSA_META results + Phase 5 code pushed to origin/main commit **66a87f0** (Failedvixo/flink-k8s-adaptive-scheduler), 2026-06-16. User plans to DELETE results/ from local disk to free space — so in future sessions read results from the GitHub remote / that commit, not the local tree (it may be gone). `git show 66a87f0:results/q5-sine/SARSA_META/METRICS-SUMMARY.txt` etc., or gh/raw URLs.

**Next**: (1) re-run q8 base arms (+V4/V5) at 600s for valid q8 comparison; optionally V4/V5 on q5 for meta-vs-meta. (2) Validate V5 busy_inst. (3) Phase 6 ProPS+ V6 (13 feats + LLM, test if larger feature space breaks the argmax plateau). See [[project_v2_q8_anomaly]], [[feedback_pitfalls]].
