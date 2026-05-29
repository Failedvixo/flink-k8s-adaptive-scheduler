#!/usr/bin/env python3
"""
Train LinUCB weights for the OFFLINE_BANDIT meta-scheduler.

Each ARM is a base scheduling strategy. For each historical run we:
  1. Parse autoscaler.log to get cluster context snapshots (CPU%, mem% per node).
  2. Read METRICS-SUMMARY.txt to get the run's reward signal.
     --reward throughput          (default) → 'Throughput: N ev/s'
     --reward throughput_per_core         → 'Throughput / core: N ev/s'  (Nexmark block)
  3. Emit one (context_vector, reward) sample per snapshot,
     where reward = raw_reward_of_run / max_raw_reward_across_runs.

Training scope (DISTS below) is unchanged on purpose: we still train on the
synthetic-graph benchmark only so that Q5 / Q8 evaluations remain zero-shot
out-of-distribution tests of generalisation.

Then for each arm we fit ridge regression in closed form:
        A     = Xᵀ X + λ I
        b     = Xᵀ r
        θ     = A⁻¹ b
        A_inv = A⁻¹

Result is dumped to scheduler/src/main/resources/offline_bandit_weights.json
(picked up at scheduler startup by OfflineBanditStrategy.java).

Usage:  python3 scripts/train_offline_bandit.py
"""
import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
DEFAULT_OUT = ROOT / "scheduler/src/main/resources/offline_bandit_weights.json"
DEFAULT_ARMS = ["FCFS", "BALANCED", "SARSA"]
# Globals reassigned from argparse in main().
OUT: Path = DEFAULT_OUT
ARMS: list = list(DEFAULT_ARMS)
DISTS = ["autoscaler-const", "autoscaler-sine", "autoscaler-step"]
FEATURE_NAMES = [
    "bias",
    "avg_cpu",
    "max_cpu",
    "min_cpu",
    "cpu_imbalance",
    "cpu_velocity",   # (avg_cpu - prev_avg_cpu) / dt_sec  / 10  → typical [-0.2, 0.2]
    "avg_mem",
    "elapsed_norm",   # seconds since first snapshot / 600
    "saturation",     # min(1.25, max_cpu / 80)
]
FEATURE_DIM = len(FEATURE_NAMES)
ALPHA_RUNTIME = 1.0
DECISION_INTERVAL_MS = 30_000
RIDGE_LAMBDA = 1.0
ELAPSED_NORM_SEC = 600.0
SAT_THRESHOLD = 80.0
VELOCITY_SCALE = 10.0

RX_TS_NODE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+(\S+)\s+\d+m\s+(\d+)%\s+\S+\s+(\d+)%"
)
RX_THROUGHPUT = re.compile(r"^\s*Throughput:\s+(\d+)\s+ev/s", re.MULTILINE)
RX_TPUT_PER_CORE = re.compile(r"^\s*Throughput / core:\s+([\d,]+)\s+ev/s", re.MULTILINE)

# Reassigned from argparse in main(). 'throughput' keeps V1 behaviour intact;
# 'throughput_per_core' aligns the training signal with the efficiency metric
# we report in the thesis (Cost/Mevent and Tput/core).
REWARD_MODE = "throughput"


def parse_snapshots(autoscaler_log: Path):
    """Return a list of dicts: [{ts:int, cpu:{node:pct}, mem:{node:pct}}, ...]."""
    if not autoscaler_log.exists():
        return []
    snapshots = {}
    for line in autoscaler_log.read_text(errors="replace").splitlines():
        m = RX_TS_NODE.match(line)
        if not m:
            continue
        h, mn, s, node, cpu_pct, mem_pct = m.groups()
        ts = int(h) * 3600 + int(mn) * 60 + int(s)
        snap = snapshots.setdefault(ts, {"ts": ts, "cpu": {}, "mem": {}})
        snap["cpu"][node] = float(cpu_pct)
        snap["mem"][node] = float(mem_pct)
    snaps = sorted(snapshots.values(), key=lambda s: s["ts"])
    # Keep only snapshots with at least one node populated.
    return [s for s in snaps if s["cpu"]]


def parse_reward(metrics_summary: Path, mode: str):
    """Return the raw reward signal for a run, or None if unavailable."""
    if not metrics_summary.exists():
        return None
    text = metrics_summary.read_text(errors="replace")
    if mode == "throughput_per_core":
        m = RX_TPUT_PER_CORE.search(text)
        return int(m.group(1).replace(",", "")) if m else None
    m = RX_THROUGHPUT.search(text)
    return int(m.group(1)) if m else None


def context_vector(snap, prev_snap, first_ts):
    cpus = list(snap["cpu"].values())
    mems = list(snap["mem"].values())
    avg_cpu = sum(cpus) / len(cpus)
    max_cpu = max(cpus)
    min_cpu = min(cpus)
    imbalance = max(0.0, max_cpu - min_cpu)
    avg_mem = sum(mems) / len(mems) if mems else 0.0

    if prev_snap is not None:
        prev_cpus = list(prev_snap["cpu"].values())
        prev_avg = sum(prev_cpus) / len(prev_cpus) if prev_cpus else avg_cpu
        dt_sec = max(1, snap["ts"] - prev_snap["ts"])
        velocity_per_sec = (avg_cpu - prev_avg) / dt_sec
    else:
        velocity_per_sec = 0.0

    elapsed_sec = max(0.0, snap["ts"] - first_ts)
    saturation = min(1.25, max_cpu / SAT_THRESHOLD)

    return np.array([
        1.0,
        avg_cpu / 100.0,
        max_cpu / 100.0,
        min_cpu / 100.0,
        imbalance / 100.0,
        velocity_per_sec / VELOCITY_SCALE,
        avg_mem / 100.0,
        elapsed_sec / ELAPSED_NORM_SEC,
        saturation,
    ])


