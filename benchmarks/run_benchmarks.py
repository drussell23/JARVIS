#!/usr/bin/env python3
"""
Trinity AI — Benchmark Chart Generator
Run with: .venv/bin/python3.13 benchmarks/run_benchmarks.py
Charts are saved as PNGs in the benchmarks/ folder.
"""
import os, json, math
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl_trinity_cache")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
HISTORY    = SCRIPT_DIR / "history.json"

if not HISTORY.exists():
    print(f"❌  history.json not found at {HISTORY}")
    raise SystemExit(1)

# ── Style ─────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    "figure.facecolor": "#0d1117",
    "axes.facecolor":   "#161b22",
    "axes.edgecolor":   "#30363d",
    "axes.labelcolor":  "#c9d1d9",
    "axes.titlecolor":  "#e6edf3",
    "axes.titlesize":   13,
    "axes.titleweight": "bold",
    "axes.grid":        True,
    "grid.color":       "#21262d",
    "grid.linewidth":   0.8,
    "xtick.color":      "#8b949e",
    "ytick.color":      "#8b949e",
    "text.color":       "#c9d1d9",
    "legend.facecolor": "#161b22",
    "legend.edgecolor": "#30363d",
    "legend.fontsize":  9,
    "font.family":      "monospace",
    "figure.dpi":       130,
})

CYAN   = "#58a6ff"
GREEN  = "#3fb950"
RED    = "#f85149"
YELLOW = "#d29922"
PURPLE = "#bc8cff"
DIM    = "#8b949e"

# ── Load + flatten ────────────────────────────────────────────────────────────
with HISTORY.open() as f:
    raw = json.load(f)

print(f"📊  Loaded {len(raw)} run(s) from history.json\n")

rows = []
test_rows = []

for entry in raw:
    run_ts    = entry.get("run_ts", "?")
    run_label = run_ts.replace("T", "\n")
    sys_info  = entry.get("system", {})

    for key in ("inference_0", "inference_1"):
        t = entry.get(key)
        if not t:
            continue
        rows.append({
            "run_ts":            run_ts,
            "run_label":         run_label,
            "task":              t["label"],
            "latency_s":         t["latency_ms"] / 1000,
            "tok_s":             t["tok_s"],
            "completion_tokens": t["completion_tokens"],
            "model":             t.get("model", "jarvis-prime"),
        })

    t = entry.get("tests")
    if t:
        test_rows.append({
            "run_ts":           run_ts,
            "run_label":        run_label,
            "tests_passed":     t["passed"],
            "tests_failed":     t["failed"],
            "pass_rate":        t["pass_rate"],
            "duration_s":       t["duration_s"],
            "tests_per_second": t["tests_per_second"],
        })

df       = pd.DataFrame(rows)
df_tests = pd.DataFrame(test_rows) if test_rows else pd.DataFrame()

tasks  = df["task"].unique()
runs   = df["run_ts"].unique()
n_runs = len(runs)
n_tasks = len(tasks)
x      = np.arange(n_runs)
width  = 0.35
colors = [CYAN, GREEN]
run_labels = df.drop_duplicates("run_ts").sort_values("run_ts")["run_label"].tolist()

saved = []

def _save(fig, name):
    path = SCRIPT_DIR / name
    fig.savefig(path, bbox_inches="tight", dpi=130)
    plt.close(fig)
    saved.append(name)
    print(f"  ✅  {name}")

def _grouped_bars(ax, metric, ylabel, fmt_fn, ylim_mult=1.25):
    for i, (task, color) in enumerate(zip(tasks, colors)):
        subset = df[df["task"] == task].sort_values("run_ts")
        vals   = subset[metric].values
        offset = (i - (n_tasks - 1) / 2) * width
        bars   = ax.bar(x + offset, vals, width, label=task,
                        color=color, alpha=0.85, zorder=3)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() * 1.02, fmt_fn(v),
                    ha="center", va="bottom", fontsize=8, color="#e6edf3")
    ax.set_xticks(x)
    ax.set_xticklabels(run_labels, fontsize=7)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.legend(fontsize=7)
    ax.set_ylim(0, df[metric].max() * ylim_mult)

# ── Chart 1: Throughput ───────────────────────────────────────────────────────
print("Generating charts...")
fig, ax = plt.subplots(figsize=(max(7, n_runs * 2.8), 5))
_grouped_bars(ax, "tok_s", "Throughput (tok/s)", lambda v: f"{v:.1f}")
ax.axhline(20, color=YELLOW, linewidth=0.8, linestyle="--", zorder=2)
ax.text(n_runs - 0.45, 20.4, "baseline 20 tok/s", fontsize=7, color=YELLOW)
ax.set_title("Inference Throughput per Run — NVIDIA L4 · Q4_K_M")
fig.tight_layout()
_save(fig, "chart_throughput.png")

