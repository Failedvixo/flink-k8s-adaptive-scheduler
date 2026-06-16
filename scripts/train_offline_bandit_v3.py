#!/usr/bin/env python3
"""
Train OFFLINE_BANDIT_V3 using ProPS+ — Prompted Policy Search.

Each arm is a base scheduling strategy. Instead of fitting ridge regression to
predict per-arm reward (what train_offline_bandit.py does), we optimise the
*policy* directly: at runtime the meta-scheduler picks argmax_a theta_a · x,
so we score a candidate (theta_BANDIT, theta_LEAST_LOADED, theta_BALANCED) by
its mean counterfactual reward over historical snapshots — for each snapshot,
predict the arm and look up that arm's actual Tput/core in that distribution.

An LLM (Qwen2.5-7B-Instruct via Ollama by default) is the optimiser: it sees a
history of (params, reward) pairs and proposes the next parameter vector. This
mirrors the ProPS / ProPS+ loop from Zhou et al. NeurIPS 2025.

Training scope is autoscaler-* only — Q5 / Q8 evaluations stay zero-shot.

Usage:
  python3 scripts/train_offline_bandit_v3.py            # ollama backend
  python3 scripts/train_offline_bandit_v3.py --backend mock  # pipeline test
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
DEFAULT_OUT = ROOT / "scheduler/src/main/resources/offline_bandit_v3_weights.json"
DEFAULT_ARMS = ["BANDIT", "LEAST_LOADED", "BALANCED"]
DISTS = ["autoscaler-const", "autoscaler-sine", "autoscaler-step"]
FEATURE_NAMES = [
    "bias", "avg_cpu", "max_cpu", "min_cpu", "cpu_imbalance",
    "cpu_velocity", "avg_mem", "elapsed_norm", "saturation",
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
RX_TPUT_PER_CORE = re.compile(r"^\s*Throughput / core:\s+([\d,]+)\s+ev/s", re.MULTILINE)
RX_PARAMS = re.compile(r"params\[(\d+)\]\s*[:=]\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")


# ---------------------------------------------------------------- data loading

def parse_snapshots(autoscaler_log: Path):
    if not autoscaler_log.exists():
        return []
    snaps = {}
    for line in autoscaler_log.read_text(errors="replace").splitlines():
        m = RX_TS_NODE.match(line)
        if not m:
            continue
        h, mn, s, node, cpu_pct, mem_pct = m.groups()
        ts = int(h) * 3600 + int(mn) * 60 + int(s)
        snap = snaps.setdefault(ts, {"ts": ts, "cpu": {}, "mem": {}})
        snap["cpu"][node] = float(cpu_pct)
        snap["mem"][node] = float(mem_pct)
    out = sorted(snaps.values(), key=lambda s: s["ts"])
    return [s for s in out if s["cpu"]]


def parse_tputpercore(metrics_summary: Path):
    if not metrics_summary.exists():
        return None
    text = metrics_summary.read_text(errors="replace")
    m = RX_TPUT_PER_CORE.search(text)
    return int(m.group(1).replace(",", "")) if m else None


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
        velocity = (avg_cpu - prev_avg) / dt_sec
    else:
        velocity = 0.0
    elapsed = max(0.0, snap["ts"] - first_ts)
    saturation = min(1.25, max_cpu / SAT_THRESHOLD)
    return np.array([
        1.0,
        avg_cpu / 100.0, max_cpu / 100.0, min_cpu / 100.0,
        imbalance / 100.0, velocity / VELOCITY_SCALE,
        avg_mem / 100.0, elapsed / ELAPSED_NORM_SEC, saturation,
    ])


def load_dataset(arms, dists):
    """Return (rewards{(dist,arm)->tpc}, snapshots[(dist,ctx)...], arm_X{arm->[ctx]})."""
    rewards = {}
    snapshots = []
    arm_X = {a: [] for a in arms}
    for dist in dists:
        for arm in arms:
            run_dir = RESULTS / dist / arm
            tpc = parse_tputpercore(run_dir / "METRICS-SUMMARY.txt")
            snaps = parse_snapshots(run_dir / "autoscaler.log")
            if tpc is None or tpc <= 0 or not snaps:
                print(f"  [skip] {dist}/{arm} tpc={tpc} snaps={len(snaps)}")
                continue
            rewards[(dist, arm)] = float(tpc)
            first_ts = snaps[0]["ts"]
            prev = None
            for s in snaps:
                ctx = context_vector(s, prev, first_ts)
                arm_X[arm].append(ctx)
                snapshots.append((dist, ctx))
                prev = s
            print(f"  [load] {dist}/{arm}: snaps={len(snaps)} tpc={tpc}")
    return rewards, snapshots, arm_X


# ---------------------------------------------------- policy evaluation

def score_policy(params, arms, snapshots, rewards, normalisation,
                 soft_temperature=0.0):
    """Counterfactual mean Tput/core, normalised.

    soft_temperature == 0  -> argmax-arm policy (original, piecewise-constant).
    soft_temperature  > 0  -> softmax-weighted expected reward (smooth in theta).
                              Used during training to give LLM a gradient signal.
                              Deployed policy at runtime is still argmax, so we
                              also report the hard-argmax score elsewhere.
    """
    thetas = params.reshape(len(arms), FEATURE_DIM)
    total = 0.0
    n = 0
    for dist, ctx in snapshots:
        scores = thetas @ ctx
        if soft_temperature > 0.0:
            z = scores / soft_temperature
            z = z - z.max()                # numerically stable softmax
            p = np.exp(z)
            p = p / p.sum()
            expected = 0.0
            valid = 0.0
            for i, a in enumerate(arms):
                r = rewards.get((dist, a))
                if r is not None:
                    expected += p[i] * r
                    valid += p[i]
            if valid <= 0:
                continue
            total += expected / valid     # renormalise if some arms had no reward
        else:
            arm_name = arms[int(np.argmax(scores))]
            r = rewards.get((dist, arm_name))
            if r is None:
                continue
            total += r
        n += 1
    return (total / n) / normalisation if n else 0.0


def arm_distribution(params, arms, snapshots):
    thetas = params.reshape(len(arms), FEATURE_DIM)
    counts = {}
    for dist, ctx in snapshots:
        counts.setdefault(dist, {a: 0 for a in arms})
        chosen = arms[int(np.argmax(thetas @ ctx))]
        counts[dist][chosen] += 1
    return counts


def compute_a_inv(arm_X):
    out = {}
    for arm, xs in arm_X.items():
        if not xs:
            out[arm] = np.eye(FEATURE_DIM).tolist()
            continue
        X = np.vstack(xs)
        A = X.T @ X + RIDGE_LAMBDA * np.eye(FEATURE_DIM)
        out[arm] = np.linalg.inv(A).tolist()
    return out


# ---------------------------------------------------- LLM backends

class BaseBackend:
    name = "base"

    def propose(self, system_prompt, history_text, n_params):
        raise NotImplementedError


class MockBackend(BaseBackend):
    """Gaussian random proposals — for end-to-end pipeline tests without an LLM."""
    name = "mock"

    def __init__(self, seed=0, stddev=1.0):
        self.rng = np.random.default_rng(seed)
        self.stddev = stddev

    def propose(self, system_prompt, history_text, n_params):
        v = self.rng.normal(0.0, self.stddev, size=n_params)
        line = "; ".join(f"params[{i}]: {x:.5g}" for i, x in enumerate(v))
        return v, "mock random Gaussian proposal: " + line


class AnthropicBackend(BaseBackend):
    """Anthropic Messages API. Default model: Claude Haiku 4.5."""
    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, model, api_key, temperature, max_tokens):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens

    def propose(self, system_prompt, history_text, n_params):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": history_text}],
        }
        # Retry on 429 / 529 (overloaded) with backoff.
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(
                self.ENDPOINT,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": self.API_VERSION,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 529):
                    wait = 10 * (attempt + 1)
                    last_err = e
                    time.sleep(wait)
                    continue
                raise
        else:
            raise RuntimeError(f"Anthropic backend failed after retries: {last_err}")
        blocks = payload.get("content", [])
        content = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        matches = RX_PARAMS.findall(content)
        idx_val = {}
        for idx_str, val_str in matches:
            i = int(idx_str)
            if 0 <= i < n_params:
                idx_val[i] = float(val_str)
        missing = [i for i in range(n_params) if i not in idx_val]
        if missing:
            raise RuntimeError(
                f"LLM proposal missing params {missing[:10]}... (got {len(idx_val)}/{n_params}). "
                f"First 600 chars of response:\n{content[:600]}"
            )
        v = np.array([idx_val[i] for i in range(n_params)])
        return v, content


class GeminiBackend(BaseBackend):
    """Google Generative Language API (free tier supports gemini-1.5-pro/flash)."""
    name = "gemini"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    def __init__(self, model, api_key, temperature, max_tokens, throttle_sec=0.0):
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.throttle_sec = throttle_sec

    def propose(self, system_prompt, history_text, n_params):
        if self.throttle_sec > 0:
            time.sleep(self.throttle_sec)
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": history_text}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                # Gemini 2.5 series defaults to "thinking" mode which consumes
                # the maxOutputTokens budget before the actual output, causing
                # mid-vector truncation. We don't need chain-of-thought for the
                # structured numeric output, so disable it.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        url = self.ENDPOINT.format(model=self.model, key=self.api_key)
        # Retry with exponential backoff on 429.
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 30 * (attempt + 1)
                    last_err = e
                    time.sleep(wait)
                    continue
                raise
        else:
            raise RuntimeError(f"Gemini 429 after retries: {last_err}")
        candidates = payload.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates. Payload: {json.dumps(payload)[:500]}")
        parts = candidates[0].get("content", {}).get("parts", [])
        content = "".join(p.get("text", "") for p in parts)
        matches = RX_PARAMS.findall(content)
        idx_val = {}
        for idx_str, val_str in matches:
            i = int(idx_str)
            if 0 <= i < n_params:
                idx_val[i] = float(val_str)
        missing = [i for i in range(n_params) if i not in idx_val]
        if missing:
            raise RuntimeError(
                f"LLM proposal missing params {missing[:10]}... (got {len(idx_val)}/{n_params}). "
                f"First 600 chars of response:\n{content[:600]}"
            )
        v = np.array([idx_val[i] for i in range(n_params)])
        return v, content


class OllamaBackend(BaseBackend):
    name = "ollama"

    def __init__(self, model, host, temperature, max_tokens, num_ctx):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.num_ctx = num_ctx

    def propose(self, system_prompt, history_text, n_params):
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": history_text},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "num_ctx": self.num_ctx,
            },
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["message"]["content"]
        matches = RX_PARAMS.findall(content)
        idx_val = {}
        for idx_str, val_str in matches:
            i = int(idx_str)
            if 0 <= i < n_params:
                idx_val[i] = float(val_str)
        missing = [i for i in range(n_params) if i not in idx_val]
        if missing:
            raise RuntimeError(
                f"LLM proposal missing params {missing[:10]}... (got {len(idx_val)}/{n_params}). "
                f"First 600 chars of response:\n{content[:600]}"
            )
        v = np.array([idx_val[i] for i in range(n_params)])
        return v, content


# ---------------------------------------------------- prompt building

ARM_DESCRIPTIONS = {
    "BANDIT": ("Online UCB1 multi-armed bandit with one arm per node. "
               "Picks the node whose upper-confidence-bound on observed CPU load is highest."),
    "LEAST_LOADED": "Deterministic: pick the node with the lowest current CPU%.",
    "BALANCED": "Round-robin assignment that keeps the pod count per node as even as possible.",
    "FCFS": "First-come-first-served: place on whichever node was returned first by the API.",
    "SARSA": ("Tabular SARSA on (state, node) pairs. Discretises cluster CPU state and learns "
              "Q-values via on-policy temporal-difference updates."),
}


def build_system_prompt(arms):
    n_arms = len(arms)
    n_params = n_arms * FEATURE_DIM
    feature_block = (
        "  x[0]  bias           constant 1.0\n"
        "  x[1]  avg_cpu        mean CPU% across worker nodes / 100\n"
        "  x[2]  max_cpu        hottest node CPU% / 100\n"
        "  x[3]  min_cpu        coldest node CPU% / 100\n"
        "  x[4]  cpu_imbalance  (max_cpu - min_cpu) / 100, in [0, 1]\n"
        "  x[5]  cpu_velocity   recent rate of change of avg_cpu, /10. Can be negative.\n"
        "  x[6]  avg_mem        mean memory% across nodes / 100\n"
        "  x[7]  elapsed_norm   seconds since scheduler start / 600. Grows with time.\n"
        "  x[8]  saturation     min(1.25, max_cpu/80). >1 means hottest node is near limit."
    )
    arm_block = "\n".join(
        f"  arm[{i}] = {a}: {ARM_DESCRIPTIONS.get(a, '(no description)')}"
        for i, a in enumerate(arms)
    )
    param_block_lines = []
    for i, a in enumerate(arms):
        lo = i * FEATURE_DIM
        hi = lo + FEATURE_DIM - 1
        param_block_lines.append(f"  params[{lo}..{hi}] = theta for {a}")
    param_block = "\n".join(param_block_lines)
    return (
        "You are a numerical policy optimiser for a meta-scheduler used by an adaptive\n"
        "Flink-on-Kubernetes scheduler. The meta-scheduler picks ONE base scheduling\n"
        "strategy (an 'arm') for each meta-decision, based on the current cluster\n"
        "context x (described below). You optimise the LINEAR scoring weights for each\n"
        "arm; at runtime the scheduler picks the arm with the highest linear score:\n\n"
        "    score(arm_a) = theta_a . x           and chooses argmax_a score(arm_a)\n\n"
        f"ARMS ({n_arms}, in fixed order):\n{arm_block}\n\n"
        f"CONTEXT FEATURES ({FEATURE_DIM} dims, all roughly normalised):\n{feature_block}\n\n"
        f"PARAMETERS — total {n_params} numbers, flatten per-arm weight matrices:\n{param_block}\n\n"
        "Reward f(params) is the average counterfactual normalised throughput-per-core\n"
        "of the policy on a held-out set of historical cluster snapshots. Higher is\n"
        "better; values near 1.0 approach the oracle best. You receive a history of\n"
        "previous (params, f(params)) pairs and must propose a NEW vector.\n\n"
        "OUTPUT FORMAT — MANDATORY:\n"
        "Reply with EXACTLY ONE LINE listing all parameters in this exact format:\n\n"
        f"  params[0]: <value>; params[1]: <value>; ... ; params[{n_params - 1}]: <value>\n\n"
        "Then optionally any short reasoning on subsequent lines. Numbers must be\n"
        "plain decimals (e.g. -1.234, 0.05, 2.7). Keep |params[i]| <= 5. Do NOT wrap\n"
        "the line in code fences. Do NOT skip indices. Strict adherence to the format\n"
        "is required for the parser to read your proposal."
    )


def build_history_text(history, n_params):
    lines = [
        "Past proposals and observed rewards (higher f(params) is better):",
        "",
    ]
    for params, score in history:
        parts = [f"params[{i}]: {x:.5g}" for i, x in enumerate(params)]
        lines.append("; ".join(parts) + f"; f(params): {score:.4f}")
    lines.append("")
    lines.append(
        f"Propose ONE NEW vector of {n_params} parameters expected to score higher "
        f"than the best above. Output the single line in the params[i]: <value>; ... "
        f"format as specified, then optional brief reasoning."
    )
    return "\n".join(lines)


# ---------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Train OFFLINE_BANDIT_V3 (ProPS+).")
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                        help=f"Comma-separated arms. Default: {','.join(DEFAULT_ARMS)}")
    parser.add_argument("--dists", default=",".join(DISTS),
                        help="Comma-separated training scenarios. Each entry corresponds "
                             "to a results/<entry>/ directory. Default trains on the "
                             "synthetic autoscaler-* benchmark; pass q2-const,q2-sine,q2-step "
                             "(or similar) to train on a Nexmark scenario.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--backend", default="ollama",
                        choices=["ollama", "gemini", "anthropic", "mock"])
    parser.add_argument("--model", default="qwen2.5:7b",
                        help="Model name. ollama: e.g. qwen2.5:7b. gemini: gemini-2.5-flash. "
                             "anthropic: claude-haiku-4-5-20251001 or claude-sonnet-4-6.")
    parser.add_argument("--ollama-host",
                        default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--gemini-key", default=os.environ.get("GEMINI_API_KEY", ""))
    parser.add_argument("--gemini-throttle", type=float, default=0.0,
                        help="Seconds to sleep before each Gemini call (free-tier rate limits).")
    parser.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    parser.add_argument("--warmup", type=int, default=10,
                        help="Number of random Gaussian seed proposals before LLM loop.")
    parser.add_argument("--iters", type=int, default=30,
                        help="Number of LLM-driven proposals.")
    parser.add_argument("--history-keep", type=int, default=10,
                        help="Top-K past proposals shown to the LLM each iter.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--num-ctx", type=int, default=8192,
                        help="Ollama context window. 8192 fits system+history+output comfortably.")
    parser.add_argument("--soft-arm-temperature", type=float, default=0.0,
                        help="If >0, use softmax-weighted expected reward (smooth) "
                             "as the LLM-facing reward instead of argmax-arm (piecewise). "
                             "Runtime policy is always argmax — this only smooths training. "
                             "Acts as T_start when --soft-arm-temperature-end is set.")
    parser.add_argument("--soft-arm-temperature-end", type=float, default=None,
                        help="If set, geometric anneal of soft-arm-temperature from "
                             "--soft-arm-temperature (start) to this value (end) over the main loop. "
                             "Curriculum: explore broad mixtures early, sharpen toward argmax late. "
                             "Suggested: start 5.0, end 0.1.")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    dists = [d.strip() for d in args.dists.split(",") if d.strip()]
    n_params = len(arms) * FEATURE_DIM
    out_path = Path(args.out)
    log_path = Path(args.log) if args.log else out_path.with_suffix(".trainlog.txt")
    rng = np.random.default_rng(args.seed)

    print(f"Arms:    {arms}")
    print(f"Dists:   {dists}")
    print(f"Backend: {args.backend} (model={args.model})")
    print(f"Warmup:  {args.warmup} | Main: {args.iters} | History-keep: {args.history_keep}")
    print(f"Output:  {out_path}")
    print(f"Log:     {log_path}")

    print("\nLoading dataset...")
    rewards, snapshots, arm_X = load_dataset(arms, dists)
    if not rewards:
        sys.exit("No usable runs.")
    max_reward = max(rewards.values())
    print(f"Reward grid (Tput/core, max={max_reward:.0f}):")
    for d in dists:
        row_parts = []
        for a in arms:
            r = rewards.get((d, a))
            row_parts.append(f"{a}={r:.0f}" if r else f"{a}=-")
        print(f"  {d}: " + " ".join(row_parts))
    print(f"Total snapshots used for policy eval: {len(snapshots)}")

    if args.backend == "ollama":
        backend = OllamaBackend(
            model=args.model,
            host=args.ollama_host,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            num_ctx=args.num_ctx,
        )
    elif args.backend == "gemini":
        backend = GeminiBackend(
            model=args.model,
            api_key=args.gemini_key,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            throttle_sec=args.gemini_throttle,
        )
    elif args.backend == "anthropic":
        backend = AnthropicBackend(
            model=args.model,
            api_key=args.anthropic_key,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    else:
        backend = MockBackend(seed=args.seed)

    system_prompt = build_system_prompt(arms)

    log_f = log_path.open("w")
    log_f.write(f"# v3 trainer (ProPS+) started {datetime.now().isoformat()}\n")
    log_f.write(f"# arms={arms} backend={backend.name} model={args.model}\n")
    log_f.write(f"# warmup={args.warmup} iters={args.iters} "
                f"history_keep={args.history_keep} seed={args.seed} "
                f"temperature={args.temperature}\n")
    log_f.write(f"# dists={dists}\n")
    log_f.write(f"# reward grid (normalisation max={max_reward}):\n")
    for d in dists:
        for a in arms:
            r = rewards.get((d, a))
            log_f.write(f"#   {d}/{a}: {r}\n")
    log_f.write("\n")
    log_f.flush()

    history = []  # list of (params_array, normalised_score)

    T_start = args.soft_arm_temperature
    T_end = args.soft_arm_temperature_end if args.soft_arm_temperature_end is not None else T_start
    use_soft = T_start > 0.0
    anneal = use_soft and T_start != T_end and T_end > 0.0

    def temperature_at(t):
        if not anneal or args.iters <= 1:
            return T_start
        return T_start * (T_end / T_start) ** (t / max(1, args.iters - 1))

    def score_at(params, T):
        return score_policy(params, arms, snapshots, rewards, max_reward, T)

    def score_pair(params, T):
        soft_s = score_at(params, T) if T > 0.0 else None
        hard_s = score_at(params, 0.0)
        train_s = soft_s if soft_s is not None else hard_s
        return train_s, hard_s

    if anneal:
        print(f"\nAnnealing soft-arm temperature: T_start={T_start} → T_end={T_end} "
              f"over {args.iters} iters (geometric).")
        print(f"  iter 1 T={temperature_at(0):.3f}  iter {args.iters} T={temperature_at(args.iters - 1):.3f}")
    elif use_soft:
        print(f"\nFixed soft-arm temperature T={T_start}.")

    # Under annealing, the LLM-facing score depends on current T_t. So instead
    # of caching one score per history entry, we keep just the params and
    # re-score under T_t each iter. The hard score is also tracked so the
    # final best-param pick reflects deployed (argmax) performance.
    params_history = []  # list of params arrays only
    hard_scores = []     # parallel list of hard-argmax scores

    print(f"\n=== Warmup: {args.warmup} random Gaussian proposals ===")
    T0 = temperature_at(0)
    for i in range(args.warmup):
        params = rng.normal(0.0, 1.0, size=n_params)
        train_s, hard_s = score_pair(params, T0)
        params_history.append(params)
        hard_scores.append(hard_s)
        dist_arms = arm_distribution(params, arms, snapshots)
        log_f.write(f"WARMUP {i + 1}/{args.warmup}: f(T={T0:.3f})={train_s:.4f}"
                    f"{f' hard={hard_s:.4f}' if use_soft else ''}\n")
        log_f.write(f"  params: {np.round(params, 3).tolist()}\n")
        log_f.write(f"  picks:  {dist_arms}\n\n")
        log_f.flush()
        msg = f"  warmup {i + 1}/{args.warmup}: f={train_s:.4f}"
        if use_soft:
            msg += f" hard={hard_s:.4f}"
        print(msg)

    print(f"\n=== Main loop: {args.iters} LLM proposals ===")
    for it in range(args.iters):
        T_t = temperature_at(it)
        # Re-score every past params under current T_t for comparable prompt scores.
        rescored = [(p, score_at(p, T_t) if use_soft else score_at(p, 0.0))
                    for p in params_history]
        rescored.sort(key=lambda x: x[1])
        top = rescored[-args.history_keep:]
        hist_text = build_history_text(top, n_params)
        t0 = time.time()
        try:
            new_params, reasoning = backend.propose(system_prompt, hist_text, n_params)
        except Exception as e:
            dt = time.time() - t0
            log_f.write(f"ITER {it + 1}/{args.iters}: T={T_t:.3f} backend error after {dt:.1f}s: {e}\n\n")
            log_f.flush()
            print(f"  iter {it + 1}/{args.iters}: ERROR {e!r}")
            continue
        dt = time.time() - t0
        train_s, hard_s = score_pair(new_params, T_t)
        params_history.append(new_params)
        hard_scores.append(hard_s)
        dist_arms = arm_distribution(new_params, arms, snapshots)
        best_hard = max(hard_scores)
        soft_suffix = f" hard={hard_s:.4f}" if use_soft else ""
        anneal_suffix = f" T={T_t:.3f}" if anneal else ""
        log_f.write(f"ITER {it + 1}/{args.iters}:{anneal_suffix} f={train_s:.4f}{soft_suffix} "
                    f"best_hard={best_hard:.4f} dt={dt:.1f}s\n")
        log_f.write(f"  params: {np.round(new_params, 3).tolist()}\n")
        log_f.write(f"  picks:  {dist_arms}\n")
        log_f.write(f"  reasoning: {reasoning[:500]}\n\n")
        log_f.flush()
        print(f"  iter {it + 1}/{args.iters}:{anneal_suffix} f={train_s:.4f}{soft_suffix} "
              f"best_hard={best_hard:.4f} dt={dt:.1f}s")

    # Pick the params with the best HARD-argmax score, since that's the policy
    # actually deployed at runtime in Java (which uses argmax over theta @ x).
    best_idx = int(np.argmax(hard_scores))
    best_params = params_history[best_idx]
    best_hard = hard_scores[best_idx]
    best_score = best_hard  # legacy field name = hard for backwards compatibility
    print(f"\nBest hard-argmax score = {best_hard:.4f} "
          f"({best_hard * max_reward:.0f} ev/s/core deployed)")
    if use_soft:
        # Also report the soft score of that winner at the final temperature.
        T_final = temperature_at(args.iters - 1) if anneal else T_start
        soft_at_final = score_at(best_params, T_final)
        print(f"   ↳ soft score at T_final={T_final:.3f}: {soft_at_final:.4f}")

    print("Computing per-arm A_inv from training contexts (ridge basis)...")
    a_inv_map = compute_a_inv(arm_X)

    thetas = best_params.reshape(len(arms), FEATURE_DIM)
    payload = {
        "feature_dim": FEATURE_DIM,
        "alpha": ALPHA_RUNTIME,
        "decision_interval_ms": DECISION_INTERVAL_MS,
        "feature_names": FEATURE_NAMES,
        "ridge_lambda": RIDGE_LAMBDA,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "reward_mode": "throughput_per_core",
        "trainer": "props_plus_v3",
        "trainer_args": {
            "backend": backend.name,
            "model": args.model,
            "warmup": args.warmup,
            "iters": args.iters,
            "history_keep": args.history_keep,
            "seed": args.seed,
            "temperature": args.temperature,
            "soft_arm_temperature": args.soft_arm_temperature,
        },
        "best_normalised_score": float(best_score),
        "best_hard_argmax_score": float(best_hard),
        "max_reward_used_for_normalisation": int(max_reward),
        "arms_trained": arms,
        "arms": {
            a: {"theta": thetas[i].tolist(), "A_inv": a_inv_map[a]}
            for i, a in enumerate(arms)
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    log_f.write(f"\nFINAL: best_score={best_score:.4f} wrote {out_path}\n")
    log_f.close()
    print(f"Wrote {out_path}")
    print(f"Log:   {log_path}")
    print("\nRebuild the scheduler image to pick up the new weights:")
    print("  cd scheduler && mvn clean package "
          "&& docker build -t adaptive-scheduler:offline_bandit_v3 .")


if __name__ == "__main__":
    main()