def collect_dataset():
    """Return {arm: (X, r)} where X is (n, d) and r is (n,)."""
    runs = []   # list of (arm, context_list, raw_reward)
    for dist in DISTS:
        for arm in ARMS:
            run_dir = RESULTS / dist / arm
            tp = parse_reward(run_dir / "METRICS-SUMMARY.txt", REWARD_MODE)
            snaps = parse_snapshots(run_dir / "autoscaler.log")
            if tp is None or tp <= 0 or not snaps:
                print(f"  [skip] {dist}/{arm} ({REWARD_MODE}={tp}, snaps={len(snaps)})")
                continue
            first_ts = snaps[0]["ts"]
            ctxs = []
            prev = None
            for s in snaps:
                ctxs.append(context_vector(s, prev, first_ts))
                prev = s
            runs.append((arm, ctxs, tp))
            print(f"  [load] {dist}/{arm}: {len(ctxs)} snapshots, {REWARD_MODE}={tp}")

    if not runs:
        sys.exit("No usable runs found. Make sure results/autoscaler-* are populated.")

    # Reward normalisation: divide by max raw reward so r ∈ [0, 1].
    max_tp = max(tp for _, _, tp in runs)
    print(f"Reward normalisation: max {REWARD_MODE} = {max_tp} ev/s")

    by_arm = {arm: ([], []) for arm in ARMS}
    for arm, ctxs, tp in runs:
        r = tp / max_tp
        for x in ctxs:
            by_arm[arm][0].append(x)
            by_arm[arm][1].append(r)

    out = {}
    for arm, (xs, rs) in by_arm.items():
        if not xs:
            print(f"  [warn] arm {arm} has no samples — using zero/identity")
            out[arm] = (np.zeros((0, FEATURE_DIM)), np.zeros(0))
        else:
            out[arm] = (np.vstack(xs), np.array(rs))
    return out, max_tp


def fit_arm(X: np.ndarray, r: np.ndarray):
    """Closed-form ridge regression. Returns (theta, A_inv)."""
    if X.shape[0] == 0:
        A = RIDGE_LAMBDA * np.eye(FEATURE_DIM)
        A_inv = np.linalg.inv(A)
        theta = np.zeros(FEATURE_DIM)
        return theta, A_inv
    A = X.T @ X + RIDGE_LAMBDA * np.eye(FEATURE_DIM)
    b = X.T @ r
    A_inv = np.linalg.inv(A)
    theta = A_inv @ b
    return theta, A_inv


def main():
    global ARMS, OUT, REWARD_MODE
    parser = argparse.ArgumentParser(description="Train LinUCB weights for OFFLINE_BANDIT.")
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                        help=f"Comma-separated arms. Default: {','.join(DEFAULT_ARMS)}")
    parser.add_argument("--reward", default="throughput",
                        choices=["throughput", "throughput_per_core"],
                        help="Reward signal. throughput_per_core uses the Nexmark "
                             "canonical metric and targets efficiency over absolute volume.")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"Output weights JSON path. Default: {DEFAULT_OUT.relative_to(ROOT)}")
    args = parser.parse_args()
    ARMS = [a.strip() for a in args.arms.split(",") if a.strip()]
    REWARD_MODE = args.reward
    OUT = Path(args.out)
    print(f"Arms:   {ARMS}")
    print(f"Reward: {REWARD_MODE}")
    print(f"Output: {OUT}")
    print(f"Reading runs from {RESULTS}")
    dataset, max_tp = collect_dataset()

    arm_payload = {}
    print("\nFitting per-arm ridge regression:")
    for arm in ARMS:
        X, r = dataset[arm]
        theta, A_inv = fit_arm(X, r)
        arm_payload[arm] = {
            "samples":    int(X.shape[0]),
            "mean_reward": float(r.mean()) if r.size else 0.0,
            "theta":      [float(v) for v in theta],
            "A_inv":      [[float(v) for v in row] for row in A_inv],
        }
        print(f"  {arm:<10} n={X.shape[0]:<5} mean_r={arm_payload[arm]['mean_reward']:.3f} "
              f"theta={[round(t,3) for t in theta]}")

    payload = {
        "feature_dim": FEATURE_DIM,
        "alpha": ALPHA_RUNTIME,
        "decision_interval_ms": DECISION_INTERVAL_MS,
        "feature_names": FEATURE_NAMES,
        "ridge_lambda": RIDGE_LAMBDA,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "reward_mode": REWARD_MODE,
        "max_reward_used_for_normalisation": int(max_tp),
        "arms_trained": ARMS,
        "arms": {
            arm: {"theta": arm_payload[arm]["theta"],
                  "A_inv": arm_payload[arm]["A_inv"]}
            for arm in ARMS
        },
        "diagnostics": {
            arm: {"samples": arm_payload[arm]["samples"],
                  "mean_reward": arm_payload[arm]["mean_reward"]}
            for arm in ARMS
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    out_resolved = OUT.resolve()
    try:
        rel = out_resolved.relative_to(ROOT)
    except ValueError:
        rel = out_resolved
    print(f"\nWrote {rel}")
    print("Rebuild and redeploy the scheduler to pick up the new weights:")
    print("  cd scheduler && mvn clean package && docker build -t adaptive-scheduler:offline-bandit .")


if __name__ == "__main__":
    main()
