---
name: Known pitfalls and operational gotchas
description: Things that have caused silent failures or wasted time — check these first when debugging
type: feedback
originSessionId: 1132784d-0659-4570-bd97-16aa76782774
modified: 2026-08-04T22:44:15.254Z
---
**FIXED_STRATEGY=ADAPTIVE crashes the scheduler** — The enum in Java has no `ADAPTIVE` value. To run in adaptive mode, omit the env var entirely (do not set it to "ADAPTIVE").
**Why**: Java enum `valueOf("ADAPTIVE")` throws IllegalArgumentException at startup.
**How to apply**: When setting up adaptive mode experiments, unset FIXED_STRATEGY rather than setting it to "ADAPTIVE".

---

**Namespace resets to `default` after every minikube restart** — Always run `kubectl config set-context --current --namespace=flink` after starting minikube.
**Why**: Minikube does not persist context namespace across restarts.
**How to apply**: Add this to any setup checklist or script that starts minikube.

---

**Nexmark JAR is lost on every JobManager pod restart** — Must re-upload with `kubectl cp` after every JM restart. The JAR lives at `/tmp/nexmark.jar` inside the pod.
**Why**: Ephemeral pod filesystem.
**How to apply**: run-experiment-common.sh now auto-uploads if missing. If running submission manually, verify with `kubectl exec deployment/flink-jobmanager -- ls /tmp/nexmark.jar`.

---