# ── Chart 2: Latency ─────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(max(7, n_runs * 2.8), 5))
_grouped_bars(ax, "latency_s", "Latency (s)", lambda v: f"{v:.1f}s")
ax.set_title("End-to-End Inference Latency per Run")
fig.tight_layout()
_save(fig, "chart_latency.png")

# ── Chart 3: Tokens generated ─────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(max(7, n_runs * 2.8), 5))
_grouped_bars(ax, "completion_tokens", "Completion Tokens",
              lambda v: str(int(v)), ylim_mult=1.35)
ax.set_title("Tokens Generated per Run  (250 → 631 after removing max_tokens cap)")
fig.tight_layout()
_save(fig, "chart_tokens.png")

# ── Chart 4: Consistency scatter ─────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(7, 5))
for task, color in zip(tasks, colors):
    sub = df[df["task"] == task]
    ax.scatter(sub["completion_tokens"], sub["latency_s"],
               label=task, color=color, s=90, zorder=4,
               edgecolors="#0d1117", linewidths=0.6)
    for _, row in sub.iterrows():
        ax.annotate(f"{row['tok_s']:.1f} tok/s",
                    (row["completion_tokens"], row["latency_s"]),
                    textcoords="offset points", xytext=(6, 4),
                    fontsize=7, color=DIM)
if len(df) >= 2:
    xs = df["completion_tokens"].values
    ys = df["latency_s"].values
    c  = np.polyfit(xs, ys, 1)
    xl = np.linspace(xs.min() * 0.8, xs.max() * 1.1, 100)
    ax.plot(xl, np.polyval(c, xl), color=YELLOW, lw=1,
            linestyle="--", alpha=0.7, label="linear fit")
    tps = 1 / c[0] if c[0] != 0 else 0
    ax.text(0.98, 0.05, f"Implied throughput: {tps:.1f} tok/s",
            transform=ax.transAxes, ha="right", fontsize=8, color=YELLOW)
ax.set_xlabel("Completion Tokens"); ax.set_ylabel("Latency (s)")
ax.set_title("Latency vs Tokens — Linear = Consistent Throughput")
ax.legend()
fig.tight_layout()
_save(fig, "chart_consistency.png")

# ── Chart 5: Governance tests ─────────────────────────────────────────────────
if not df_tests.empty:
    x_t   = np.arange(len(df_tests))
    rl_t  = df_tests["run_label"].tolist()
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))

    ax = axes[0]
    bars = ax.bar(x_t, df_tests["pass_rate"], color=GREEN, alpha=0.85, zorder=3)
    for bar, v in zip(bars, df_tests["pass_rate"]):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.05, f"{v:.1f}%",
                ha="center", va="bottom", fontsize=8, color="#e6edf3")
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=7, rotation=10)
    ax.set_ylim(95, 101); ax.set_ylabel("%"); ax.set_title("Pass Rate")
    ax.axhline(99, color=YELLOW, lw=0.8, linestyle="--")

    ax = axes[1]
    ax.bar(x_t, df_tests["tests_passed"], color=CYAN, alpha=0.85, zorder=3, label="passed")
    ax.bar(x_t, df_tests["tests_failed"], bottom=df_tests["tests_passed"],
           color=RED, alpha=0.6, zorder=3, label="pre-existing failures")
    for xi, (p, f) in enumerate(zip(df_tests["tests_passed"], df_tests["tests_failed"])):
        ax.text(xi, p + f + 4, f"{p:,}", ha="center", fontsize=8, color=GREEN)
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=7, rotation=10)
    ax.set_ylabel("Count"); ax.set_title("Tests Passed")
    ax.legend(fontsize=7)

    ax = axes[2]
    bars = ax.bar(x_t, df_tests["tests_per_second"], color=PURPLE, alpha=0.85, zorder=3)
    for bar, v in zip(bars, df_tests["tests_per_second"]):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.2, f"{v:.0f}/s",
                ha="center", va="bottom", fontsize=8, color="#e6edf3")
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=7, rotation=10)
    ax.set_ylabel("tests/s"); ax.set_title("Suite Speed")

    fig.suptitle("Ouroboros Governance Test Suite — Reliability Across Runs",
                 fontsize=13, fontweight="bold", color="#e6edf3", y=1.02)
    fig.tight_layout()
    _save(fig, "chart_governance_tests.png")
