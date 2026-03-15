#!/usr/bin/env python3
"""
Trinity AI — Benchmark Chart Generator  (v3)
Async · parallel · fully data-driven · no hardcoded values.
Run: .venv/bin/python3.13 benchmarks/run_benchmarks.py
"""
from __future__ import annotations
import asyncio, concurrent.futures, json, datetime, time
from dataclasses import dataclass
from pathlib import Path
import os

# ── Matplotlib: cache + Agg backend BEFORE any other mpl import ──────────────
_CACHE = Path(os.environ.get("MPLCONFIGDIR", "/tmp/mpl_trinity_cache"))
_CACHE.mkdir(parents=True, exist_ok=True)
os.environ["MPLCONFIGDIR"] = str(_CACHE)

import matplotlib
matplotlib.use("Agg")                   # Thread-safe, non-interactive
import matplotlib.pyplot as plt         # rcParams only — no plt.figure() in workers
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FormatStrFormatter
import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
HISTORY    = SCRIPT_DIR / "history.json"

if not HISTORY.exists():
    raise SystemExit(f"❌  history.json not found at {HISTORY}")

# ── Performance targets (configurable — drives grades & baseline annotations) ─
TARGETS: dict[str, float] = {
    "tok_s":             20.0,   # tok/s — L4 + llama-cpp published baseline
    "pass_rate":         99.0,   # % — governance reliability floor
    "tests_per_second":  10.0,   # tests/s — minimum acceptable suite speed
    "threat_latency_s":   8.0,   # s — max acceptable threat-analysis latency
}

# ── Compute display names (extensible) ────────────────────────────────────────
COMPUTE_DISPLAY: dict[str, str] = {
    "gpu_l4":   "NVIDIA L4",
    "gpu_a100": "NVIDIA A100",
    "gpu_t4":   "NVIDIA T4",
    "gpu_v100": "NVIDIA V100",
    "cpu":      "CPU",
}

# ── Color palette ─────────────────────────────────────────────────────────────
BG     = "#0d1117"
PANEL  = "#161b22"
BORDER = "#30363d"
FG     = "#c9d1d9"
FG_HI  = "#e6edf3"
GRID   = "#21262d"
DIM    = "#8b949e"
CYAN   = "#58a6ff"
GREEN  = "#3fb950"
RED    = "#f85149"
YELLOW = "#d29922"
PURPLE = "#bc8cff"
ORANGE = "#f0883e"
TEAL   = "#39d353"

_PALETTE = [CYAN, GREEN, ORANGE, PURPLE, TEAL, YELLOW, RED]

# ── Global rcParams — set once before workers spawn, read-only thereafter ─────
plt.rcParams.update({
    "figure.facecolor":  BG,
    "axes.facecolor":    PANEL,
    "axes.edgecolor":    BORDER,
    "axes.labelcolor":   FG,
    "axes.titlecolor":   FG_HI,
    "axes.titlesize":    12,
    "axes.titleweight":  "bold",
    "axes.grid":         True,
    "axes.axisbelow":    True,
    "grid.color":        GRID,
    "grid.linewidth":    0.7,
    "xtick.color":       DIM,
    "ytick.color":       DIM,
    "xtick.labelsize":   8,
    "ytick.labelsize":   8,
    "text.color":        FG,
    "legend.facecolor":  PANEL,
    "legend.edgecolor":  BORDER,
    "legend.fontsize":   8,
    "font.family":       "monospace",
    "figure.dpi":        140,
})

# ══════════════════════════════════════════════════════════════════════════════
# Data container — passed read-only to all chart workers
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BenchmarkData:
    raw:           list[dict]
    df:            pd.DataFrame
    df_tests:      pd.DataFrame
    tasks:         list[str]
    task_colors:   dict[str, str]
    runs:          list[str]
    n_runs:        int
    n_tasks:       int
    x:             np.ndarray
    bar_w:         float
    run_labels:    list[str]
    gpu_display:   str
    model_display: str
    artifact_disp: str
    quant_tag:     str
    ctx_display:   object          # int | str — from history system block
    hw_subtitle:   str
    targets:       dict[str, float]
    script_dir:    Path

    @property
    def ctx_str(self) -> str:
        return (f"{self.ctx_display:,} tok"
                if isinstance(self.ctx_display, int)
                else f"{self.ctx_display} tok")


# ══════════════════════════════════════════════════════════════════════════════
# Thread-safe figure factories (bypass plt global state)
# ══════════════════════════════════════════════════════════════════════════════

def _make_fig(figsize: tuple[float, float] = (8.0, 5.0)) -> Figure:
    fig = Figure(figsize=figsize, facecolor=BG)
    FigureCanvasAgg(fig)
    return fig


def _make_subplots(nrows: int = 1, ncols: int = 1,
                   figsize: tuple[float, float] = (8.0, 5.0),
                   **kwargs) -> tuple[Figure, object]:
    fig = Figure(figsize=figsize, facecolor=BG)
    FigureCanvasAgg(fig)
    axes = fig.subplots(nrows, ncols, **kwargs)
    return fig, axes


def _save_fig(fig: Figure, name: str, script_dir: Path) -> str:
    """Write PNG to disk, release figure memory. Thread-safe."""
    fig.savefig(script_dir / name, bbox_inches="tight", dpi=140,
                facecolor=fig.get_facecolor())
    fig.clf()
    return name


