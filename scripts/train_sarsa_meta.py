#!/usr/bin/env python3
"""
Train a tabular SARSA Q-table for the SARSA_META meta-scheduler (Phase 5).

Motivation
----------
OFFLINE_BANDIT (V1..V5) is a LinUCB meta-scheduler: it regresses the run
reward *linearly* on the continuous context and picks the arm with the
highest UCB score. The professor's open task was to characterise SARSA as a
*meta-optimizer* (not as a base arm — that failed by cold start, see V4 vs
V4_WITH_SARSA). SARSA_META does exactly that:

  * State  = a small subset of the cluster context, discretised into bins
             (LOW/MED/HIGH per feature via training-data terciles).
  * Action = index of the base arm (FCFS / BALANCED / LEAST_LOADED / BANDIT).
  * Q(s,a) = expected normalised throughput/core of committing to arm a in
             discretised cluster-state s, learned by on-policy TD(0).

Why SARSA works here where SARSA-as-an-arm did not
--------------------------------------------------
Each historical run used ONE base strategy for its whole duration, so a run
is an on-policy trajectory of the constant-arm behaviour policy:

    s_0 --a--> s_1 --a--> ... --a--> s_T      reward R = norm. Tput/core

Because the arm is fixed within a run, the SARSA bootstrap action a' == a, so
the update is a clean policy-evaluation of "commit to arm a":

    Q(s_t,a) <- Q(s_t,a) + alpha [ r_t + gamma * Q(s_{t+1},a) - Q(s_t,a) ]

The greedy meta-policy is pi(s) = argmax_a Q(s,a). There is no cold start: the
Q-table is fully trained offline and only *read* at runtime, exactly like the
LinUCB weights.

Reward shaping
--------------
  --reward-shaping per_step  (default): r_t = R for every step. Fixed point is
        Q(s,a) = R / (1 - gamma); the 1/(1-gamma) factor is constant across
        arms so the argmax (the policy) is unchanged, while TD backups share
        value across states co-visited in a trajectory -> generalisation to
        nearby states. Robust to short trajectories.
  --reward-shaping terminal: r_t = 0 for t<T, r_T = R. Textbook terminal-reward
        SARSA; signal decays as gamma^(T-t) so use a high gamma.

State features default to host metrics that are well-populated across all
q2-{const,sine,step} runs (busy_inst is ~0 in the q2-sine logs, so it is NOT
in the default state — pass --state-features to include it once fixed runs
exist).

Output: scheduler/src/main/resources/sarsa_meta_weights.json, read at startup
by SarsaMetaStrategy.java.
"""
import argparse
import json
import random
import sys
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

import numpy as np

# Reuse the exact feature pipeline used by the LinUCB trainer so the runtime
# context vector (built identically in Java) lines up feature-for-feature.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_offline_bandit import (  # noqa: E402
    parse_snapshots,
    parse_reward,
    context_vector,
    FEATURE_NAMES,
)

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
DEFAULT_OUT = ROOT / "scheduler/src/main/resources/sarsa_meta_weights.json"

DEFAULT_ARMS = ["FCFS", "BALANCED", "LEAST_LOADED", "BANDIT"]
DEFAULT_DISTS = ["q2-const", "q2-sine", "q2-step"]
# Host-metric features that are populated in every q2 run. busy_inst is left
# out on purpose (it is ~0 in the q2-sine logs and would degenerate the state).
DEFAULT_STATE_FEATURES = ["saturation", "cpu_imbalance", "mem_imbalance"]

DECISION_INTERVAL_MS = 30_000