**CRLF line endings in scripts from /mnt/c/** — Scripts copied from Windows paths get CRLF. Run `sed -i 's/\r$//'` or `dos2unix` on any script that comes from /mnt/c/.
**Why**: Bash on Linux fails with "bad interpreter" or silent parsing errors on CRLF scripts.

---

**Backup files in scheduler/src/ corrupt the shaded JAR** — `.backup` and `.ORIGINAL` files in scheduler/src/ get picked up by Maven and corrupt the uber-jar. They're in .gitignore but watch for them if Maven builds behave strangely.
**Why**: Maven shade plugin includes everything under src/ unless explicitly excluded.

---

**Editing flink-manifests.yaml does NOT change cluster state** — Easy to assume changes are live. The `jobmanager.scheduler: adaptive` config sat in the YAML for a long time without being applied.
**Why**: Manifests are version-controlled config, not live state. Need explicit `kubectl apply -f` + JM restart.
**How to apply**: After editing flink-manifests.yaml, always `kubectl apply -f kubernetes/flink-manifests.yaml && kubectl rollout restart deployment/flink-jobmanager` and verify with `curl http://localhost:8081/config | grep scheduler`.

---

**Flink PUT /jobs/{id}/resource-requirements rejects partial vertex payloads** — If you only send the vertex you want to rescale, Flink returns an error. The PUT must include ALL vertices in the job.
**Why**: API treats the payload as the new full target spec, not a delta.
**How to apply**: GET /jobs/{id} first, iterate vertices, set non-target ones to `lowerBound=upperBound=current_par`. autoscaler.sh already does this.

---

**Cumulative busy-time metric is misleading when staleness drops events** — Using cumulative `accumulated-busy-time` to compute busy% breaks under backpressure. When CPU-Load is overloaded, events age in upstream buffers, get stale-dropped BEFORE the CPU work starts, so accumulated-busy-time stays flat → busy% looks low → autoscaler thinks load is fine.
**Why**: Stale-drop fires inside CPU-Load before the work loop, so the time spent on dropped events is never counted.
**How to apply**: Use instantaneous busy% from delta over a window: `Δaccumulated_busy / (Δt × parallelism)`. Even better for autoscaling under stale-drop policy: use filter backpressure as the signal instead.

---

**Recompile + reupload JAR after any GraphConfig.fromArgs change** — If the running JAR is old and you add a new arg position, the value at that index gets parsed as the next arg in the old order. Concrete past failure: passing `cpuLoadParallelism=2` at args[6] but old JAR didn't know about it → got parsed as `maxEventAgeMs=2` → all events stale-dropped at 2ms.
**Why**: The submitted args list is positional; mismatch between arg order in script and arg order in JAR causes silent semantic shifts.
**How to apply**: After ANY change to GraphConfig.fromArgs: `mvn clean package` in flink-nexmark-job/, then `kubectl cp` to JM (run-experiment-common.sh auto-uploads if missing).

---

**Checkpoints go to a POD-LOCAL path — no shared storage, so state cannot survive moving between TaskManagers** — `state.checkpoints.dir: file:///tmp/checkpoints` (flink-manifests.yaml:95,141) with NO volume mounted on the TMs (verified 2026-08-04: their only volumeMount is the serviceaccount token, and `/tmp/checkpoints` does not even exist on a TM pod). Each pod's `/tmp` is its own ephemeral disk.
**Why**: it works today only by accident — the state is small enough that Flink inlines it in the checkpoint metadata instead of writing files (JM's `/tmp/checkpoints` = 100K total, `shared/` and `taskowned/` empty). Once state grows past the inline threshold it gets written as files on the writing TM's local disk, and a task recovering on a different TM will look for a path that does not exist in its pod.
**How to apply**: before any experiment that depends on real state surviving a rescale (Nexmark Q5/Q8 windows, and any measurement of the balance-vs-state-locality trade-off), give checkpoints shared storage — PVC, NFS, MinIO/S3, or a hostPath if everything is pinned to one node. Also worth re-checking whether past Q5/Q8 runs actually persisted state or just rode the inline path. Related: [[project_flink_core_fork]].

---

**Nexmark env vars (JOB_CLASS, EXTRA_JOB_ARGS, HEAVY_VERTEX_PATTERN) silently leak from parent shell into old-benchmark scripts** — If the user previously sourced `experiment-q5.sh` / `experiment-q8.sh` in their interactive terminal, those env vars stay exported. Subsequent runs of `experiment-offline-v2.sh` / `experiment-offline.sh` / `experiment-default.sh` / the simple per-strategy scripts inherit them and silently run `NexmarkRealJob` instead of `ConfigurableGraphJob`, writing results into `results/autoscaler-*/...` that look fine but contain Q5/Q8 data instead of synthetic-graph data.
**Why**: `run-experiment-common.sh` checks `if [ -n "${JOB_CLASS:-}" ]` to add `-c CLASS` to the flink submit. A leaked non-empty value passes that check.
**How to apply**: Confirmed fix in `run-experiment-common.sh`: when `WORKLOAD == autoscaler-*`, the function `unset`s all three Nexmark env vars defensively. The three multi-dist wrappers (offline, offline-v2, default) also `unset` at top as belt-and-suspenders. Detection: `jq -r '.name' results/autoscaler-*/STRATEGY/job-details.json` should return `Nexmark-Config[...]`. If it returns `Nexmark-Q5` or `Nexmark-Q8`, the run is corrupt and must be redone.

---

**`kubectl port-forward` dies mid-run and leaves the PROCESS alive with a dead tunnel** — every
`curl localhost:8081` then returns empty, and any script that reads job state through it concludes the
job is not running. Cost 4 of 6 arms of a 2-hour run on 2026-08-11: the jobs had been submitted and
were running fine; the driver simply could not see them, waited out its RUNNING timeout and skipped
each arm. `pgrep` still showed the forwarder, so "is it running" is the wrong check.
**Why**: the forwarder survives a connection reset without re-establishing the tunnel, so it keeps
answering the port while forwarding nothing. Long runs (campaign, placement experiment) are exactly
where it happens.
**How to apply**: never test the tunnel with `pgrep` — test it with an actual request
(`curl -s -m 3 localhost:8081/overview`). For job control prefer no tunnel at all:
`kubectl exec -n flink deployment/flink-jobmanager -- curl -s http://localhost:8081/<path>`, which is
what `scripts/run-placement-experiment.sh` now does. Where a real HTTP endpoint IS required
(`arm_controller.py --rest`), keep a watchdog that re-checks every ~15s and restarts the forwarder,
killing the stale one first — a dead tunnel is worse than no tunnel. `scripts/run-fork-campaign.sh`
still depends on the host port-forward and has NOT been hardened.