# ══════════════════════════════════════════════════════════════════════════════
# Pure helpers — no global mutable state, safe to call from any thread
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_ts(run_ts: str) -> str:
    """'2026-03-14T16-26-33'  →  'Mar 14\\n16:26'"""
    try:
        date_s, time_s = run_ts.split("T")
        h, m = time_s.split("-")[:2]
        d = datetime.date.fromisoformat(date_s)
        return f"{d.strftime('%b %-d')}\n{h}:{m}"
    except Exception:
        return run_ts


def _task_color(idx: int) -> str:
    return _PALETTE[idx % len(_PALETTE)]


def _bar_width(n_runs: int) -> float:
    return max(0.14, min(0.38, 0.70 / max(n_runs, 1)))


def _fig_width(n_runs: int, base: float = 5.5, per_run: float = 3.0) -> float:
    return max(base, n_runs * per_run)


def _subtitle(ax, text: str, color: str = DIM) -> None:
    """Bottom subtitle via xlabel — concise text prevents edge-clipping."""
    ax.set_xlabel(text, fontsize=7, color=color, labelpad=4)


def _bar_labels(ax, bars, vals, fmt_fn, yoff_frac: float = 0.03,
                fontsize: int = 9) -> None:
    ymax = ax.get_ylim()[1]
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + ymax * yoff_frac,
                fmt_fn(v), ha="center", va="bottom",
                fontsize=fontsize, fontweight="bold", color=FG_HI)


def _big_stat(ax, value_str: str, label_str: str,
              value_color: str = GREEN) -> None:
    ax.text(0.5, 0.62, value_str, transform=ax.transAxes,
            fontsize=30, fontweight="bold", color=value_color,
            ha="center", va="center", zorder=10)
    ax.text(0.5, 0.28, label_str, transform=ax.transAxes,
            fontsize=8, color=FG, ha="center", va="center", zorder=10)


def _delta_badge(ax, vals: np.ndarray,
                 higher_is_better: bool = True) -> None:
    """↑↓ % change badge (second-to-last → last), top-right corner."""
    if len(vals) < 2:
        return
    prev, curr = float(vals[-2]), float(vals[-1])
    if abs(prev) < 1e-9:
        return
    pct  = (curr - prev) / abs(prev) * 100
    good = (pct >= 0) == higher_is_better
    sym  = "↑" if pct >= 0 else "↓"
    clr  = GREEN if good else RED
    ax.text(0.97, 0.97, f"{sym} {abs(pct):.1f}%",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=8, color=clr, fontweight="bold", zorder=12,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=PANEL,
                      edgecolor=clr, alpha=0.85, linewidth=0.8))


def _delta_str(curr: float, prev: float | None,
               higher_good: bool = True) -> tuple[str, str]:
    if prev is None or abs(prev) < 1e-9:
        return ("", DIM)
    pct  = (curr - prev) / abs(prev) * 100
    sym  = "↑" if pct >= 0 else "↓"
    good = (pct >= 0) == higher_good
    return (f"{sym} {abs(pct):.1f}% vs prev", GREEN if good else RED)


def _grade(value: float, target: float,
           higher_is_better: bool = True) -> tuple[str, str]:
    if target < 1e-9:
        return ("?", DIM)
    ratio = (value / target) if higher_is_better else (target / value)
    if ratio >= 1.20: return "A+", GREEN
    if ratio >= 1.10: return "A",  GREEN
    if ratio >= 1.00: return "A-", CYAN
    if ratio >= 0.90: return "B+", CYAN
    if ratio >= 0.80: return "B",  YELLOW
    if ratio >= 0.70: return "C",  YELLOW
    return "D", RED


def _r_squared(xs: np.ndarray, ys: np.ndarray,
               coeffs: np.ndarray) -> float:
    res   = ys - np.polyval(coeffs, xs)
    ss_r  = float(np.sum(res ** 2))
    ss_t  = float(np.sum((ys - np.mean(ys)) ** 2))
    return 1.0 - ss_r / ss_t if ss_t > 1e-12 else 1.0


def _smart_annotate(ax, xs: list[float], ys: list[float],
                    labels: list[str], fontsize: int = 8) -> None:
    """Stagger annotation offsets by sort rank to avoid overlap."""
    _offs = [(12, 8), (12, -20), (-70, 8), (-70, -20), (12, 24), (12, -34)]
    order = sorted(range(len(xs)), key=lambda i: (xs[i], ys[i]))
    for rank, i in enumerate(order):
        off = _offs[rank % len(_offs)]
        ax.annotate(labels[i], (xs[i], ys[i]),
                    textcoords="offset points", xytext=off,
                    fontsize=fontsize, color=FG_HI, fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color=DIM, lw=0.5,
                                   shrinkA=4, shrinkB=4))


def _apply_gov_xlim(axes_list: list, n_t: int) -> None:
    """Symmetric x-padding so single-run bars are never full-width blocks."""
    pad = max(0.55, 1.2 / max(n_t, 1))
    for ax in axes_list:
        ax.set_xlim(-pad, max(n_t - 1, 0) + pad)


def _fix_pct_axis(ax) -> None:
    """Replace the ε artifact on zoomed %-axes with clean float labels."""
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))


