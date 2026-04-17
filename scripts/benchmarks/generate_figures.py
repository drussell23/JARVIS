#!/usr/bin/env python3
"""
Generate presentation-grade data visualizations for the DW benchmark report.

Output: PNG files in docs/benchmarks/figures/ at 300 DPI, research-paper styled.

Design principles (v2, tuned after Derek's feedback on v1 clutter):
- Larger base figure size so each chart has breathing room in the PDF.
- 300 DPI for print sharpness.
- Fewer elements per chart — ONE headline insight per figure.
- Larger, readable fonts (12pt axis labels, 13pt title).
- Consistent palette — DoubleWord blue, Claude amber, neutral greys.
- Minimal gridlines, no unnecessary decorations.
- Annotations in the figure body rather than as captions (captions go in the MD).

Figures produced:
    fig01_cost_per_op.png         — Per-op cost (horizontal bar, log scale)
    fig02_spend_split.png         — Current vs design-intent spend donuts
    fig03_smoke_test_timeline.png — 4 runs, stream vs non-stream wall-clock
    fig04_token_composition.png   — Qwen 397B reasoning vs visible stacked bars
    fig05_scaling_economics.png   — Daily cost at scale, log-log
    fig06_sse_chunk_rate.png      — Qwen 397B agent-scale stream timeline

Security
--------
Reads no secrets. Pure static data from the report body.
"""
from __future__ import annotations

import os
from pathlib import Path

# Must be set BEFORE importing matplotlib
_TMP = os.environ.get("TMPDIR", "/tmp")
os.environ.setdefault("MPLCONFIGDIR", f"{_TMP}/mpl-config")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np

# ---------------------------------------------------------------------------
# Presentation-grade defaults (v2 — tuned for readability in an embedded PDF)
# ---------------------------------------------------------------------------
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 12,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.titlepad": 16,
    "axes.labelsize": 12,
    "axes.labelweight": "normal",
    "axes.labelpad": 10,
    "axes.linewidth": 1.0,
    "axes.edgecolor": "#3c3c3c",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": "#e8e8e8",
    "grid.linewidth": 0.6,
    "xtick.color": "#3c3c3c",
    "ytick.color": "#3c3c3c",
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "xtick.major.pad": 6,
    "ytick.major.pad": 6,
    "legend.fontsize": 11,
    "legend.frameon": False,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.35,
    "savefig.facecolor": "white",
    "figure.facecolor": "white",
})

# Color palette — restrained, print-friendly, colorblind-conscious
DW_BLUE = "#2c5aa0"          # DoubleWord primary
DW_BLUE_LIGHT = "#8aa8d0"    # DoubleWord secondary
CLAUDE_AMBER = "#c87533"     # Claude primary (amber, not orange)
CLAUDE_AMBER_LIGHT = "#e0b890"
OPUS_RED = "#8b3030"
HAIKU_LIGHT = "#e8c99a"
GREEN_OK = "#2d7a4f"
RED_FAIL = "#a13838"
AMBER_ACCENT = "#b08800"
NEUTRAL_DARK = "#2c2c2c"
NEUTRAL_MID = "#7a7a7a"
NEUTRAL_LIGHT = "#d8d8d8"

OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks" / "figures"
OUT.mkdir(parents=True, exist_ok=True)


def _save(fig, name: str) -> None:
    path = OUT / name
    fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white", pad_inches=0.35)
    plt.close(fig)
    print(f"  wrote {path.relative_to(path.parent.parent.parent.parent)}")


# ---------------------------------------------------------------------------
# Figure 1 — Per-operation cost (simpler, more breathing room)
# ---------------------------------------------------------------------------
def fig01_cost_per_op() -> None:
    """
    Horizontal bars, log x-axis. Dollar annotations inline with each bar.
    No legend needed — bar colors and labels are self-describing.
    """
    providers = [
        "DoubleWord\n(Qwen 397B / Gemma 31B)",
        "Claude Haiku",
        "Claude Sonnet",
        "Claude Opus",
    ]
    costs = [
        0.0016,    # DW avg
        0.0152,    # Haiku
        0.057,     # Sonnet
        0.285,     # Opus
    ]
    colors = [DW_BLUE, HAIKU_LIGHT, CLAUDE_AMBER, OPUS_RED]

    fig, ax = plt.subplots(figsize=(11, 5.0))
    y_pos = np.arange(len(providers))
    ax.barh(y_pos, costs, color=colors, edgecolor=NEUTRAL_DARK, linewidth=0.7, height=0.55)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(providers, fontsize=12)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Cost per 7,000-token operation (USD, log scale)")
    ax.set_title("Per-operation cost — 7K-token reasoning op (4K input + 3K output)",
                 loc="left", pad=18)

    # Inline dollar annotations
    for i, c in enumerate(costs):
        ax.text(c * 1.22, i, f"${c:.4f}",
                va="center", ha="left", fontsize=12, color=NEUTRAL_DARK, fontweight="bold")

    ax.set_xlim(0.0007, 1.2)
    ax.grid(axis="x", alpha=0.4)
    ax.grid(axis="y", visible=False)

    # Ratio callout
    sonnet_ratio = costs[2] / costs[0]
    opus_ratio = costs[3] / costs[0]
    ax.text(0.0008, 3.55,
            f"DoubleWord is {sonnet_ratio:.0f}× cheaper than Claude Sonnet\n"
            f"and {opus_ratio:.0f}× cheaper than Claude Opus for equivalent work",
            fontsize=12, color=DW_BLUE, style="italic", fontweight="bold",
            bbox=dict(facecolor="#f0f5ff", edgecolor=DW_BLUE, boxstyle="round,pad=0.7", linewidth=1.2))

    _save(fig, "fig01_cost_per_op.png")


