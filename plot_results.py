#!/usr/bin/env python3
"""
Plot throughput, drop% and stale% across strategies and arrival distributions.

Supports multiple benchmarks via --benchmark:
  original  →  results/autoscaler-{const,sine,step}/{STRATEGY}/  (ConfigurableGraphJob)
  q5        →  results/q5-{const,sine,step}/{STRATEGY}/          (Nexmark Q5 Hot Items)
  q8        →  results/q8-{const,sine,step}/{STRATEGY}/          (Nexmark Q8 New Users)
  all       →  produce plots for each available benchmark

Output PNGs/CSVs are suffixed with the benchmark name when not "original".
"""
import argparse
import re
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).parent
RESULTS = ROOT / "results"
OUT_DIR = RESULTS / "plots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BENCHMARKS = {
    "original": [("autoscaler-const", "CONSTANT"),
                 ("autoscaler-sine",  "SINE"),
                 ("autoscaler-step",  "STEP")],
    "q5":       [("q5-const", "CONSTANT"),
                 ("q5-sine",  "SINE"),
                 ("q5-step",  "STEP")],
    "q8":       [("q8-const", "CONSTANT"),
                 ("q8-sine",  "SINE"),
                 ("q8-step",  "STEP")],
}

# Active set, overwritten in main() based on --benchmark.
DISTS = BENCHMARKS["original"]
BENCH_LABEL = "original"

STRATEGIES = ["FCFS", "BALANCED", "LEAST_LOADED", "BANDIT", "SARSA", "ADAPTIVE",
              "OFFLINE_BANDIT", "OFFLINE_BANDIT_V2", "OFFLINE_BANDIT_V3",
              "OFFLINE_BANDIT_V4", "OFFLINE_BANDIT_V5", "SARSA_META", "DEFAULT"]

# Display label override: data is loaded from results/.../{STRATEGY}/ on disk,
# but in plots/CSVs we show DISPLAY_NAMES[s] (or s itself if not mapped).
# Change the value here if you want a different label on the chart/CSV.
DISPLAY_NAMES = {
    "OFFLINE_BANDIT_V3": "LLM_BANDIT",
    "SARSA_META": "SARSA_META",
}


def display(s):
    return DISPLAY_NAMES.get(s, s)

RX_THROUGHPUT = re.compile(r"^\s*Throughput:\s+(\d+)\s+ev/s", re.MULTILINE)
RX_DROP_PCT   = re.compile(r"^\s*Drop %:\s+([\d.]+)\s*%", re.MULTILINE)
RX_STALE_PCT  = re.compile(r"^\s*Stale %:\s+([\d.]+)\s*%", re.MULTILINE)
# Nexmark canonical metrics (appended by scripts/compute_nexmark_metrics.py).
RX_CORES_AVG  = re.compile(r"^\s*Cores avg:\s+([\d.]+)\s+cores", re.MULTILINE)
RX_COST_MEV   = re.compile(r"^\s*Cost / Mevent:\s+([\d.]+)", re.MULTILINE)
RX_TPUT_CORE  = re.compile(r"^\s*Throughput / core:\s+([\d,]+)", re.MULTILINE)


def parse(path: Path):
    if not path.exists():
        return None
    txt = path.read_text(encoding="utf-8", errors="replace")
    m_t  = RX_THROUGHPUT.search(txt)
    m_d  = RX_DROP_PCT.search(txt)
    m_s  = RX_STALE_PCT.search(txt)
    m_c  = RX_CORES_AVG.search(txt)
    m_co = RX_COST_MEV.search(txt)
    m_tc = RX_TPUT_CORE.search(txt)
    return {
        "throughput":        int(m_t.group(1)) if m_t else 0,
        "drop_pct":          float(m_d.group(1)) if m_d else 0.0,
        "stale_pct":         float(m_s.group(1)) if m_s else 0.0,
        "cores_avg":         float(m_c.group(1)) if m_c else 0.0,
        "cost_per_mevent":   float(m_co.group(1)) if m_co else 0.0,
        "tput_per_core":     int(m_tc.group(1).replace(",", "")) if m_tc else 0,
    }


def collect():
    data = {}
    for dist_dir, dist_label in DISTS:
        data[dist_label] = {}
        for strat in STRATEGIES:
            f = RESULTS / dist_dir / strat / "METRICS-SUMMARY.txt"
            d = parse(f)
            if d is None:
                print(f"  [missing] {f.relative_to(ROOT)}")
                d = {"throughput": 0, "drop_pct": 0.0, "stale_pct": 0.0,
                     "cores_avg": 0.0, "cost_per_mevent": 0.0, "tput_per_core": 0}
            data[dist_label][strat] = d
    return data


