#!/bin/bash
# Offline contextual bandit (LinUCB) — V3 variant.
# Same arm set as V2 {BANDIT, LEAST_LOADED, BALANCED}, but theta weights are
# optimised by ProPS+ (LLM-driven prompted policy search) instead of supervised
# ridge regression. Reference: Zhou et al., "Prompted Policy Search", NeurIPS 2025.
#
# Pipeline:
#   1. Re-train V3 LinUCB weights via train_offline_bandit_v3.py (Ollama LLM).
#      Requires a local Ollama daemon at $OLLAMA_HOST with the chosen model pulled.
#   2. Run the experiment on each arrival distribution (CONSTANT, SINE, STEP).

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Defensive: prevent Nexmark env-var leak from prior shell sourcing q5/q8 scripts.
unset JOB_CLASS EXTRA_JOB_ARGS HEAVY_VERTEX_PATTERN

OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
V3_MODEL="${V3_MODEL:-qwen2.5:7b}"
V3_WARMUP="${V3_WARMUP:-10}"
V3_ITERS="${V3_ITERS:-30}"
V3_SEED="${V3_SEED:-42}"
V3_TEMPERATURE="${V3_TEMPERATURE:-0.7}"

if [ "${SKIP_V3_TRAIN:-0}" = "1" ]; then
  echo "=========================================="
  echo "  [PRE] SKIP_V3_TRAIN=1 — reusing existing weights at"
  echo "        scheduler/src/main/resources/offline_bandit_v3_weights.json"
  echo "=========================================="
  if [ ! -f "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v3_weights.json" ]; then
    echo "ERROR: SKIP_V3_TRAIN=1 but weights file does not exist"
    exit 1
  fi
else
  echo "=========================================="
  echo "  [PRE] Re-training offline bandit V3 weights (ProPS+)"
  echo "  Arms:    BANDIT, LEAST_LOADED, BALANCED"
  echo "  Backend: ollama @ ${OLLAMA_HOST} model=${V3_MODEL}"
  echo "  Loop:    warmup=${V3_WARMUP} iters=${V3_ITERS} seed=${V3_SEED}"
  echo "  Reward:  throughput_per_core (counterfactual policy eval)"
  echo "  Scope:   autoscaler-* only (Q5/Q8 stay zero-shot OOD tests)"
  echo "=========================================="
  if ! OLLAMA_HOST="http://${OLLAMA_HOST}" python3 "${SCRIPT_DIR}/scripts/train_offline_bandit_v3.py" \
          --arms BANDIT,LEAST_LOADED,BALANCED \
          --backend ollama \
          --model "${V3_MODEL}" \
          --warmup "${V3_WARMUP}" \
          --iters "${V3_ITERS}" \
          --seed "${V3_SEED}" \
          --temperature "${V3_TEMPERATURE}" \
          --out "${SCRIPT_DIR}/scheduler/src/main/resources/offline_bandit_v3_weights.json"; then
    echo "ERROR: offline bandit V3 training failed — aborting"
    exit 1
  fi
fi
echo ""

set +e   # individual runs handle their own errors
source "${SCRIPT_DIR}/run-experiment-common.sh"

run_strategy_experiment "OFFLINE_BANDIT_V3" "false" "autoscaler-const" 100000 300 8 10 2500 CONSTANT 2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V3" "false" "autoscaler-sine"   60000 300 8 10 2500 SINE     2 15000 "true"
run_strategy_experiment "OFFLINE_BANDIT_V3" "false" "autoscaler-step"   60000 300 8 10 2500 STEP     2 15000 "true"
