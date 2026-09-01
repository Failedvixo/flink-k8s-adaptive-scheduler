# Memory Index

- [User profile — Vicente](user_profile.md) — Master's thesis student at UDP, adaptive Flink scheduler for K8s
- [Project architecture & calibration](project_architecture.md) — Design, 12-arg run_strategy_experiment, autoscaler.sh internals, calibrated params, why staleness matters
- [Project status — K8s-side line (PAUSED)](project_status.md) — V1..V5 + SARSA_META meta-schedulers over K8s pod placement. Paused since Aug 5 2026 in favour of the fork line; its "cluster down" blocker is stale.
- [V2 underperforms V1 on Q8](project_v2_q8_anomaly.md) — V1 beats V2-new by +42% Tput/core on Q8/SINE; thrashing hypothesis + investigation entry points
- [Known pitfalls and operational gotchas](feedback_pitfalls.md) — FIXED_STRATEGY=ADAPTIVE crash, namespace reset, JAR loss, CRLF, backups, manifests-not-applied, partial vertex payload rejected, cumulative busy% misleading, recompile after fromArgs change, Nexmark env-var leak
- [Nexmark real benchmark — invocation protocol](project_nexmark_integration.md) — JOB_CLASS/EXTRA_JOB_ARGS/HEAVY_VERTEX_PATTERN env vars + dir convention for Q5/Q8 jobs
- [Phase 3 reward/state design](project_phase3_reward_design.md) — Bandit/SARSA driven by Flink metrics aggregated per TM (busy% dispersion), not K8s node metrics nor the structural "balanced" proxy
- [NEXT SESSION — correr el experimento con job sintético](project_next_session_pilot.md) — 4 corridas controladas nulas por 4 razones estructurales distintas; el comando exacto y los huecos de diseño conocidos
- [Paper CETSA/LBA-CE — lo que realmente lee](project_paper_cetsa.md) — Li et al. TBD 2023 leído del PDF: el clúster heterogéneo es carga estructural del algoritmo, y un clúster homogéneo lo degenera — explica los nulos
- [Flink core fork — phases 1-3](project_flink_core_fork.md) — Fork de Flink 1.18. CLAVE: el banco offline demuestra que LEAST_LOADED es óptimo exacto sobre TMs idénticos (0.0% de brecha) — eso explica todos los nulos; la heterogeneidad es requisito, y LPT es el listón real para ACO/GA
- [Vicente corre los comandos él mismo](feedback_user_runs_commands.md) — dar comandos, nunca ejecutar nada que toque el clúster
- [Migración a Flink 2.3](project_flink_2x_migration.md) — spec del port + hallazgo clave: Flink 2.3 ya balancea carga, pero con pesos declarados y máquinas idénticas
- [Paper SP-Ant (ACO en Storm)](project_paper_spant.md) — ACO gana 50% pero por comunicación + feromona persistente, dos cosas que el ACO de la tesis no tiene; precedente citable para el término de comunicación
- [freeSlots = lo que el job pidió, no el clúster](project_freeslots_submission.md) — un TM sano puede quedar fuera de la decisión; pedir >10 slots para que `fast` entre
- [El objetivo RL: caracterizar operadores](project_rl_goal_operator_profiling.md) — no es elegir brazos ni co-localizar; requiere máquinas con >1 dimensión de recurso
- [Resultado citable — campaña 20260830](project_headline_result.md) — LPT +33% y ACO +34% pasando Bonferroni; por qué las campañas previas subestiman
- [Comunicación: cerrada con doble nulo](project_communication_closed.md) — el mecanismo cambia las decisiones pero no hay red que ahorrar en un host único
- [Costo de romper el slot sharing](project_slot_sharing_cost.md) — nada en throughput, 3.5x en slots; y por qué SHARED a par 2 es inestable con CPU_LOAD=2500