def plot_grouped(data, metric_key, title, ylabel, fname):
    n_strats = len(STRATEGIES)
    n_dists  = len(DISTS)
    x = np.arange(n_strats)
    bar_w = 0.8 / n_dists
    colors = ["#4C72B0", "#DD8452", "#55A467"]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    for i, (_, dist_label) in enumerate(DISTS):
        vals = [data[dist_label][s][metric_key] for s in STRATEGIES]
        offset = (i - (n_dists - 1) / 2) * bar_w
        bars = ax.bar(x + offset, vals, bar_w, label=dist_label, color=colors[i])
        for b, v in zip(bars, vals):
            if v > 0:
                ax.text(b.get_x() + b.get_width() / 2, v,
                        f"{v:,.0f}" if metric_key == "throughput" else f"{v:.1f}",
                        ha="center", va="bottom", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels([display(s) for s in STRATEGIES], rotation=15)
    ax.set_xlabel("Strategy")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(title="Arrival distribution")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = OUT_DIR / _suffixed(fname)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def _suffixed(fname: str) -> str:
    """Append `_<benchmark>` to filenames for non-original benchmarks."""
    if BENCH_LABEL == "original":
        return fname
    stem, _, ext = fname.rpartition(".")
    return f"{stem}_{BENCH_LABEL}.{ext}"


def plot_combined(data):
    fig, axes = plt.subplots(1, 3, figsize=(20, 6))
    metrics = [
        ("throughput", "Throughput (ev/s)",     "Useful throughput",       "{:,.0f}"),
        ("drop_pct",   "Source drop %",          "Source-side drops",       "{:.1f}"),
        ("stale_pct",  "Stale-drop %",           "CPU-load stale drops",    "{:.2f}"),
    ]
    n_strats = len(STRATEGIES)
    n_dists  = len(DISTS)
    x = np.arange(n_strats)
    bar_w = 0.8 / n_dists
    colors = ["#4C72B0", "#DD8452", "#55A467"]

    for ax, (key, ylabel, title, fmt) in zip(axes, metrics):
        for i, (_, dist_label) in enumerate(DISTS):
            vals = [data[dist_label][s][key] for s in STRATEGIES]
            offset = (i - (n_dists - 1) / 2) * bar_w
            bars = ax.bar(x + offset, vals, bar_w, label=dist_label, color=colors[i])
            for b, v in zip(bars, vals):
                if v > 0:
                    ax.text(b.get_x() + b.get_width() / 2, v, fmt.format(v),
                            ha="center", va="bottom", fontsize=7)
        ax.set_xticks(x)
        ax.set_xticklabels([display(s) for s in STRATEGIES], rotation=20)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.3)
    axes[0].legend(title="Arrival dist", loc="upper right")
    fig.suptitle(f"Strategy comparison ({BENCH_LABEL}) across arrival distributions", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = OUT_DIR / _suffixed("all_metrics_grouped.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def plot_nexmark(data):
    """3-panel chart of Nexmark canonical metrics: cores, cost/Mev, throughput/core."""
    fig, axes = plt.subplots(1, 3, figsize=(20, 6))
    metrics = [
        ("cores_avg",       "Cores avg",          "Avg CPU cores used",            "{:.2f}"),
        ("cost_per_mevent", "Cost / Mevent (core·s)", "Cost / Mevent — lower is better", "{:.0f}"),
        ("tput_per_core",   "Throughput / core (ev/s)", "Throughput per core — higher is better", "{:,.0f}"),
    ]
    n_strats = len(STRATEGIES)
    n_dists  = len(DISTS)
    x = np.arange(n_strats)
    bar_w = 0.8 / n_dists
    colors = ["#4C72B0", "#DD8452", "#55A467"]

    for ax, (key, ylabel, title, fmt) in zip(axes, metrics):
        for i, (_, dist_label) in enumerate(DISTS):
            vals = [data[dist_label][s][key] for s in STRATEGIES]
            offset = (i - (n_dists - 1) / 2) * bar_w
            bars = ax.bar(x + offset, vals, bar_w, label=dist_label, color=colors[i])
            for b, v in zip(bars, vals):
                if v > 0:
                    ax.text(b.get_x() + b.get_width() / 2, v, fmt.format(v),
                            ha="center", va="bottom", fontsize=7)
        ax.set_xticks(x)
        ax.set_xticklabels([display(s) for s in STRATEGIES], rotation=20)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.3)
    axes[0].legend(title="Arrival dist", loc="upper right")
    fig.suptitle(f"Nexmark canonical metrics ({BENCH_LABEL}) across arrival distributions",
                 fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = OUT_DIR / _suffixed("nexmark_metrics.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  wrote {out.relative_to(ROOT)}")


def write_csv(data):
    out = OUT_DIR / _suffixed("summary.csv")
    with out.open("w") as fh:
        fh.write("distribution,strategy,throughput_ev_s,drop_pct,stale_pct,"
                 "cores_avg,cost_per_mevent,tput_per_core\n")
        for _, dist_label in DISTS:
            for s in STRATEGIES:
                d = data[dist_label][s]
                fh.write(f"{dist_label},{display(s)},{d['throughput']},{d['drop_pct']},{d['stale_pct']},"
                         f"{d['cores_avg']:.2f},{d['cost_per_mevent']:.2f},{d['tput_per_core']}\n")
    print(f"  wrote {out.relative_to(ROOT)}")


def _run_one_benchmark(label):
    global DISTS, BENCH_LABEL
    DISTS = BENCHMARKS[label]
    BENCH_LABEL = label
    print(f"\n=== Benchmark: {label} ===")
    print("Collecting metrics...")
    data = collect()
    print("Generating plots...")
    plot_grouped(data, "throughput",
                 f"Useful throughput by strategy and arrival distribution ({label})",
                 "Events/sec", "throughput_grouped.png")
    plot_grouped(data, "drop_pct",
                 f"Source drop % by strategy and arrival distribution ({label})",
                 "Drop %", "drop_grouped.png")
    plot_grouped(data, "stale_pct",
                 f"Stale-drop % by strategy and arrival distribution ({label})",
                 "Stale %", "stale_grouped.png")
    plot_combined(data)
    plot_nexmark(data)
    write_csv(data)


def main():
    parser = argparse.ArgumentParser(description="Plot scheduler benchmark metrics.")
    parser.add_argument("--benchmark", default="original",
                        choices=list(BENCHMARKS.keys()) + ["all"],
                        help="Which benchmark family to plot (default: original)")
    args = parser.parse_args()

    targets = list(BENCHMARKS.keys()) if args.benchmark == "all" else [args.benchmark]
    for t in targets:
        _run_one_benchmark(t)
    print("\nDone.")


if __name__ == "__main__":
    main()