def collect_trajectories(dists, arms, reward_mode):
    """Return (trajectories, max_reward).

    trajectories: list of dicts {arm, contexts: list[np.ndarray(13)], reward}.
    contexts hold the FULL 13-dim feature vector; the state subset is selected
    later so the same dataset can be rebinned without re-parsing logs.
    """
    trajectories = []
    for dist in dists:
        for arm in arms:
            run_dir = RESULTS / dist / arm
            tp = parse_reward(run_dir / "METRICS-SUMMARY.txt", reward_mode)
            snaps = parse_snapshots(run_dir / "autoscaler.log")
            if tp is None or tp <= 0 or not snaps:
                print(f"  [skip] {dist}/{arm} ({reward_mode}={tp}, snaps={len(snaps)})")
                continue
            first_ts = snaps[0]["ts"]
            ctxs, prev = [], None
            for s in snaps:
                ctxs.append(context_vector(s, prev, first_ts))
                prev = s
            trajectories.append({"arm": arm, "contexts": ctxs, "reward": float(tp)})
            print(f"  [load] {dist}/{arm}: {len(ctxs)} snapshots, {reward_mode}={tp}")

    if not trajectories:
        sys.exit("No usable runs found. Check results/<dist>/<arm>/autoscaler.log.")
    max_reward = max(t["reward"] for t in trajectories)
    print(f"Reward normalisation: max {reward_mode} = {max_reward:.0f} ev/s")
    return trajectories, max_reward


def compute_bin_edges(trajectories, feature_idx, bins):
    """Per-feature inner quantile edges (bins-1 of them) pooled over all rows."""
    edges = {}
    for name, idx in feature_idx.items():
        vals = np.array([c[idx] for t in trajectories for c in t["contexts"]])
        qs = [k / bins for k in range(1, bins)]            # e.g. [1/3, 2/3]
        e = [float(np.quantile(vals, q)) for q in qs]
        # Guard against degenerate (all-equal) features producing flat edges.
        for i in range(1, len(e)):
            if e[i] <= e[i - 1]:
                e[i] = e[i - 1] + 1e-9
        edges[name] = e
    return edges


def discretize(ctx, feature_idx, bin_edges):
    """Map a full context vector to a state key like '0,2,1'."""
    parts = []
    for name, idx in feature_idx.items():
        parts.append(str(bisect_right(bin_edges[name], ctx[idx])))
    return ",".join(parts)


def train_sarsa(trajectories, arms, feature_idx, bin_edges, max_reward,
                gamma, alpha0, epochs, shaping, seed):
    """On-policy TD(0) SARSA over fixed-arm trajectories. Returns (Q, visits)."""
    rng = random.Random(seed)
    # Q[state][arm] and visit counts, created lazily.
    Q = {}
    visits = {}

    def ensure(state):
        if state not in Q:
            Q[state] = {a: 0.0 for a in arms}
            visits[state] = {a: 0 for a in arms}

    order = list(range(len(trajectories)))
    for ep in range(epochs):
        # Decaying learning rate for convergence.
        alpha = alpha0 * (1.0 - ep / max(1, epochs)) + 1e-3
        rng.shuffle(order)
        for ti in order:
            tr = trajectories[ti]
            a = tr["arm"]
            R = tr["reward"] / max_reward
            states = [discretize(c, feature_idx, bin_edges) for c in tr["contexts"]]
            for st in states:
                ensure(st)
            T = len(states)
            for t in range(T):
                s = states[t]
                if shaping == "per_step":
                    r = R
                else:  # terminal
                    r = R if t == T - 1 else 0.0
                if t < T - 1:
                    target = r + gamma * Q[states[t + 1]][a]   # a' == a (fixed arm)
                else:
                    target = r                                  # terminal bootstrap
                Q[s][a] += alpha * (target - Q[s][a])
                if ep == epochs - 1:
                    visits[s][a] += 1
    return Q, visits


def marginal_arm(trajectories, arms, max_reward):
    """Best arm by mean normalised reward — fallback for unseen runtime states."""
    sums = {a: 0.0 for a in arms}
    counts = {a: 0 for a in arms}
    for tr in trajectories:
        sums[tr["arm"]] += tr["reward"] / max_reward
        counts[tr["arm"]] += 1
    means = {a: (sums[a] / counts[a]) if counts[a] else 0.0 for a in arms}
    best = max(means, key=means.get)
    return best, means


def policy_summary(Q, arms):
    """Distribution of greedy argmax arms across learned states."""
    dist = {a: 0 for a in arms}
    for st, qa in Q.items():
        best = max(qa, key=qa.get)
        dist[best] += 1
    return dist