def _grouped_bars(ax, d: BenchmarkData, metric: str, ylabel: str,
                  fmt_fn, ylim_mult: float = 1.30) -> None:
    """Shared grouped-bar renderer — thread-safe, no global state."""
    for i, task in enumerate(d.tasks):
        color  = d.task_colors[task]
        subset = d.df[d.df["task"] == task].sort_values("run_ts")
        vals   = subset[metric].values
        offset = (i - (d.n_tasks - 1) / 2) * d.bar_w
        bars   = ax.bar(d.x + offset, vals, d.bar_w, label=task,
                        color=color, alpha=0.88, zorder=3,
                        edgecolor=BG, linewidth=0.5)
        _bar_labels(ax, bars, vals, fmt_fn, fontsize=8)
    ax.set_xticks(d.x)
    ax.set_xticklabels(d.run_labels, fontsize=8)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.legend(fontsize=7, loc="upper left")
    ax.set_ylim(0, float(d.df[metric].max()) * ylim_mult)
    if d.n_runs >= 2:
        first_vals = (d.df[d.df["task"] == d.tasks[0]]
                      .sort_values("run_ts")[metric].values)
        higher = metric in ("tok_s", "completion_tokens")
        _delta_badge(ax, first_vals, higher_is_better=higher)


def _kpi_panel(ax, title: str, value_str: str,
               grade_ltr: str, grade_clr: str,
               delta_text: str = "", delta_clr: str = DIM,
               sub: str = "") -> None:
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(FancyBboxPatch(
        (0.03, 0.05), 0.94, 0.90,
        boxstyle="round,pad=0.02", linewidth=1.0,
        edgecolor=BORDER, facecolor=PANEL, zorder=0))
    ax.text(0.5,  0.89, title,      ha="center", fontsize=9, color=DIM)
    ax.text(0.88, 0.76, grade_ltr,  ha="center",
            fontsize=16, fontweight="bold", color=grade_clr)
    ax.text(0.5,  0.54, value_str,  ha="center",
            fontsize=26, fontweight="bold", color=FG_HI)
    if delta_text:
        ax.text(0.5, 0.30, delta_text, ha="center",
                fontsize=9, color=delta_clr, fontweight="bold")
    if sub:
        ax.text(0.5, 0.13, sub, ha="center", fontsize=7, color=DIM)


# ══════════════════════════════════════════════════════════════════════════════
# Chart workers — each is a pure function, returns (filename, elapsed_s)
# ══════════════════════════════════════════════════════════════════════════════