else:
    print("  ⚠️   No test data yet — skipping governance chart")

# ── Chart 6: Dashboard (all in one) ──────────────────────────────────────────
has_tests = not df_tests.empty
n_rows    = 2 if has_tests else 1
fig       = plt.figure(figsize=(16, 5 * n_rows))
fig.patch.set_facecolor("#0d1117")

ax1 = fig.add_subplot(n_rows, 3, 1)
ax2 = fig.add_subplot(n_rows, 3, 2)
ax3 = fig.add_subplot(n_rows, 3, 3)

_grouped_bars(ax1, "tok_s",             "tok/s",  lambda v: f"{v:.1f}")
_grouped_bars(ax2, "latency_s",         "s",      lambda v: f"{v:.1f}s")
_grouped_bars(ax3, "completion_tokens", "tokens", lambda v: str(int(v)))
ax1.set_title("Throughput",       fontsize=10)
ax2.set_title("Latency",          fontsize=10)
ax3.set_title("Tokens Generated", fontsize=10)

if has_tests:
    x_t  = np.arange(len(df_tests))
    rl_t = df_tests["run_label"].tolist()
    ax4  = fig.add_subplot(n_rows, 3, 4)
    ax5  = fig.add_subplot(n_rows, 3, 5)
    ax6  = fig.add_subplot(n_rows, 3, 6)

    ax4.bar(x_t, df_tests["pass_rate"], color=GREEN, alpha=0.85, zorder=3)
    ax4.set_ylim(95, 101)
    ax4.set_xticks(x_t); ax4.set_xticklabels(rl_t, fontsize=6, rotation=10)
    ax4.set_ylabel("%", fontsize=8); ax4.set_title("Pass Rate", fontsize=10)
    ax4.axhline(99, color=YELLOW, lw=0.8, linestyle="--")

    ax5.bar(x_t, df_tests["tests_passed"], color=CYAN, alpha=0.85, zorder=3)
    ax5.set_xticks(x_t); ax5.set_xticklabels(rl_t, fontsize=6, rotation=10)
    ax5.set_ylabel("count", fontsize=8); ax5.set_title("Tests Passed", fontsize=10)

    ax6.bar(x_t, df_tests["tests_per_second"], color=PURPLE, alpha=0.85, zorder=3)
    ax6.set_xticks(x_t); ax6.set_xticklabels(rl_t, fontsize=6, rotation=10)
    ax6.set_ylabel("tests/s", fontsize=8); ax6.set_title("Suite Speed", fontsize=10)

fig.suptitle("Trinity AI — Full Benchmark Dashboard",
             fontsize=15, fontweight="bold", color="#e6edf3", y=1.01)
fig.tight_layout()
_save(fig, "chart_dashboard.png")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("  TRINITY AI — BENCHMARK SUMMARY")
print("=" * 60)
print(f"  Runs recorded: {len(raw)}")
print()
for task in tasks:
    sub = df[df["task"] == task]
    print(f"  [{task}]")
    print(f"    Throughput : {sub['tok_s'].mean():.1f} tok/s avg"
          f"  (range {sub['tok_s'].min():.1f}–{sub['tok_s'].max():.1f})")
    print(f"    Latency    : {sub['latency_s'].mean():.1f}s avg"
          f"  (range {sub['latency_s'].min():.1f}–{sub['latency_s'].max():.1f}s)")
    print(f"    Tokens     : {sub['completion_tokens'].mean():.0f} avg"
          f"  (range {sub['completion_tokens'].min():.0f}"
          f"–{sub['completion_tokens'].max():.0f})")
    print()
if not df_tests.empty:
    print("  [Governance Tests]")
    print(f"    Pass rate  : {df_tests['pass_rate'].mean():.1f}% avg")
    print(f"    Tests run  : {df_tests['tests_passed'].max():,} (latest)")
    print(f"    Speed      : {df_tests['tests_per_second'].mean():.0f} tests/s avg")
    print()
print("  [Hardware]")
print("    GPU   : NVIDIA L4 (g2-standard-4 · 23 GB VRAM)")
print("    Model : Qwen2.5-Coder-14B-Instruct · Q4_K_M")
print("    Ctx   : 8,192 tokens")
print("=" * 60)
print()
print(f"Charts saved to benchmarks/:")
for name in saved:
    print(f"  📈  {name}")