def main():
    p = argparse.ArgumentParser(description="Train SARSA_META tabular Q-table.")
    p.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    p.add_argument("--dists", default=",".join(DEFAULT_DISTS))
    p.add_argument("--reward", default="throughput_per_core",
                   choices=["throughput", "throughput_per_core"])
    p.add_argument("--state-features", default=",".join(DEFAULT_STATE_FEATURES),
                   help=f"Comma-separated subset of {FEATURE_NAMES}")
    p.add_argument("--bins", type=int, default=3, help="Bins per feature (LOW/MED/HIGH=3).")
    p.add_argument("--gamma", type=float, default=0.9)
    p.add_argument("--alpha", type=float, default=0.1, help="Initial learning rate.")
    p.add_argument("--epochs", type=int, default=400)
    p.add_argument("--reward-shaping", default="per_step", choices=["per_step", "terminal"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    dists = [d.strip() for d in args.dists.split(",") if d.strip()]
    state_features = [f.strip() for f in args.state_features.split(",") if f.strip()]
    for f in state_features:
        if f not in FEATURE_NAMES:
            sys.exit(f"Unknown state feature '{f}'. Choose from {FEATURE_NAMES}")
    if args.bins < 2:
        sys.exit("--bins must be >= 2")
    feature_idx = {f: FEATURE_NAMES.index(f) for f in state_features}

    print(f"Arms:           {arms}")
    print(f"Dists:          {dists}")
    print(f"Reward:         {args.reward}")
    print(f"State features: {state_features} (idx {[feature_idx[f] for f in state_features]})")
    print(f"Bins:           {args.bins}  gamma={args.gamma} alpha={args.alpha} "
          f"epochs={args.epochs} shaping={args.reward_shaping}")
    print(f"Reading runs from {RESULTS}\n")

    trajectories, max_reward = collect_trajectories(dists, arms, args.reward)
    bin_edges = compute_bin_edges(trajectories, feature_idx, args.bins)
    print("\nBin edges (inner quantiles):")
    for f in state_features:
        print(f"  {f:<14} {[round(e, 4) for e in bin_edges[f]]}")

    Q, visits = train_sarsa(
        trajectories, arms, feature_idx, bin_edges, max_reward,
        gamma=args.gamma, alpha0=args.alpha, epochs=args.epochs,
        shaping=args.reward_shaping, seed=args.seed)

    default_arm, arm_means = marginal_arm(trajectories, arms, max_reward)
    pol = policy_summary(Q, arms)

    print(f"\nMean normalised reward per arm: "
          f"{ {a: round(v, 3) for a, v in arm_means.items()} }")
    print(f"Fallback (marginal) arm for unseen states: {default_arm}")
    print(f"Learned states: {len(Q)}")
    print(f"Greedy policy arm distribution over states: {pol}")

    payload = {
        "type": "sarsa_meta",
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "arms": arms,
        "state_features": state_features,
        "bins": args.bins,
        "bin_edges": {f: bin_edges[f] for f in state_features},
        "gamma": args.gamma,
        "alpha": args.alpha,
        "epochs": args.epochs,
        "reward_mode": args.reward,
        "reward_shaping": args.reward_shaping,
        "decision_interval_ms": DECISION_INTERVAL_MS,
        "max_reward_used_for_normalisation": float(max_reward),
        "default_arm": default_arm,
        "q": {st: {a: float(qa[a]) for a in arms} for st, qa in Q.items()},
        "diagnostics": {
            "arm_mean_reward": {a: float(v) for a, v in arm_means.items()},
            "policy_arm_distribution": pol,
            "state_visits_last_epoch": visits,
            "num_states": len(Q),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    try:
        rel = out.resolve().relative_to(ROOT)
    except ValueError:
        rel = out.resolve()
    print(f"\nWrote {rel}")
    print("Rebuild + redeploy the scheduler to pick up the Q-table:")
    print("  ./force-scheduler-change.sh   (or cd scheduler && mvn clean package && docker build ...)")


if __name__ == "__main__":
    main()