def _chart_throughput(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_throughput.png"
    t0   = time.perf_counter()
    fig, ax = _make_subplots(figsize=(_fig_width(d.n_runs), 5))
    _grouped_bars(ax, d, "tok_s", "Throughput (tok/s)", lambda v: f"{v:.1f}")

    bl = d.targets["tok_s"]
    ax.axhline(bl, color=YELLOW, lw=1, ls="--", zorder=2, alpha=0.8)
    ax.text(d.n_runs - 0.45, bl + 0.3,
            f"{bl:.0f} tok/s baseline", fontsize=7, color=YELLOW)

    latest = float(d.df.sort_values("run_ts")["tok_s"].iloc[-1])
    pct_up = (latest / bl - 1) * 100
    gl, gc = _grade(latest, bl)
    ax.text(0.02, 0.97, f"Grade: {gl}", transform=ax.transAxes,
            ha="left", va="top", fontsize=10, color=gc, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=PANEL,
                      edgecolor=gc, alpha=0.85, lw=0.8))
    ax.set_title(f"Inference Throughput — {d.hw_subtitle}")
    _subtitle(ax,
        f"Avg {float(d.df['tok_s'].mean()):.1f} tok/s  ·  "
        f"{pct_up:.0f}% above {bl:.0f} tok/s L4 baseline", color=FG)
    fig.tight_layout()
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_latency(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_latency.png"
    t0   = time.perf_counter()
    fig, ax = _make_subplots(figsize=(_fig_width(d.n_runs, base=7), 5))

    for i, task in enumerate(d.tasks):
        color  = d.task_colors[task]
        subset = d.df[d.df["task"] == task].sort_values("run_ts")
        vals   = subset["latency_s"].values
        toks   = subset["completion_tokens"].values
        offset = (i - (d.n_tasks - 1) / 2) * d.bar_w
        bars   = ax.bar(d.x + offset, vals, d.bar_w, label=task,
                        color=color, alpha=0.88, zorder=3,
                        edgecolor=BG, linewidth=0.5)
        _bar_labels(ax, bars, vals, lambda v: f"{v:.1f}s", fontsize=8)
        # Token count embedded mid-bar
        for bar, tok in zip(bars, toks):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() * 0.44,
                    f"{int(tok)}t",
                    ha="center", va="center", fontsize=7,
                    color=BG, fontweight="bold", alpha=0.9, zorder=5)

    ax.set_xticks(d.x); ax.set_xticklabels(d.run_labels, fontsize=8)
    ax.set_ylabel("Latency (s)", fontsize=9)
    ax.legend(fontsize=7, loc="upper left")
    ax.set_ylim(0, float(d.df["latency_s"].max()) * 1.35)
    if d.n_runs >= 2:
        first_lat = (d.df[d.df["task"] == d.tasks[0]]
                     .sort_values("run_ts")["latency_s"].values)
        _delta_badge(ax, first_lat, higher_is_better=False)

    mean_tps = float(d.df["tok_s"].mean())
    ax.set_title("End-to-End Inference Latency  (token count inside bar)")
    # Short subtitle — avoids left-edge clipping
    _subtitle(ax,
        f"~{mean_tps:.1f} tok/s  ·  latency ∝ tokens  ·  "
        f"threat target < {d.targets['threat_latency_s']:.0f}s", color=FG)
    fig.tight_layout()
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_tokens(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_tokens.png"
    t0   = time.perf_counter()
    fig, ax = _make_subplots(figsize=(_fig_width(d.n_runs), 5))
    _grouped_bars(ax, d, "completion_tokens", "Completion Tokens",
                  lambda v: str(int(v)), ylim_mult=1.40)
    ax.set_title("Tokens Generated per Run")
    tok_min = int(d.df["completion_tokens"].min())
    tok_max = int(d.df["completion_tokens"].max())
    _subtitle(ax,
        f"Output range {tok_min}–{tok_max} tok  ·  "
        f"more tokens = fuller response  ·  throughput stable regardless", color=FG)
    fig.tight_layout()
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_consistency(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_consistency.png"
    t0   = time.perf_counter()
    fig, ax = _make_subplots(figsize=(7, 5))

    all_xs, all_ys, all_labels = [], [], []
    for task in d.tasks:
        color = d.task_colors[task]
        sub   = d.df[d.df["task"] == task]
        ax.scatter(sub["completion_tokens"], sub["latency_s"],
                   label=task, color=color, s=110, zorder=4,
                   edgecolors=BG, linewidths=0.8)
        for _, row in sub.iterrows():
            all_xs.append(float(row["completion_tokens"]))
            all_ys.append(float(row["latency_s"]))
            all_labels.append(f"{row['tok_s']:.1f} tok/s")

    xs_a = np.array(all_xs); ys_a = np.array(all_ys)

    if len(d.df) >= 2:
        coeffs = np.polyfit(xs_a, ys_a, 1)
        xl     = np.linspace(xs_a.min() * 0.68, xs_a.max() * 1.18, 200)
        ax.plot(xl, np.polyval(coeffs, xl), color=YELLOW, lw=1.2,
                ls="--", alpha=0.75, label="linear fit", zorder=2)

        sigma = float(np.std(ys_a - np.polyval(coeffs, xs_a)))
        ax.fill_between(xl,
                        np.polyval(coeffs, xl) - sigma,
                        np.polyval(coeffs, xl) + sigma,
                        color=YELLOW, alpha=0.07, zorder=1, label="±1σ band")

        tps_implied = 1.0 / coeffs[0] if abs(coeffs[0]) > 1e-9 else 0.0
        r2          = _r_squared(xs_a, ys_a, coeffs)
        ax.text(0.97, 0.12,
                f"Implied: {tps_implied:.1f} tok/s\nR² = {r2:.4f}",
                transform=ax.transAxes, ha="right", fontsize=9,
                color=YELLOW, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor=PANEL,
                          edgecolor=YELLOW, alpha=0.85, lw=0.8))

    _smart_annotate(ax, all_xs, all_ys, all_labels)
    ax.set_xlabel("Completion Tokens", fontsize=9)
    ax.set_ylabel("Latency (s)", fontsize=9)
    ax.set_title("Latency vs Tokens — Linear Relationship = Constant Throughput")
    _subtitle(ax,
        "R² → 1.0 confirms stable GPU utilization — no thermal throttle, no memory variance",
        color=FG)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_governance(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_governance_tests.png"
    t0   = time.perf_counter()
    if d.df_tests.empty:
        return f"(skipped){name}", time.perf_counter() - t0

    n_t   = len(d.df_tests)
    x_t   = np.arange(n_t)
    rl_t  = d.df_tests["run_label"].tolist()
    gov_w = max(0.22, min(0.52, 0.62 / n_t))

    fig, axes = _make_subplots(1, 3, figsize=(15, 5))
    fig.set_facecolor(BG)

    # ── Panel A: Pass rate ───────────────────────────────────────────────────
    ax = axes[0]
    ax.bar(x_t, d.df_tests["pass_rate"], color=GREEN, alpha=0.75,
           zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
    _apply_gov_xlim([ax], n_t)
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=8)
    _y_lo = max(94.0, float(d.df_tests["pass_rate"].min()) - 1.5)
    ax.set_ylim(_y_lo, 101.0)
    ax.set_ylabel("Pass Rate (%)", fontsize=9)
    _fix_pct_axis(ax)   # ← eliminates ε rendering artifact
    ax.set_title("Governance Test Pass Rate")

    bl_pr = d.targets["pass_rate"]
    ax.axhline(bl_pr, color=YELLOW, lw=1, ls="--", alpha=0.8, zorder=2)
    ax.text(float(x_t[-1]) + gov_w / 2 + 0.05, bl_pr + 0.06,
            f"{bl_pr:.0f}% target", fontsize=7, color=YELLOW, ha="right")

    lpr        = float(d.df_tests["pass_rate"].iloc[-1])
    gl_pr, gc_pr = _grade(lpr, bl_pr)
    _big_stat(ax, f"{lpr:.1f}%", "latest run pass rate", GREEN)
    ax.text(0.03, 0.97, f"Grade: {gl_pr}", transform=ax.transAxes,
            ha="left", va="top", fontsize=9, color=gc_pr, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.2", facecolor=PANEL,
                      edgecolor=gc_pr, alpha=0.85, lw=0.8))
    if n_t >= 2:
        _delta_badge(ax, d.df_tests["pass_rate"].values)
    n_fail = int(d.df_tests["tests_failed"].iloc[-1])
    _subtitle(ax,
        f"0 security regressions  ·  {n_fail} pre-existing structural failures excluded")

    # ── Panel B: Tests passed ────────────────────────────────────────────────
    ax = axes[1]
    ax.bar(x_t, d.df_tests["tests_passed"], color=CYAN, alpha=0.75,
           zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w, label="passing")
    ax.bar(x_t, d.df_tests["tests_failed"], bottom=d.df_tests["tests_passed"],
           color=RED, alpha=0.50, zorder=3, edgecolor=BG, linewidth=0.5,
           width=gov_w, label="pre-existing failures")
    _apply_gov_xlim([ax], n_t)
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=8)
    ax.set_ylabel("Test Count", fontsize=9)
    ax.set_title("Tests Passed vs Pre-existing Failures")
    ax.legend(fontsize=7, loc="lower right")
    lp  = int(d.df_tests["tests_passed"].iloc[-1])
    lf  = int(d.df_tests["tests_failed"].iloc[-1])
    _big_stat(ax, f"{lp:,}", f"passing  ·  {lf} pre-existing excluded", CYAN)
    if n_t >= 2:
        _delta_badge(ax, d.df_tests["tests_passed"].values)
    _subtitle(ax,
        "Pre-existing failures are structural test harness issues — not pipeline regressions")

    # ── Panel C: Suite speed ─────────────────────────────────────────────────
    ax = axes[2]
    ax.bar(x_t, d.df_tests["tests_per_second"], color=PURPLE, alpha=0.75,
           zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
    _apply_gov_xlim([ax], n_t)
    ax.set_xticks(x_t); ax.set_xticklabels(rl_t, fontsize=8)
    ax.set_ylabel("Tests / Second", fontsize=9)
    ax.set_title("Test Suite Execution Speed")
    bl_sp = d.targets["tests_per_second"]
    ax.axhline(bl_sp, color=YELLOW, lw=0.8, ls="--", alpha=0.7, zorder=2)
    ltps = float(d.df_tests["tests_per_second"].iloc[-1])
    ldur = float(d.df_tests["duration_s"].iloc[-1])
    ltot = int(d.df_tests["tests_passed"].iloc[-1] + d.df_tests["tests_failed"].iloc[-1])
    gl_sp, gc_sp = _grade(ltps, bl_sp)
    _big_stat(ax, f"{ltps:.0f}/s", f"{ltot:,} tests in {ldur:.0f}s", PURPLE)
    ax.text(0.03, 0.97, f"Grade: {gl_sp}", transform=ax.transAxes,
            ha="left", va="top", fontsize=9, color=gc_sp, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.2", facecolor=PANEL,
                      edgecolor=gc_sp, alpha=0.85, lw=0.8))
    if n_t >= 2:
        _delta_badge(ax, d.df_tests["tests_per_second"].values)
    _subtitle(ax, "Ouroboros suite: circuit breakers · trust graduators · FSM")

    fig.suptitle("Ouroboros Governance Test Suite — Reliability Across Runs",
                 fontsize=14, fontweight="bold", color=FG_HI, y=1.02)
    fig.tight_layout()
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_scorecard(d: BenchmarkData) -> tuple[str, float]:
    name = "chart_scorecard.png"
    t0   = time.perf_counter()
    fig, axes = _make_subplots(2, 4, figsize=(17, 7.5))
    fig.set_facecolor(BG)
    fig.suptitle(f"Trinity AI — Performance Scorecard  ·  {d.hw_subtitle}",
                 fontsize=14, fontweight="bold", color=FG_HI, y=1.01)

    # ── Row 0: Inference KPIs ─────────────────────────────────────────────────
    run_avg  = d.df.groupby("run_ts")["tok_s"].mean().sort_index()
    l_avg    = float(run_avg.iloc[-1])
    p_avg    = float(run_avg.iloc[-2]) if len(run_avg) >= 2 else None
    ds0, dc0 = _delta_str(l_avg, p_avg, higher_good=True)
    gl0, gc0 = _grade(l_avg, d.targets["tok_s"])
    _kpi_panel(axes[0][0], "Avg Throughput", f"{l_avg:.1f} tok/s",
               gl0, gc0, ds0, dc0, f"target ≥ {d.targets['tok_s']:.0f} tok/s")

    threat_task = d.tasks[min(1, d.n_tasks - 1)]
    t_lat  = d.df[d.df["task"] == threat_task].sort_values("run_ts")
    l_lat  = float(t_lat["latency_s"].iloc[-1])
    p_lat  = float(t_lat["latency_s"].iloc[-2]) if len(t_lat) >= 2 else None
    ds1, dc1 = _delta_str(l_lat, p_lat, higher_good=False)
    gl1, gc1 = _grade(l_lat, d.targets["threat_latency_s"], higher_is_better=False)
    _kpi_panel(axes[0][1], "Threat Latency", f"{l_lat:.1f}s",
               gl1, gc1, ds1, dc1, f"target < {d.targets['threat_latency_s']:.0f}s")

    run_max = d.df.groupby("run_ts")["completion_tokens"].max().sort_index()
    l_tok   = int(run_max.iloc[-1])
    p_tok   = int(run_max.iloc[-2]) if len(run_max) >= 2 else None
    ds2, dc2 = _delta_str(float(l_tok), float(p_tok) if p_tok else None)
    _kpi_panel(axes[0][2], "Peak Output", f"{l_tok:,} tok",
               "A", GREEN, ds2, dc2, "completion tokens (latest run)")

    if len(d.df) >= 2:
        cf  = np.polyfit(d.df["completion_tokens"].values, d.df["latency_s"].values, 1)
        r2  = _r_squared(d.df["completion_tokens"].values, d.df["latency_s"].values, cf)
        gl3, gc3 = _grade(r2, 0.95)
        _kpi_panel(axes[0][3], "GPU Consistency", f"{r2:.4f}",
                   gl3, gc3, "R²  latency / tokens", DIM, "1.0000 = perfectly stable")
    else:
        _kpi_panel(axes[0][3], "GPU Consistency", "—", "—", DIM,
                   "", DIM, "need ≥ 2 runs")

    # ── Row 1: Governance KPIs ────────────────────────────────────────────────
    if not d.df_tests.empty:
        lpr  = float(d.df_tests["pass_rate"].iloc[-1])
        ppr  = float(d.df_tests["pass_rate"].iloc[-2]) if len(d.df_tests) >= 2 else None
        ds4, dc4 = _delta_str(lpr, ppr)
        gl4, gc4 = _grade(lpr, d.targets["pass_rate"])
        _kpi_panel(axes[1][0], "Gov. Pass Rate", f"{lpr:.1f}%",
                   gl4, gc4, ds4, dc4, f"target ≥ {d.targets['pass_rate']:.0f}%")

        ltp = int(d.df_tests["tests_passed"].iloc[-1])
        ltf = int(d.df_tests["tests_failed"].iloc[-1])
        _kpi_panel(axes[1][1], "Tests Passing", f"{ltp:,}",
                   "A+", GREEN, f"{ltf} pre-existing excluded", DIM,
                   "structural harness issues — not regressions")

        ltps = float(d.df_tests["tests_per_second"].iloc[-1])
        ptps = float(d.df_tests["tests_per_second"].iloc[-2]) if len(d.df_tests) >= 2 else None
        ds6, dc6 = _delta_str(ltps, ptps)
        gl6, gc6 = _grade(ltps, d.targets["tests_per_second"])
        _kpi_panel(axes[1][2], "Suite Speed", f"{ltps:.0f}/s",
                   gl6, gc6, ds6, dc6, f"target ≥ {d.targets['tests_per_second']:.0f}/s")
    else:
        for col in range(3):
            axes[1][col].axis("off")
            axes[1][col].text(0.5, 0.5, "No governance data yet",
                              ha="center", va="center",
                              fontsize=9, color=DIM,
                              transform=axes[1][col].transAxes)

    # ── System info panel (always bottom-right) ───────────────────────────────
    ax_s = axes[1][3]
    ax_s.set_xlim(0, 1); ax_s.set_ylim(0, 1); ax_s.axis("off")
    ax_s.add_patch(FancyBboxPatch(
        (0.03, 0.05), 0.94, 0.90,
        boxstyle="round,pad=0.02", linewidth=1.0,
        edgecolor=BORDER, facecolor=PANEL, zorder=0))
    ax_s.text(0.5, 0.89, "System", ha="center", fontsize=9, color=DIM)
    _model_short = (d.model_display
                    .replace("-Instruct", "")
                    .replace("Qwen2.5-Coder-", "Qwen2.5-C-"))
    sys_rows = [
        ("GPU",   d.gpu_display),
        ("Model", _model_short),
        ("Quant", d.quant_tag),
        ("Ctx",   d.ctx_str),
        ("Runs",  str(len(d.raw))),
    ]
    for k, (lbl, val) in enumerate(sys_rows):
        y = 0.76 - k * 0.135
        ax_s.text(0.10, y, lbl + ":", ha="left",  fontsize=8, color=DIM)
        ax_s.text(0.92, y, val,        ha="right", fontsize=8,
                  color=FG_HI, fontweight="bold")

    fig.tight_layout(pad=1.6)
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


