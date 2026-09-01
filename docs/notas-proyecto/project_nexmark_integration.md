---
name: Nexmark real benchmark — invocation protocol
description: Args, env vars and dir conventions for the Q5/Q8 Nexmark Real jobs added on top of the original ConfigurableGraphJob pipeline
type: project
originSessionId: 78eca039-b7c6-4b31-b618-984883204635
---
A second benchmark family lives under `flink-nexmark-job/.../nexmark/`:
`NexmarkRealJob` dispatches `--query=q5|q8` (Hot Items / Person⨝Auction).

**Why:** Original `ConfigurableGraphJob` is a custom CPU-load stress test, not standard. Real Nexmark queries make results comparable to literature (Beam, Flink benchmarks papers).

**How to apply:**

- The uber-jar has Main-Class = `ConfigurableGraphJob`. To run Nexmark, override at submit time with `-c com.thesis.benchmark.nexmark.NexmarkRealJob`. Both `run-experiment-common.sh` and `autoscaler.sh` honor `JOB_CLASS` env var to do this.
- NexmarkRealJob args are POSITIONAL and must match ConfigurableGraphJob for args[0..7] so the runner is compatible: `rate dur par window cpuLoad dist heavyPar maxAge`. The `cpuLoad` slot is IGNORED by NexmarkRealJob but must be present. Then append Nexmark-only args via `EXTRA_JOB_ARGS`: `"<query> <zipfAlpha> <hotAuctionPool>"`.
- The autoscaler targets a single "heavy vertex" via substring match. Set `HEAVY_VERTEX_PATTERN` per query: `"hot-items-count"` for Q5, `"new-users-join"` for Q8. Default = `"CPU Load Simulator"` (backwards-compat with ConfigurableGraphJob).
- Result subdir convention: `results/q5-{const,sine,step}/{STRATEGY}/` and `results/q8-{const,sine,step}/{STRATEGY}/`. `plot_results.py --benchmark q5|q8|all` picks them up; output PNGs/CSVs get suffixed `_q5`/`_q8`.
- Vertex names emitted with `uid()` in NexmarkRealJob: `nexmark-source`, `filter-bids`/`filter-persons`/`filter-auctions`, `project-bid`/`project-person`/`project-auction`, `to-tuple`, `hot-items-count`, `top-hot-auction`, `q5-sink`, `new-users-join`, `q8-sink`. Pin these — autoscaler.sh greps them.
- Operator chaining is DISABLED in NexmarkRealJob (`env.disableOperatorChaining()`) so each vertex shows up separately in `/jobs/{id}/vertices` for the autoscaler to inspect. Don't re-enable.
