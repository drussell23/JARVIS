#!/usr/bin/env python3
"""
Generate figures for the O+V research paper.

Output: PNG files in docs/architecture/figures/ at 300 DPI.

Figures:
    fig01_trinity_architecture.png      — Body/Mind/Soul three-part organism
    fig02_pipeline_flow.png             — 11-phase pipeline flow
    fig03_routing_topology.png          — 5 routes with cost targets
    fig04_iron_gate_stack.png           — Iron Gate hierarchy
    fig05_risk_escalator.png            — 4-tier risk escalator
    fig06_venom_tools.png               — 16 built-in tools + MCP
    fig07_consciousness_layers.png      — Trinity Consciousness layers
    fig08_sensor_funnel.png             — 16 sensors → router
    fig09_dw_3tier.png                  — DW 3-tier event-driven
    fig10_breakthrough_timeline.png     — Session A→W arc
    fig11_six_layer_loop.png            — Complete 6-layer loop
    fig12_functions_not_agents.png      — Phase 0/3 reseating roadmap
"""
from __future__ import annotations

import os
from pathlib import Path

_TMP = os.environ.get("TMPDIR", "/tmp")
os.environ.setdefault("MPLCONFIGDIR", f"{_TMP}/mpl-config")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Patch
import numpy as np

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlepad": 14,
    "axes.labelsize": 11,
    "axes.linewidth": 0.9,
    "axes.edgecolor": "#3c3c3c",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": "#e8e8e8",
    "grid.linewidth": 0.6,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "legend.frameon": False,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.35,
    "savefig.facecolor": "white",
    "figure.facecolor": "white",
})

# Color palette
BLUE = "#2c5aa0"
BLUE_LIGHT = "#8aa8d0"
BLUE_BG = "#e8f0fa"
AMBER = "#c87533"
AMBER_LIGHT = "#e0b890"
AMBER_BG = "#faf0e0"
GREEN = "#2d7a4f"
GREEN_BG = "#e8f4ec"
RED = "#a13838"
RED_BG = "#fae0e0"
PURPLE = "#6a3d9a"
PURPLE_BG = "#f0e8fa"
NEUTRAL = "#3c3c3c"
NEUTRAL_MID = "#7a7a7a"
NEUTRAL_LIGHT = "#d8d8d8"

OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "architecture" / "figures"
OUT.mkdir(parents=True, exist_ok=True)


def _save(fig, name: str) -> None:
    path = OUT / name
    fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white", pad_inches=0.35)
    plt.close(fig)
    print(f"  wrote {path.relative_to(path.parent.parent.parent.parent)}")


def _box(ax, x, y, w, h, text, facecolor, edgecolor, fontsize=11, fontweight="normal",
         textcolor="#1f2328"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02",
                                facecolor=facecolor, edgecolor=edgecolor, linewidth=1.2))
    ax.text(x + w/2, y + h/2, text, ha="center", va="center",
            fontsize=fontsize, fontweight=fontweight, color=textcolor)


def _arrow(ax, xy1, xy2, color=NEUTRAL, lw=1.5, style="-|>"):
    ax.add_patch(FancyArrowPatch(xy1, xy2, arrowstyle=style, mutation_scale=18,
                                 color=color, linewidth=lw))