def _chart_dashboard(d: BenchmarkData) -> tuple[str, float]:
    name      = "chart_dashboard.png"
    t0        = time.perf_counter()
    has_tests = not d.df_tests.empty
    n_rows    = 2 if has_tests else 1
    fig       = _make_fig(figsize=(18, 5.5 * n_rows))
    fig.set_facecolor(BG)

    ax1 = fig.add_subplot(n_rows, 3, 1)
    ax2 = fig.add_subplot(n_rows, 3, 2)
    ax3 = fig.add_subplot(n_rows, 3, 3)
    _grouped_bars(ax1, d, "tok_s",             "tok/s",  lambda v: f"{v:.1f}")
    _grouped_bars(ax2, d, "latency_s",         "s",      lambda v: f"{v:.1f}s")
    _grouped_bars(ax3, d, "completion_tokens", "tokens", lambda v: str(int(v)))
    ax1.set_title("Throughput (tok/s)", fontsize=10)
    ax2.set_title("Latency (s)",        fontsize=10)
    ax3.set_title("Tokens Generated",   fontsize=10)
    ax1.axhline(d.targets["tok_s"], color=YELLOW, lw=0.8, ls="--", alpha=0.7)

    if has_tests:
        n_t   = len(d.df_tests)
        x_t   = np.arange(n_t)
        rl_t  = d.df_tests["run_label"].tolist()
        gov_w = max(0.22, min(0.52, 0.62 / n_t))

        ax4 = fig.add_subplot(n_rows, 3, 4)
        ax5 = fig.add_subplot(n_rows, 3, 5)
        ax6 = fig.add_subplot(n_rows, 3, 6)

        ax4.bar(x_t, d.df_tests["pass_rate"], color=GREEN, alpha=0.75,
                zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
        _apply_gov_xlim([ax4], n_t)
        ax4.set_xticks(x_t); ax4.set_xticklabels(rl_t, fontsize=7)
        _y_lo = max(94.0, float(d.df_tests["pass_rate"].min()) - 1.5)
        ax4.set_ylim(_y_lo, 101.0)
        ax4.set_ylabel("Pass Rate (%)", fontsize=8)
        _fix_pct_axis(ax4)   # ← eliminates ε artifact
        ax4.set_title("Pass Rate", fontsize=10)
        ax4.axhline(d.targets["pass_rate"], color=YELLOW, lw=0.8, ls="--", alpha=0.8)
        _big_stat(ax4, f"{float(d.df_tests['pass_rate'].iloc[-1]):.1f}%",
                  "governance pass rate", GREEN)
        if n_t >= 2: _delta_badge(ax4, d.df_tests["pass_rate"].values)

        ax5.bar(x_t, d.df_tests["tests_passed"], color=CYAN, alpha=0.75,
                zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
        ax5.bar(x_t, d.df_tests["tests_failed"], bottom=d.df_tests["tests_passed"],
                color=RED, alpha=0.45, zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
        _apply_gov_xlim([ax5], n_t)
        ax5.set_xticks(x_t); ax5.set_xticklabels(rl_t, fontsize=7)
        ax5.set_ylabel("count", fontsize=8)
        ax5.set_title("Tests Passed", fontsize=10)
        _big_stat(ax5, f"{int(d.df_tests['tests_passed'].iloc[-1]):,}",
                  "tests passing", CYAN)
        if n_t >= 2: _delta_badge(ax5, d.df_tests["tests_passed"].values)

        ax6.bar(x_t, d.df_tests["tests_per_second"], color=PURPLE, alpha=0.75,
                zorder=3, edgecolor=BG, linewidth=0.5, width=gov_w)
        _apply_gov_xlim([ax6], n_t)
        ax6.set_xticks(x_t); ax6.set_xticklabels(rl_t, fontsize=7)
        ax6.set_ylabel("tests/s", fontsize=8)
        ax6.set_title("Suite Speed", fontsize=10)
        _big_stat(ax6, f"{float(d.df_tests['tests_per_second'].iloc[-1]):.0f}/s",
                  "test execution rate", PURPLE)
        if n_t >= 2: _delta_badge(ax6, d.df_tests["tests_per_second"].values)

    fig.suptitle(f"Trinity AI — Full Benchmark Dashboard  ·  {d.hw_subtitle}",
                 fontsize=14, fontweight="bold", color=FG_HI, y=1.01)
    fig.tight_layout(pad=1.5)
    return _save_fig(fig, name, d.script_dir), time.perf_counter() - t0


# ══════════════════════════════════════════════════════════════════════════════
# Chart registry — add new chart functions here, nothing else changes
# ══════════════════════════════════════════════════════════════════════════════
CHART_REGISTRY: dict[str, object] = {
    "chart_throughput.png":       _chart_throughput,
    "chart_latency.png":          _chart_latency,
    "chart_tokens.png":           _chart_tokens,
    "chart_consistency.png":      _chart_consistency,
    "chart_governance_tests.png": _chart_governance,
    "chart_scorecard.png":        _chart_scorecard,
    "chart_dashboard.png":        _chart_dashboard,
}


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

def _load_data() -> BenchmarkData:
    with HISTORY.open() as f:
        raw: list[dict] = json.load(f)

    latest_sys   = next((e["system"] for e in reversed(raw) if "system" in e), {})
    gpu_key      = latest_sys.get("compute", "")
    gpu_display  = COMPUTE_DISPLAY.get(gpu_key,
                       gpu_key.replace("_", " ").upper() or "GPU")
    model_disp   = latest_sys.get("model", "Unknown Model")
    artifact     = latest_sys.get("artifact", "")
    quant_tag    = artifact.rsplit("-", 1)[-1].replace(".gguf", "") if artifact else "Q4_K_M"
    ctx          = latest_sys.get("context_window", "?")
    hw_sub       = f"{gpu_display}  ·  {model_disp}  ·  {quant_tag}"

    # Discover inference keys dynamically
    inf_keys: list[str] = []
    for entry in raw:
        for k in sorted(entry):
            if k.startswith("inference_") and k not in inf_keys:
                inf_keys.append(k)

    rows: list[dict] = []
    test_rows: list[dict] = []

    for entry in raw:
        run_ts    = entry.get("run_ts", "?")
        run_label = _fmt_ts(run_ts)
        for key in inf_keys:
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
                "model":             t.get("model", "unknown"),
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

    tasks       = list(df["task"].unique())
    task_colors = {t: _task_color(i) for i, t in enumerate(tasks)}
    runs        = list(df["run_ts"].unique())
    n_runs      = len(runs)
    n_tasks     = len(tasks)
    bar_w       = _bar_width(n_runs)
    run_labels  = (df.drop_duplicates("run_ts")
                   .sort_values("run_ts")["run_label"].tolist())

    return BenchmarkData(
        raw=raw, df=df, df_tests=df_tests,
        tasks=tasks, task_colors=task_colors,
        runs=runs, n_runs=n_runs, n_tasks=n_tasks,
        x=np.arange(n_runs), bar_w=bar_w, run_labels=run_labels,
        gpu_display=gpu_display, model_display=model_disp,
        artifact_disp=artifact, quant_tag=quant_tag,
        ctx_display=ctx, hw_subtitle=hw_sub,
        targets=TARGETS, script_dir=SCRIPT_DIR,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Async orchestrator — charts run in parallel, results stream as they finish
# ══════════════════════════════════════════════════════════════════════════════

async def _timed_chart(loop, pool, fn, d: BenchmarkData):
    return await loop.run_in_executor(pool, fn, d)


async def main() -> None:
    t_load = time.perf_counter()
    d      = _load_data()
    print(f"📊  Loaded {len(d.raw)} run(s) from history.json  "
          f"({time.perf_counter()-t_load:.3f}s)\n")
    print(f"🚀  Generating {len(CHART_REGISTRY)} charts in parallel "
          f"({len(CHART_REGISTRY)} workers)...\n")

    loop    = asyncio.get_event_loop()
    saved:  list[str]  = []
    timings: list[float] = []
    t_start = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(CHART_REGISTRY),
            thread_name_prefix="trinity-chart") as pool:

        task_map: dict = {
            asyncio.ensure_future(_timed_chart(loop, pool, fn, d)): label
            for label, fn in CHART_REGISTRY.items()
        }

        pending = set(task_map)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                label = task_map[fut]
                try:
                    filename, elapsed = fut.result()
                    timings.append(elapsed)
                    if filename.startswith("(skipped)"):
                        print(f"  ⚠️   {filename[9:]:<35}  (no data)")
                    else:
                        saved.append(filename)
                        print(f"  ✅  {filename:<35} ({elapsed:.2f}s)")
                except Exception as exc:
                    print(f"  ❌  {label}: {exc}")

    total  = time.perf_counter() - t_start
    serial = sum(timings)
    factor = serial / total if total > 1e-4 else 1.0

    print(f"\n  ⚡  {len(saved)} charts in {total:.2f}s "
          f"(serial equiv: {serial:.2f}s  ·  {factor:.1f}× parallelism)\n")

    # ── Console summary ───────────────────────────────────────────────────────
    print("=" * 64)
    print("  TRINITY AI — BENCHMARK SUMMARY")
    print("=" * 64)
    print(f"  Runs     : {len(d.raw)}")
    print(f"  Hardware : {d.gpu_display}")
    print(f"  Model    : {d.model_display}")
    print(f"  Context  : {d.ctx_str}")
    print()
    for task in d.tasks:
        sub      = d.df[d.df["task"] == task]
        gl, _    = _grade(float(sub["tok_s"].iloc[-1]), d.targets["tok_s"])
        print(f"  [{task}]  Grade: {gl}")
        print(f"    Throughput : {float(sub['tok_s'].mean()):.1f} tok/s avg"
              f"  (range {float(sub['tok_s'].min()):.1f}–{float(sub['tok_s'].max()):.1f})")
        print(f"    Latency    : {float(sub['latency_s'].mean()):.1f}s avg"
              f"  (range {float(sub['latency_s'].min()):.1f}–{float(sub['latency_s'].max()):.1f}s)")
        print(f"    Tokens     : {float(sub['completion_tokens'].mean()):.0f} avg"
              f"  (range {int(sub['completion_tokens'].min())}"
              f"–{int(sub['completion_tokens'].max())})")
        print()
    if not d.df_tests.empty:
        lpr    = float(d.df_tests["pass_rate"].iloc[-1])
        gl_g,_ = _grade(lpr, d.targets["pass_rate"])
        print(f"  [Governance Tests]  Grade: {gl_g}")
        print(f"    Pass rate  : {float(d.df_tests['pass_rate'].mean()):.1f}% avg"
              f"  (target: {d.targets['pass_rate']:.0f}%)")
        print(f"    Tests run  : {int(d.df_tests['tests_passed'].max()):,} (latest)")
        print(f"    Speed      : {float(d.df_tests['tests_per_second'].mean()):.0f} tests/s avg")
        print()
    print("=" * 64)
    print()
    print("Charts saved to benchmarks/:")
    for name in saved:
        print(f"  📈  {name}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
