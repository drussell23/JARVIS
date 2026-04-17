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
# Figure 2 — 11-phase pipeline
# ---------------------------------------------------------------------------
def fig02_pipeline_flow() -> None:
    fig, ax = plt.subplots(figsize=(13, 5.5))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")

    phases = [
        ("CLASSIFY", 0.3),
        ("ROUTE", 1.7),
        ("CONTEXT\nEXPANSION", 3.1),
        ("PLAN", 4.7),
        ("GENERATE", 6.0),
        ("VALIDATE", 7.5),
        ("GATE", 9.0),
        ("APPROVE", 10.3),
        ("APPLY", 11.7),
        ("VERIFY", 13.0),
    ]
    y = 3.8
    for (name, x) in phases:
        _box(ax, x, y, 1.2, 0.95, name, BLUE_BG, BLUE, fontsize=10, fontweight="bold")

    # Arrows between phases
    for i in range(len(phases) - 1):
        x1 = phases[i][1] + 1.2
        x2 = phases[i + 1][1]
        _arrow(ax, (x1, y + 0.47), (x2, y + 0.47), color=NEUTRAL, lw=1.5)

    # COMPLETE at end
    _box(ax, 13.3, 3.8, 0.7, 0.95, "✓\nCOMPLETE", GREEN_BG, GREEN, fontsize=9, fontweight="bold")
    _arrow(ax, (14.2, y + 0.47), (14.2, y + 0.47), color=GREEN, lw=1.5)

    # POSTMORTEM below (failure branch)
    _box(ax, 6.5, 1.8, 2.0, 0.95, "✗\nPOSTMORTEM\n(any failure)", RED_BG, RED, fontsize=9, fontweight="bold")
    _arrow(ax, (7.5, 3.5), (7.5, 2.8), color=RED, lw=1.3, style="-|>")

    # Title
    ax.text(7, 5.4, "Ouroboros Pipeline — 11 Phases", ha="center",
            fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(7, 0.8, "Every operation traverses the same phases. Every phase transition is logged.\nEvery unhandled exception routes to POSTMORTEM. Every retry is bounded.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

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
# Figure 6 — Venom tool ecosystem
# ---------------------------------------------------------------------------
def fig06_venom_tools() -> None:
    fig, ax = plt.subplots(figsize=(12, 6.5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.5)
    ax.set_aspect("equal")
    ax.axis("off")

    categories = [
        ("Comprehension", ["read_file", "search_code", "list_symbols"], BLUE_BG, BLUE, 0.3, 4.8),
        ("Discovery", ["glob_files", "list_dir"], GREEN_BG, GREEN, 3.5, 4.8),
        ("Call Graph", ["get_callers"], PURPLE_BG, PURPLE, 5.9, 4.8),
        ("History", ["git_log", "git_diff", "git_blame"], AMBER_BG, AMBER, 8.0, 4.8),
        ("Mutation", ["edit_file", "write_file", "delete_file"], RED_BG, RED, 0.3, 2.3),
        ("Execution", ["bash", "run_tests"], "#f0e0e8", "#8a3a6a", 3.5, 2.3),
        ("Web", ["web_fetch", "web_search"], "#e0f0f0", "#3a7a7a", 5.9, 2.3),
        ("Human", ["ask_human"], "#f8e0e0", "#7a3a3a", 8.0, 2.3),
    ]

    for (cat, tools, bg, fg, x, y) in categories:
        h = 0.5 + len(tools) * 0.38
        _box(ax, x, y - h + 0.5, 2.4, h, "", bg, fg)
        ax.text(x + 1.2, y + 0.25, cat, ha="center", fontsize=11, fontweight="bold", color=fg)
        for j, t in enumerate(tools):
            ax.text(x + 1.2, y - 0.1 - j * 0.32, t, ha="center", fontsize=10, color=NEUTRAL,
                    family="monospace")

    # MCP External
    _box(ax, 10.2, 2.3, 1.5, 3.0, "MCP\nExternal\ntools\n\ndiscovered\nat prompt time\n\nmcp_*_*\nauto-allowed",
         "#eee4f5", "#5a3a8a", fontsize=9)

    ax.text(6, 6.05, "Venom Tool Ecosystem — 16 built-in + dynamic MCP",
            ha="center", fontsize=14, fontweight="bold", color=NEUTRAL)
    ax.text(6, 0.8, "All default-allowed. Iron Gate provides structural containment per tool call.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

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
# Figure 8 — Sensor funnel
# ---------------------------------------------------------------------------
def fig08_sensor_funnel() -> None:
    fig, ax = plt.subplots(figsize=(11.5, 7.5))
    ax.set_xlim(0, 11.5)
    ax.set_ylim(0, 7.5)
    ax.set_aspect("equal")
    ax.axis("off")

    # Sensors column (2 columns of 8)
    sensors_col1 = [
        "TestFailureSensor",
        "VoiceCommandSensor",
        "OpportunityMinerSensor",
        "CapabilityGapSensor",
        "ScheduledTriggerSensor",
        "BacklogSensor",
        "RuntimeHealthSensor",
        "WebIntelligenceSensor",
    ]
    sensors_col2 = [
        "PerformanceRegressionSensor",
        "DocStalenessSensor",
        "GitHubIssueSensor",
        "ProactiveExplorationSensor",
        "CrossRepoDriftSensor",
        "TodoScannerSensor",
        "CUExecutionSensor",
        "IntentDiscoverySensor",
    ]

    y0 = 6.5
    dy = 0.55
    for i, s in enumerate(sensors_col1):
        _box(ax, 0.3, y0 - i * dy, 2.8, 0.48, s, BLUE_BG, BLUE, fontsize=9.5)
    for i, s in enumerate(sensors_col2):
        _box(ax, 3.2, y0 - i * dy, 2.8, 0.48, s, BLUE_BG, BLUE, fontsize=9.5)

    # Funnel arrows to router
    for i in range(8):
        y = y0 - i * dy + 0.24
        ax.annotate("", xy=(6.5, 3.7), xytext=(5.95, y),
                    arrowprops=dict(arrowstyle="->", color=NEUTRAL_MID, lw=0.7))

    # Router
    _box(ax, 6.5, 2.8, 2.8, 1.6, "UnifiedIntakeRouter\n\n- dedup\n- file-lock DAG\n- priority queue\n- coalescing",
         AMBER_BG, AMBER, fontsize=10, fontweight="bold")

    _arrow(ax, (9.3, 3.6), (10.4, 3.6), color=NEUTRAL, lw=1.5)
    _box(ax, 10.4, 3.0, 1.0, 1.2, "Pipeline", GREEN_BG, GREEN, fontsize=10, fontweight="bold")

    # Title + footer
    ax.text(5.7, 7.2, "Intake Layer — 16 Sensors → Unified Router → Pipeline",
            ha="center", fontsize=13, fontweight="bold", color=NEUTRAL)
    ax.text(5.7, 1.3, "Event-driven. Four-layer storm protection. WAL persistence for crash recovery.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

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
# Figure 10 — Breakthrough timeline
# ---------------------------------------------------------------------------
def fig10_breakthrough_timeline() -> None:
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.set_xlim(-0.5, 12)
    ax.set_ylim(-0.5, 5)
    ax.axis("off")

    sessions = [
        ("A", 0.3, "ExplorationLedger\nshadow mode", BLUE_LIGHT),
        ("B", 1.2, "Enforcement\nturned on", BLUE_LIGHT),
        ("C", 2.1, "Instrumentation\nproof", BLUE_LIGHT),
        ("G", 3.0, "Full adaptation\nloop (score=25.5)", BLUE),
        ("H-N", 4.2, "8-session\nunmasking ladder", AMBER_LIGHT),
        ("O", 5.6, "First APPLY\nto disk (1/4 files)", AMBER),
        ("Q-S", 6.8, "Multi-file\nenforcement gates", BLUE_LIGHT),
        ("T", 7.9, "Follow-up A\nhypothesis falsified", AMBER_LIGHT),
        ("U", 8.8, "FSM trail kills\n'silent exit'", BLUE_LIGHT),
        ("V", 9.7, "L2 budget\ncontract bug found", AMBER_LIGHT),
        ("W", 10.8, "First multi-file\nAPPLY to disk\n20/20 pytest", GREEN),
    ]

    # Timeline line
    ax.plot([0, 11.5], [2.5, 2.5], color=NEUTRAL_MID, lw=2.5, zorder=1)

    for (label, x, descr, color) in sessions:
        # Circle
        ax.scatter([x], [2.5], s=200, c=[color], edgecolors=NEUTRAL, linewidths=1.5, zorder=3)
        ax.text(x, 2.5, label, ha="center", va="center", fontsize=8.5, fontweight="bold", zorder=4)
        # Label above or below
        y_label = 3.4 if label in ["A", "C", "G", "O", "U", "W"] else 1.6
        va = "bottom" if y_label > 2.5 else "top"
        ax.text(x, y_label, descr, ha="center", va=va, fontsize=8.5, color=NEUTRAL)

    ax.text(5.75, 4.5, "Battle Test Breakthrough Arc — Sessions A through W (2026-04-15)",
            ha="center", fontsize=13, fontweight="bold", color=NEUTRAL)
    ax.text(5.75, 0.3, "Each session revealed a distinct failure mode. Session W is the first end-to-end multi-file APPLY to disk.",
            ha="center", fontsize=10, color=NEUTRAL_MID, style="italic")

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