# ---------------------------------------------------------------------------
# Figure 2 — Spend split donuts (current vs design-intent)
# ---------------------------------------------------------------------------
def fig02_spend_split() -> None:
    """
    Two donuts side by side with clear titles. Simpler labels — no embedded
    dollar amounts cluttering the wedges; those go in the subtitle.
    """
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.4))

    # Current actual
    actual_labels = ["Claude\n98.3%", "DoubleWord\n1.7%"]
    actual_sizes = [98.3, 1.7]
    actual_colors = [CLAUDE_AMBER, DW_BLUE]

    axes[0].pie(
        actual_sizes, labels=actual_labels, colors=actual_colors,
        startangle=90, counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 2.5},
        textprops={"fontsize": 13, "color": NEUTRAL_DARK, "fontweight": "bold"},
        labeldistance=1.18,
    )
    axes[0].set_title("Current reality\n(Apr 6–16, 160 sessions, $18.63 total)",
                      fontsize=13, pad=20)

    # Design-intent target
    intent_labels = ["Claude\n~50%", "DoubleWord\n~50%"]
    intent_sizes = [50, 50]
    intent_colors = [CLAUDE_AMBER_LIGHT, DW_BLUE]

    axes[1].pie(
        intent_sizes, labels=intent_labels, colors=intent_colors,
        startangle=90, counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 2.5},
        textprops={"fontsize": 13, "color": NEUTRAL_DARK, "fontweight": "bold"},
        labeldistance=1.18,
    )
    axes[1].set_title("Design-intent target\n(per urgency_router.py cost model)",
                      fontsize=13, pad=20)

    fig.suptitle("Inference-spend split: current vs designed",
                 fontsize=15, fontweight="bold", y=1.02)
    _save(fig, "fig02_spend_split.png")