# ---------------------------------------------------------------------------
# Figure 1 — Trinity architecture
# ---------------------------------------------------------------------------
def fig01_trinity_architecture() -> None:
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")

    # Body
    _box(ax, 0.3, 2.0, 2.8, 3.5, "JARVIS\n(Body)\n\nmacOS integration\nVision, Voice,\nGhost Hands\nO+V pipeline",
         BLUE_BG, BLUE, fontsize=11, fontweight="bold")
    # Mind
    _box(ax, 3.6, 2.0, 2.8, 3.5, "J-Prime\n(Mind)\n\nSelf-hosted inference\nGCP VMs\nCross-session\nreasoning",
         AMBER_BG, AMBER, fontsize=11, fontweight="bold")
    # Soul
    _box(ax, 6.9, 2.0, 2.8, 3.5, "Reactor Core\n(Soul)\n\nSandboxed execution\nIsolated test runs\nResource limits",
         PURPLE_BG, PURPLE, fontsize=11, fontweight="bold")

    # Connectors
    _arrow(ax, (3.1, 3.75), (3.6, 3.75), color=NEUTRAL, lw=2)
    _arrow(ax, (3.6, 3.5), (3.1, 3.5), color=NEUTRAL, lw=2)
    ax.text(3.35, 4.0, "HTTP/WS", ha="center", fontsize=9, color=NEUTRAL_MID, style="italic")

    _arrow(ax, (6.4, 3.75), (6.9, 3.75), color=NEUTRAL, lw=2)
    _arrow(ax, (6.9, 3.5), (6.4, 3.5), color=NEUTRAL, lw=2)
    ax.text(6.65, 4.0, "sandbox", ha="center", fontsize=9, color=NEUTRAL_MID, style="italic")

    # Title block
    ax.text(5, 5.8, "The JARVIS Trinity — tri-partite organism",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(5, 1.5, "O+V lives in the Body, reaches into Mind & Soul\nCommunicates via explicit protocols, never shared memory",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig01_trinity_architecture.png")


# ---------------------------------------------------------------------------
# Figure 2 — 11-phase pipeline (two rows, clearer)
# ---------------------------------------------------------------------------
def fig02_pipeline_flow() -> None:
    fig, ax = plt.subplots(figsize=(11.5, 7))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 7)
    ax.set_aspect("equal")
    ax.axis("off")

    # Row 1 — first half (CLASSIFY through GENERATE)
    row1 = [
        ("CLASSIFY", 0.5),
        ("ROUTE", 2.5),
        ("CONTEXT\nEXPANSION", 4.5),
        ("PLAN", 6.7),
        ("GENERATE", 8.7),
    ]
    y1 = 4.7
    box_w, box_h = 1.7, 1.1
    for (name, x) in row1:
        _box(ax, x, y1, box_w, box_h, name, BLUE_BG, BLUE, fontsize=11, fontweight="bold")
    # Arrows row 1
    for i in range(len(row1) - 1):
        x1 = row1[i][1] + box_w
        x2 = row1[i + 1][1]
        _arrow(ax, (x1, y1 + box_h/2), (x2, y1 + box_h/2), color=NEUTRAL, lw=2)

    # Turn-around arrow (row 1 to row 2)
    ax.add_patch(FancyArrowPatch(
        (row1[-1][1] + box_w/2, y1),
        (8.7 + box_w/2, 3.2),
        arrowstyle="-|>", mutation_scale=20, color=NEUTRAL, lw=2,
        connectionstyle="arc3,rad=-0.25"))

    # Row 2 — second half (VALIDATE through VERIFY) — right-to-left
    row2 = [
        ("VERIFY", 0.5),
        ("APPLY", 2.5),
        ("APPROVE", 4.5),
        ("GATE", 6.7),
        ("VALIDATE", 8.7),
    ]
    y2 = 2.1
    for (name, x) in row2:
        _box(ax, x, y2, box_w, box_h, name, BLUE_BG, BLUE, fontsize=11, fontweight="bold")
    # Arrows row 2 (right to left)
    for i in range(len(row2) - 1, 0, -1):
        x1 = row2[i][1]
        x2 = row2[i - 1][1] + box_w
        _arrow(ax, (x1, y2 + box_h/2), (x2, y2 + box_h/2), color=NEUTRAL, lw=2)

    # COMPLETE at end of row 2
    _box(ax, 0.5, 0.4, box_w, box_h, "COMPLETE", GREEN_BG, GREEN, fontsize=11, fontweight="bold")
    _arrow(ax, (0.5 + box_w/2, y2), (0.5 + box_w/2, 0.4 + box_h), color=GREEN, lw=2)

    # Title
    ax.text(5.75, 6.6, "Ouroboros Pipeline — Eleven Phases",
            ha="center", fontsize=15, fontweight="bold", color=NEUTRAL)

    _save(fig, "fig02_pipeline_flow.png")


# ---------------------------------------------------------------------------
# Figure 3 — Routing topology (5 routes)
# ---------------------------------------------------------------------------
def fig03_routing_topology() -> None:
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")

    routes = [
        ("IMMEDIATE", "Claude direct", "$0.03", "Test failures, voice, health", RED_BG, RED),
        ("STANDARD", "DW → Claude fallback", "$0.005", "Default cascade", BLUE_BG, BLUE),
        ("COMPLEX", "Claude plans → DW executes", "$0.015", "Multi-file architectural", PURPLE_BG, PURPLE),
        ("BACKGROUND", "DW only, no fallback", "$0.002", "Mining, TODOs, doc staleness", GREEN_BG, GREEN),
        ("SPECULATIVE", "DW batch fire-and-forget", "$0.001", "Intent discovery, dream", AMBER_BG, AMBER),
    ]

    y_start = 4.8
    row_h = 0.85
    for i, (name, strategy, cost, when, bg, fg) in enumerate(routes):
        y = y_start - i * (row_h + 0.1)
        _box(ax, 0.3, y, 2.0, row_h, name, bg, fg, fontsize=11, fontweight="bold")
        _box(ax, 2.5, y, 3.0, row_h, strategy, "#ffffff", NEUTRAL_LIGHT, fontsize=10)
        _box(ax, 5.7, y, 1.0, row_h, cost, "#ffffff", NEUTRAL_LIGHT, fontsize=11, fontweight="bold")
        _box(ax, 6.9, y, 2.9, row_h, when, "#ffffff", NEUTRAL_LIGHT, fontsize=10)

    # Headers
    ax.text(1.3, 5.85, "Route", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    ax.text(4.0, 5.85, "Provider Strategy", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    ax.text(6.2, 5.85, "Cost", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    ax.text(8.35, 5.85, "When", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)

    ax.text(5, 0.4, "UrgencyRouter: pure-code lookup, zero LLM calls, <1ms latency",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig03_routing_topology.png")


# ---------------------------------------------------------------------------
# Figure 4 — Iron Gate stack
# ---------------------------------------------------------------------------
def fig04_iron_gate_stack() -> None:
    fig, ax = plt.subplots(figsize=(10.5, 7))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 7)
    ax.set_aspect("equal")
    ax.axis("off")

    gates = [
        "1. Path containment",
        "2. Protected paths (.env, credentials, .git/)",
        "3. Command blocklist (rm -rf, curl | sh)",
        "4. Exploration-first (min tool calls)",
        "5. ASCII strictness (no Unicode corruption)",
        "6. Multi-file coverage",
        "7. Cost ceilings (per-op, daily budget)",
        "8. Approval timeouts",
        "9. Worker pool ceilings",
        "10. Webhook signature verification",
        "11. Stale exploration guard (file hashes)",
        "12. File lock TTL",
    ]

    y0 = 6.3
    dy = 0.42
    for i, g in enumerate(gates):
        y = y0 - i * dy
        _box(ax, 1.2, y, 7.6, 0.36, g, BLUE_BG, BLUE, fontsize=10.5)

    # Title
    ax.text(5, 6.85, "The Iron Gate — Twelve Deterministic Safety Rules",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(5, 0.6, "Pre-linguistic. Model-uninfluenced. Deterministic. Structural. Auditable.\nEnables default-allow tools because containment is structural, not configurational.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig04_iron_gate_stack.png")


# ---------------------------------------------------------------------------
# Figure 5 — Risk escalator
# ---------------------------------------------------------------------------
def fig05_risk_escalator() -> None:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 5.5)
    ax.set_aspect("equal")
    ax.axis("off")

    tiers = [
        ("GREEN\nSAFE_AUTO", "Auto-apply silently", "Single-file, non-core", "#d7ecdc", GREEN),
        ("YELLOW\nNOTIFY_APPLY", "Auto-apply visibly (5s diff preview)", "New files, multi-file, core orchestration", "#fff0d4", AMBER_ACCENT := "#b08800"),
        ("ORANGE\nAPPROVAL_REQUIRED", "Block; wait for human Y/N", "Security-sensitive, breaking API", "#fce5d0", AMBER),
        ("RED\nBLOCKED", "Rejected; short-circuited at CLASSIFY", "Supervisor, credentials, governance core", "#fad5d5", RED),
    ]

    y0 = 4.3
    dy = 0.95
    for i, (name, action, triggers, bg, fg) in enumerate(tiers):
        y = y0 - i * dy
        _box(ax, 0.3, y, 2.2, 0.78, name, bg, fg, fontsize=11, fontweight="bold")
        _box(ax, 2.7, y, 3.3, 0.78, action, "#ffffff", NEUTRAL_LIGHT, fontsize=10)
        _box(ax, 6.2, y, 3.5, 0.78, triggers, "#ffffff", NEUTRAL_LIGHT, fontsize=10)

    ax.text(1.4, 5.05, "Risk Tier", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    ax.text(4.35, 5.05, "Pipeline Action", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    ax.text(7.95, 5.05, "Example Triggers", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)

    ax.text(5, 0.2, "Consciousness-driven escalation: high regression risk → tier elevated by one step",
            ha="center", fontsize=9, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig05_risk_escalator.png")


# ---------------------------------------------------------------------------
# Figure 6 — Venom tool ecosystem (cleaner 2-row layout with MCP as sidebar)
# ---------------------------------------------------------------------------
def fig06_venom_tools() -> None:
    fig, ax = plt.subplots(figsize=(12, 7.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7.5)
    ax.set_aspect("equal")
    ax.axis("off")

    # Larger, fewer-columns layout: 2 rows × 4 columns, each box ~2.3 wide × 2.0 tall
    categories = [
        ("Comprehension", ["read_file", "search_code", "list_symbols"], BLUE_BG, BLUE),
        ("Discovery", ["glob_files", "list_dir"], GREEN_BG, GREEN),
        ("Call Graph", ["get_callers"], PURPLE_BG, PURPLE),
        ("History", ["git_log", "git_diff", "git_blame"], AMBER_BG, AMBER),
        ("Mutation", ["edit_file", "write_file", "delete_file"], RED_BG, RED),
        ("Execution", ["bash", "run_tests"], "#f0e0e8", "#8a3a6a"),
        ("Web", ["web_fetch", "web_search"], "#e0f0f0", "#3a7a7a"),
        ("Human", ["ask_human"], "#f8e0e0", "#7a3a3a"),
    ]

    box_w = 2.3
    box_h = 2.0
    x_positions = [0.3, 2.75, 5.2, 7.65]
    for idx, (cat, tools, bg, fg) in enumerate(categories):
        row = idx // 4
        col = idx % 4
        x = x_positions[col]
        y = 4.6 - row * 2.3
        _box(ax, x, y, box_w, box_h, "", bg, fg)
        ax.text(x + box_w/2, y + box_h - 0.3, cat, ha="center",
                fontsize=12, fontweight="bold", color=fg)
        for j, t in enumerate(tools):
            ax.text(x + box_w/2, y + box_h - 0.75 - j * 0.38, t,
                    ha="center", fontsize=10, color=NEUTRAL, family="monospace")

    # MCP External — standalone wide box at bottom
    _box(ax, 0.3, 0.5, 11.4, 1.2, "", "#eee4f5", "#5a3a8a")
    ax.text(0.6, 1.35, "MCP External Tools",
            ha="left", fontsize=12, fontweight="bold", color="#5a3a8a")
    ax.text(0.6, 1.0, "Discovered dynamically from connected servers at prompt-construction time.",
            ha="left", fontsize=10, color=NEUTRAL)
    ax.text(0.6, 0.7, "Naming convention: mcp_{server}_{tool}  •  Policy rule 0b: auto-allowed  •  Transport: stdio or SSE with signature verification",
            ha="left", fontsize=10, color=NEUTRAL, family="monospace")

    ax.text(6, 7.1, "Venom Tool Ecosystem — 16 built-in tools + dynamic MCP",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)

    _save(fig, "fig06_venom_tools.png")


# ---------------------------------------------------------------------------
# Figure 7 — Trinity Consciousness layers
# ---------------------------------------------------------------------------
def fig07_consciousness_layers() -> None:
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7)
    ax.set_aspect("equal")
    ax.axis("off")

    # Core engines
    engines = [
        ("HealthCortex\n(30s polling)", 0.6, 5.2),
        ("MemoryEngine\n(168h TTL)", 3.4, 5.2),
        ("DreamEngine\n(idle blueprints)", 6.2, 5.2),
        ("ProphecyEngine\n(regression pred.)", 9.0, 5.2),
    ]
    for (name, x, y) in engines:
        _box(ax, x, y, 2.4, 1.2, name, BLUE_BG, BLUE, fontsize=10.5, fontweight="bold")

    # Awareness fusion
    fusion = [
        ("CAI\nContextual", 1.4, 3.3),
        ("SAI\nSituational", 5.0, 3.3),
        ("UAE\nUnified", 8.6, 3.3),
    ]
    for (name, x, y) in fusion:
        _box(ax, x, y, 2.2, 1.0, name, PURPLE_BG, PURPLE, fontsize=10.5, fontweight="bold")

    # Integration
    _box(ax, 2.0, 1.4, 3.4, 1.0, "ConsciousnessBridge\n(5 methods → pipeline)", AMBER_BG, AMBER,
         fontsize=10, fontweight="bold")
    _box(ax, 6.2, 1.4, 3.4, 1.0, "GoalMemoryBridge\n(ChromaDB cross-session)", AMBER_BG, AMBER,
         fontsize=10, fontweight="bold")

    # Title
    ax.text(6, 6.65, "Trinity Consciousness — Zone 6.11", ha="center",
            fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(6, 0.7, "Four core engines + three awareness-fusion layers + two integration bridges\nAuthority-bounded: advisory-only; cannot override Iron Gate or safety-critical decisions",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig07_consciousness_layers.png")


# ---------------------------------------------------------------------------
# Figure 8 — Sensor funnel (grouped by category, cleaner convergence)
# ---------------------------------------------------------------------------
def fig08_sensor_funnel() -> None:
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7)
    ax.set_aspect("equal")
    ax.axis("off")

    # Group sensors into 4 logical category boxes instead of 16 individual arrows
    groups = [
        ("File / Code events", [
            "TestFailureSensor", "OpportunityMinerSensor",
            "DocStalenessSensor", "TodoScannerSensor", "CrossRepoDriftSensor",
        ], BLUE_BG, BLUE, 5.8),
        ("Health & Performance", [
            "RuntimeHealthSensor", "PerformanceRegressionSensor",
            "CapabilityGapSensor",
        ], GREEN_BG, GREEN, 4.2),
        ("External triggers", [
            "VoiceCommandSensor", "GitHubIssueSensor",
            "WebIntelligenceSensor", "CUExecutionSensor",
        ], PURPLE_BG, PURPLE, 2.6),
        ("Scheduled / Proactive", [
            "ScheduledTriggerSensor", "BacklogSensor",
            "ProactiveExplorationSensor", "IntentDiscoverySensor",
        ], AMBER_BG, AMBER, 1.0),
    ]

    group_w = 4.0
    group_h = 1.35
    for (name, sensors, bg, fg, y) in groups:
        _box(ax, 0.3, y, group_w, group_h, "", bg, fg)
        ax.text(0.5, y + group_h - 0.3, name, ha="left",
                fontsize=11, fontweight="bold", color=fg)
        sensor_text = "  •  ".join(sensors)
        # Wrap long sensor lines
        if len(sensor_text) > 55:
            half = len(sensors) // 2
            line1 = "  •  ".join(sensors[:half + (len(sensors) % 2)])
            line2 = "  •  ".join(sensors[half + (len(sensors) % 2):])
            ax.text(0.5, y + 0.55, line1, ha="left", fontsize=9, color=NEUTRAL, family="monospace")
            ax.text(0.5, y + 0.22, line2, ha="left", fontsize=9, color=NEUTRAL, family="monospace")
        else:
            ax.text(0.5, y + 0.35, sensor_text, ha="left", fontsize=9, color=NEUTRAL, family="monospace")
        # Arrow from group to router
        ax.annotate("", xy=(6.2, 3.85), xytext=(4.35, y + group_h/2),
                    arrowprops=dict(arrowstyle="->", color=fg, lw=1.5, alpha=0.7))

    # Router (centered)
    _box(ax, 6.2, 2.8, 3.6, 2.0,
         "UnifiedIntakeRouter\n\n• deduplication\n• file-lock DAG\n• priority queue\n• coalescing (30s window)\n• WAL persistence",
         AMBER_BG, AMBER, fontsize=10.5)

    # Router → Pipeline
    _arrow(ax, (9.8, 3.8), (11.1, 3.8), color=NEUTRAL, lw=2)
    _box(ax, 11.1, 3.1, 0.9, 1.5, "Pipeline", GREEN_BG, GREEN, fontsize=10, fontweight="bold")

    # Title + footer
    ax.text(6, 6.6, "Intake Layer — 16 Sensors, 4 Groups → Router → Pipeline",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(6, 0.35, "Event-driven. Four-layer storm protection (debounce, SHA dedup, per-sensor gating, envelope dedup_key).",
            ha="center", fontsize=9.5, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig08_sensor_funnel.png")


# ---------------------------------------------------------------------------
# Figure 9 — DW 3-tier event-driven
# ---------------------------------------------------------------------------
def fig09_dw_3tier() -> None:
    fig, ax = plt.subplots(figsize=(11.5, 5.5))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 5.5)
    ax.set_aspect("equal")
    ax.axis("off")

    tiers = [
        ("Tier 0\nReal-Time SSE", "Primary path\n/v1/chat/completions\nstream=true\nZero polling", GREEN_BG, GREEN),
        ("Tier 1\nWebhook Batch", "Async batch\nBatchFutureRegistry\nHMAC-signed callbacks\nZero polling", BLUE_BG, BLUE),
        ("Tier 2\nAdaptive Poll", "Safety net only\nStart 2s → cap 30s\nExponential + jitter\n(NOT fixed interval)", AMBER_BG, AMBER),
    ]
    x0 = 0.5
    for i, (name, body, bg, fg) in enumerate(tiers):
        x = x0 + i * 3.7
        _box(ax, x, 1.4, 3.2, 2.8, name + "\n\n" + body, bg, fg, fontsize=10, fontweight="normal")
        # Title inside box
        ax.text(x + 1.6, 3.75, name.split("\n")[1], ha="center", fontsize=11, fontweight="bold", color=fg)

    # Falling arrows between tiers
    _arrow(ax, (3.75, 2.8), (4.1, 2.8), color=NEUTRAL, lw=1.5)
    _arrow(ax, (7.45, 2.8), (7.8, 2.8), color=NEUTRAL, lw=1.5)

    ax.text(5.75, 5.05, "DoubleWord 3-Tier Event-Driven Architecture",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(5.75, 0.5, "Manifesto §3 — Zero polling. Pure reflex.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig09_dw_3tier.png")


# ---------------------------------------------------------------------------
# Figure 10 — Breakthrough timeline (arc-grouped, clearer spacing)
# ---------------------------------------------------------------------------
def fig10_breakthrough_timeline() -> None:
    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.set_xlim(-0.5, 13)
    ax.set_ylim(-0.5, 6.5)
    ax.axis("off")

    # Group sessions into four color-coded arcs for clarity
    arcs = [
        ("Exploration arc (A–G)", BLUE, 0.5, 3.2, [
            ("A", 0.9, "Shadow\nmode"),
            ("B", 1.6, "Enforcement\non"),
            ("C", 2.3, "Instrumentation\nproof"),
            ("G", 3.0, "Full adaptation\nloop score=25.5"),
        ]),
        ("First APPLY (H–O)", AMBER, 3.8, 6.5, [
            ("H–N", 4.6, "8-session\nunmasking ladder"),
            ("O", 6.1, "First APPLY\n(1 of 4 files)"),
        ]),
        ("Multi-file enforcement (Q–S)", PURPLE, 7.1, 8.3, [
            ("Q–S", 7.7, "Parser fix +\nIron Gate 5"),
        ]),
        ("First multi-file APPLY (T–W)", GREEN, 8.9, 12.2, [
            ("T", 9.3, "Follow-up A\nfalsified"),
            ("U", 10.0, "FSM trail kills\n'silent exit'"),
            ("V", 10.8, "L2 budget\ncontract bug"),
            ("W", 11.7, "First multi-file\nAPPLY 20/20"),
        ]),
    ]

    # Draw arc background bands
    for (arc_name, arc_color, x1, x2, points) in arcs:
        ax.add_patch(FancyBboxPatch((x1, 2.2), x2 - x1, 1.6,
                                    boxstyle="round,pad=0.04",
                                    facecolor=arc_color, alpha=0.09,
                                    edgecolor=arc_color, linewidth=1.2))
        ax.text((x1 + x2) / 2, 4.0, arc_name, ha="center", fontsize=10.5,
                fontweight="bold", color=arc_color)

    # Timeline line
    ax.plot([0, 12.5], [3.0, 3.0], color=NEUTRAL_MID, lw=2.0, zorder=1)

    # Plot points
    for (arc_name, arc_color, x1, x2, points) in arcs:
        for (label, x, descr) in points:
            # Highlight breakthrough points (O and W)
            is_breakthrough = label in ["O", "W"]
            size = 500 if is_breakthrough else 300
            ax.scatter([x], [3.0], s=size, c=[arc_color],
                       edgecolors=NEUTRAL, linewidths=1.5, zorder=3)
            ax.text(x, 3.0, label, ha="center", va="center",
                    fontsize=10 if is_breakthrough else 9,
                    fontweight="bold", zorder=4,
                    color="white" if is_breakthrough else NEUTRAL)
            # Description below timeline
            ax.text(x, 2.3, descr, ha="center", va="top", fontsize=9, color=NEUTRAL)

    # Title + footer
    ax.text(6.25, 5.8, "Battle-Test Breakthrough Arc — Sessions A through W",
            ha="center", fontsize=15, fontweight="bold", color=NEUTRAL)
    ax.text(6.25, 5.3, "2026-04-15, ~11 hours of continuous battle-testing",
            ha="center", fontsize=11, color=NEUTRAL_MID, style="italic")
    ax.text(6.25, 0.3,
            "Sessions O (first APPLY, 1 of 4 files) and W (first multi-file APPLY, 20/20 pytest green) are the headline outcomes.",
            ha="center", fontsize=9.5, color=NEUTRAL_MID, style="italic")

    _save(fig, "fig10_breakthrough_timeline.png")


# ---------------------------------------------------------------------------
# Figure 11 — Six-layer loop
# ---------------------------------------------------------------------------
def fig11_six_layer_loop() -> None:
    fig, ax = plt.subplots(figsize=(10.5, 9.5))
    ax.set_xlim(0, 10.5)
    ax.set_ylim(0, 9.5)
    ax.set_aspect("equal")
    ax.axis("off")

    layers = [
        ("1. Strategic Direction", "compass — WHERE are we going", "Manifesto, 7 principles", PURPLE_BG, PURPLE, 8.0),
        ("2. Trinity Consciousness", "soul — WHY evolve", "MemoryEngine, ProphecyEngine, CAI/SAI/UAE", BLUE_BG, BLUE, 6.7),
        ("3. Event Spine", "senses — WHEN to act", "16 sensors, watchdog, pytest plugin", GREEN_BG, GREEN, 5.4),
        ("4. Ouroboros Pipeline", "skeleton — WHAT, safely", "11-phase FSM, Iron Gate", AMBER_BG, AMBER, 4.1),
        ("5. Venom Agentic Loop", "nervous system — HOW", "16 tools + MCP, L2 Repair", "#fae0e0", RED, 2.8),
        ("6. ChangeEngine + AutoCommitter", "action + record", "Disk writes, commit signature", "#e8e8e8", NEUTRAL, 1.5),
    ]

    for (name, subtitle, body, bg, fg, y) in layers:
        _box(ax, 1.0, y, 8.5, 1.05, "", bg, fg)
        ax.text(1.3, y + 0.75, name, ha="left", fontsize=11, fontweight="bold", color=fg)
        ax.text(1.3, y + 0.38, subtitle, ha="left", fontsize=9.5, color=fg, style="italic")
        ax.text(1.3, y + 0.1, body, ha="left", fontsize=9.5, color=NEUTRAL)

    # Down arrows between layers
    for i in range(5):
        y = layers[i][5] - 0.05
        _arrow(ax, (5.25, y), (5.25, y - 0.25), color=NEUTRAL, lw=1.5)

    # Feedback arrow (right-side curved-ish)
    ax.annotate("", xy=(9.35, 7.5), xytext=(9.35, 1.8),
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=2,
                                connectionstyle="arc3,rad=0.3"))
    ax.text(10.0, 4.5, "feedback:\noutcome →\nMemoryEngine →\nnext op benefits",
            ha="left", fontsize=9, color=GREEN, style="italic")

    ax.text(5, 9.2, "The Complete Six-Layer Loop", ha="center",
            fontsize=14, fontweight="bold", color=NEUTRAL)

    _save(fig, "fig11_six_layer_loop.png")


# ---------------------------------------------------------------------------
# Figure 12 — Functions Not Agents
# ---------------------------------------------------------------------------
def fig12_functions_not_agents() -> None:
    fig, ax = plt.subplots(figsize=(11.5, 6.5))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 6.5)
    ax.set_aspect("equal")
    ax.axis("off")

    # Phase 0
    _box(ax, 0.3, 3.3, 4.8, 2.3,
         "Phase 0 — SHIPPING (shadow)\n\nGemma Compaction Caller\nContextCompactor._build_summary()\nNon-streaming complete_sync()\n<1KB bounded output\n\nMaster: JARVIS_COMPACTION_CALLER_ENABLED",
         GREEN_BG, GREEN, fontsize=10)

    # Phase 3
    _box(ax, 6.4, 3.3, 4.8, 2.3,
         "Phase 3 — PLANNED\n\nQwen 397B Heavy Analyst\n- BlastRadius scoring (10s)\n- Episodic failure clustering (30s)\n- Deep analysis sensor\nAll non-streaming complete_sync()",
         BLUE_BG, BLUE, fontsize=10)

    _arrow(ax, (5.2, 4.45), (6.3, 4.45), color=NEUTRAL, lw=2)
    ax.text(5.75, 4.75, "24h clean\nshadow telemetry", ha="center", fontsize=9, color=NEUTRAL_MID, style="italic")

    # Design invariants
    invariants = [
        "1. Non-streaming only (stream=false)",
        "2. Short structured output (<512 tokens)",
        "3. Caller-supplied timeout",
        "4. Anti-hallucination gate",
        "5. Circuit breaker",
        "6. Shadow mode first",
    ]
    _box(ax, 1.5, 0.4, 8.5, 2.3, "", "#f8f8f8", NEUTRAL_LIGHT)
    ax.text(5.75, 2.4, "Six Design Invariants (every DW caller)", ha="center", fontsize=11, fontweight="bold", color=NEUTRAL)
    for i, inv in enumerate(invariants):
        row = i // 3
        col = i % 3
        ax.text(2.0 + col * 2.7, 1.8 - row * 0.55, inv, fontsize=9.5, color=NEUTRAL)

    ax.text(5.75, 6.1, "Functions, Not Agents — DoubleWord Reseating Roadmap",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)

    _save(fig, "fig12_functions_not_agents.png")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Generating figures for O+V research paper…")
    fig01_trinity_architecture()
    fig02_pipeline_flow()
    fig03_routing_topology()
    fig04_iron_gate_stack()
    fig05_risk_escalator()
    fig06_venom_tools()
    fig07_consciousness_layers()
    fig08_sensor_funnel()
    fig09_dw_3tier()
    fig10_breakthrough_timeline()
    fig11_six_layer_loop()
    fig12_functions_not_agents()
    print("Done.")