# ---------------------------------------------------------------------------
# Figure 3 — Smoke test timeline (simplified)
# ---------------------------------------------------------------------------
def fig03_smoke_test_timeline() -> None:
    """
    Two bars per run — stream on top, non-stream below. Color only (green =
    completed, red = failed). Time annotation inline. No verbose labels.
    """
    runs = [
        "Qwen 397B\nsmall payload",
        "Gemma 31B\nsmall payload",
        "Qwen 397B\nagent-scale",
        "Gemma 31B\nagent-scale",
    ]
    stream_times = [28.6, 16.8, 258.6, 29.8]
    stream_ok = [True, True, True, True]
    nonstream_times = [30.5, 14.7, 30.6, 30.7]
    nonstream_ok = [False, True, False, False]

    y = np.arange(len(runs))
    h = 0.36

    fig, ax = plt.subplots(figsize=(11.5, 5.8))

    s_colors = [GREEN_OK if ok else RED_FAIL for ok in stream_ok]
    ax.barh(y - h/2, stream_times, h, color=s_colors, edgecolor=NEUTRAL_DARK, linewidth=0.7)

    ns_colors = [GREEN_OK if ok else RED_FAIL for ok in nonstream_ok]
    ax.barh(y + h/2, nonstream_times, h, color=ns_colors, edgecolor=NEUTRAL_DARK, linewidth=0.7, alpha=0.72)

    ax.set_yticks(y)
    ax.set_yticklabels(runs, fontsize=12)
    ax.invert_yaxis()
    ax.set_xlabel("Wall-clock seconds")
    ax.set_title("Apr 16 smoke-test runs — stream (top of each pair) vs non-stream (bottom)",
                 loc="left", pad=18)

    # Simple inline annotations — just the time
    for i, (s, ok) in enumerate(zip(stream_times, stream_ok)):
        ax.text(s + 4, i - h/2, f"{s:.1f}s", va="center", fontsize=11,
                color=NEUTRAL_DARK, fontweight="bold")
    for i, (n, ok) in enumerate(zip(nonstream_times, nonstream_ok)):
        label = f"{n:.1f}s" + (" (timed out)" if not ok else "")
        ax.text(n + 4, i + h/2, label, va="center", fontsize=11, color=NEUTRAL_DARK)

    # 30s timeout reference line
    ax.axvline(x=30, linestyle="--", color=AMBER_ACCENT, linewidth=1.3, alpha=0.8)
    ax.text(30, -0.9, "30s client\nread-timeout",
            fontsize=10, color=AMBER_ACCENT, ha="center", va="bottom", fontweight="bold")

    ax.set_xlim(0, 300)
    ax.grid(axis="x", alpha=0.4)
    ax.grid(axis="y", visible=False)

    # Clean legend
    legend_elements = [
        Patch(facecolor=GREEN_OK, edgecolor=NEUTRAL_DARK, label="Completed normally"),
        Patch(facecolor=RED_FAIL, edgecolor=NEUTRAL_DARK, label="Timed out at client 30s read-timeout"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", fontsize=11)

    _save(fig, "fig03_smoke_test_timeline.png")


# ---------------------------------------------------------------------------
# Figure 4 — Qwen 397B token composition (cleaner stacked bars)
# ---------------------------------------------------------------------------
def fig04_token_composition() -> None:
    """
    Simple stacked bars. Clear separation between reasoning (light) and
    visible (dark). Clean percentage labels inside each segment.
    """
    scales = ["Small payload\n181 input tokens", "Agent-scale payload\n1,489 input tokens"]
    reasoning = [1276, 8417]
    visible = [184, 5716]  # (1460-1276, 14133-8417)

    x = np.arange(len(scales))
    width = 0.48

    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    ax.bar(x, reasoning, width, label="Hidden reasoning tokens",
           color=DW_BLUE_LIGHT, edgecolor=NEUTRAL_DARK, linewidth=0.7)
    ax.bar(x, visible, width, bottom=reasoning, label="Visible completion tokens",
           color=DW_BLUE, edgecolor=NEUTRAL_DARK, linewidth=0.7)

    ax.set_ylabel("Completion tokens")
    ax.set_xticks(x)
    ax.set_xticklabels(scales, fontsize=12)
    ax.set_title("Qwen 3.5 397B output: hidden reasoning tokens dominate completion",
                 loc="left", pad=18)

    # Inside-segment percentage + token count
    for i, (r, v) in enumerate(zip(reasoning, visible)):
        total = r + v
        r_pct = 100 * r / total
        v_pct = 100 - r_pct
        ax.text(i, r / 2, f"{r_pct:.0f}%\n{r:,}", ha="center", va="center",
                fontsize=12, color="white", fontweight="bold")
        ax.text(i, r + v / 2, f"{v_pct:.0f}%\n{v:,}", ha="center", va="center",
                fontsize=12, color="white", fontweight="bold")
        # Total above bar
        ax.text(i, total + 600, f"total: {total:,} tokens", ha="center",
                fontsize=11, color=NEUTRAL_DARK, fontweight="bold")

    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, 17000)
    ax.legend(loc="upper left", fontsize=11)

    _save(fig, "fig04_token_composition.png")


# ---------------------------------------------------------------------------
# Figure 5 — Scaling economics (simpler log-log)
# ---------------------------------------------------------------------------
def fig05_scaling_economics() -> None:
    """
    Log-log, three lines (DW, Sonnet, Opus). No inline annotations per point;
    one clean callout box at bottom with the headline savings number.
    """
    agents = np.array([100, 1_000, 10_000, 100_000, 1_000_000])
    ops_per_day = 1000
    cost_dw = (4000 * 0.10 / 1e6) + (3000 * 0.40 / 1e6)
    cost_sonnet = (4000 * 3.0 / 1e6) + (3000 * 15.0 / 1e6)
    cost_opus = (4000 * 15.0 / 1e6) + (3000 * 75.0 / 1e6)

    daily_dw = agents * ops_per_day * cost_dw
    daily_sonnet = agents * ops_per_day * cost_sonnet
    daily_opus = agents * ops_per_day * cost_opus

    fig, ax = plt.subplots(figsize=(11.5, 5.8))
    ax.loglog(agents, daily_opus, "-o", color=OPUS_RED, linewidth=2.2, markersize=9,
              label="Claude Opus")
    ax.loglog(agents, daily_sonnet, "-o", color=CLAUDE_AMBER, linewidth=2.2, markersize=9,
              label="Claude Sonnet")
    ax.loglog(agents, daily_dw, "-o", color=DW_BLUE, linewidth=2.8, markersize=10,
              label="DoubleWord 397B / Gemma 31B")

    ax.set_xlabel("Deployed autonomous agents (each running 1,000 ops/day at 7K tokens)")
    ax.set_ylabel("Daily inference cost (USD, log scale)")
    ax.set_title("Daily inference cost at scale — DoubleWord vs Claude",
                 loc="left", pad=18)
    ax.legend(loc="upper left", fontsize=11)

    # One clean savings callout
    saved_daily = daily_sonnet[-1] - daily_dw[-1]
    saved_yearly = saved_daily * 365
    ax.text(
        0.98, 0.03,
        f"At 1,000,000 deployed agents:\n"
        f"${saved_daily:,.0f}/day saved vs Claude Sonnet\n"
        f"≈ ${saved_yearly/1e6:,.0f}M/year",
        transform=ax.transAxes, fontsize=11.5, color=DW_BLUE, fontweight="bold",
        ha="right", va="bottom",
        bbox=dict(facecolor="#f0f5ff", edgecolor=DW_BLUE, boxstyle="round,pad=0.7", linewidth=1.3),
    )

    ax.grid(True, which="major", alpha=0.45)
    ax.grid(True, which="minor", alpha=0.2)
    _save(fig, "fig05_scaling_economics.png")


# ---------------------------------------------------------------------------
# Figure 6 — SSE chunk-rate profile (cleaner timeline)
# ---------------------------------------------------------------------------
def fig06_sse_chunk_rate() -> None:
    """
    Single horizontal timeline showing three phases of the Qwen 397B
    agent-scale stream. Dashed line for 30s stall threshold. One legend box.
    """
    total = 258.6
    ttfb = 1.87
    ttft = 154.5

    fig, ax = plt.subplots(figsize=(12.0, 3.8))

    # Three phases
    ax.barh(0, ttfb, left=0, height=0.55, color=NEUTRAL_LIGHT,
            edgecolor=NEUTRAL_DARK, linewidth=0.7,
            label="Stream open / handshake")
    ax.barh(0, ttft - ttfb, left=ttfb, height=0.55, color=DW_BLUE_LIGHT,
            edgecolor=NEUTRAL_DARK, linewidth=0.7,
            label="Reasoning phase (SSE keepalive chunks, no visible tokens yet)")
    ax.barh(0, total - ttft, left=ttft, height=0.55, color=DW_BLUE,
            edgecolor=NEUTRAL_DARK, linewidth=0.7,
            label="Content emission phase (visible tokens)")

    # Phase labels
    ax.text(ttfb / 2, 0, "1.9s", ha="center", va="center",
            fontsize=10, color=NEUTRAL_DARK, fontweight="bold")
    ax.text(ttfb + (ttft - ttfb) / 2, 0, "~152s reasoning",
            ha="center", va="center", fontsize=11, color="white", fontweight="bold")
    ax.text(ttft + (total - ttft) / 2, 0, "~104s content",
            ha="center", va="center", fontsize=11, color="white", fontweight="bold")

    # 30s stall threshold line
    ax.axvline(x=30, linestyle="--", color=AMBER_ACCENT, linewidth=1.5, alpha=0.85)
    ax.text(30, -0.56, "30s client\nno-data threshold",
            fontsize=10, color=AMBER_ACCENT, ha="center", va="top", fontweight="bold")

    # Result callout on the right
    ax.text(
        total + 20, 0,
        f"258.6s total\n3,798 SSE chunks\nNO STALL",
        fontsize=11, color=GREEN_OK, fontweight="bold", ha="left", va="center",
        bbox=dict(facecolor="#edf7f1", edgecolor=GREEN_OK, boxstyle="round,pad=0.6", linewidth=1.3),
    )

    ax.set_xlim(-8, total + 90)
    ax.set_ylim(-1.2, 1.2)
    ax.set_yticks([])
    ax.set_xlabel("Wall-clock seconds")
    ax.set_title("Qwen 3.5 397B agent-scale stream (Apr 16) — reasoning phase + content phase",
                 loc="left", pad=16)
    ax.legend(loc="upper right", fontsize=10.5, framealpha=0.9, frameon=True)
    ax.grid(axis="x", alpha=0.4)
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)

    _save(fig, "fig06_sse_chunk_rate.png")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Generating figures for DW benchmark report (v2, presentation-grade)…")
    fig01_cost_per_op()
    fig02_spend_split()
    fig03_smoke_test_timeline()
    fig04_token_composition()
    fig05_scaling_economics()
    fig06_sse_chunk_rate()
    print("Done.")
