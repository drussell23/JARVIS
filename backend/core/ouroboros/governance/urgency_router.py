"""
UrgencyRouter — Deterministic provider routing based on signal urgency,
source, and task complexity.

Manifesto §5: "Intelligence-Driven Routing"
    Tier 0 (Deterministic Fast-Path): If intent confidence is >0.95 and
    routing is unambiguous, the system routes directly via code.
    No model call. Nanosecond latency.

This module IS that Tier 0 routing layer. Pure code, zero LLM calls.
Maps (urgency, source, complexity) → ProviderRoute in <1ms.

Routes:
    IMMEDIATE   → Claude direct (skip DW). Fast reflex for critical ops.
    STANDARD    → DW 397B primary, Claude fallback. Default cascade.
    COMPLEX     → Claude plans (extended thinking), DW executes (cheap).
    BACKGROUND  → DW only. No Claude fallback, no deadline pressure.
    SPECULATIVE → DW batch fire-and-forget. Pre-computation for idle time.

Cost impact:
    IMMEDIATE:   ~$0.03/op (Claude only — speed over cost)
    STANDARD:    ~$0.005/op (DW primary — 87% savings vs Claude-only)
    COMPLEX:     ~$0.015/op (Claude plan $0.01 + DW gen $0.005 — 85% savings)
    BACKGROUND:  ~$0.002/op (DW batch only — 95% savings)
    SPECULATIVE: ~$0.001/op (DW batch, tolerate high discard rate)
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Dict, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ProviderRoute enum
# ---------------------------------------------------------------------------


class ProviderRoute(str, Enum):
    """Provider routing strategy determined at ROUTE phase.

    Each route defines a contract between the orchestrator and the
    CandidateGenerator about which providers to use and how to cascade.
    """

    IMMEDIATE = "immediate"
    """Claude direct, skip DW. Fast reflex for critical/urgent ops.
    Used for: test failures, voice commands, runtime health critical,
    security issues. Latency-sensitive — every second counts."""

    STANDARD = "standard"
    """DW 397B primary, Claude fallback. Default 2-tier cascade.
    Used for: normal-priority operations with moderate complexity.
    DW gets full budget; Claude as safety net."""

    COMPLEX = "complex"
    """Claude plans (extended thinking), DW executes plan (cheap hands).
    Used for: heavy_code, multi-file architectural changes.
    Leverages Claude's reasoning + DW's cheap token output."""

    BACKGROUND = "background"
    """DW only. No Claude fallback, no deadline pressure.
    Used for: opportunity mining, doc staleness, TODO scanning.
    Cost-optimized — accept DW failure rather than waste Claude tokens."""

    SPECULATIVE = "speculative"
    """DW batch fire-and-forget. Pre-computation for idle time.
    Used for: intent discovery, dream engine, proactive exploration.
    Results cached; applied opportunistically if still relevant."""


# ---------------------------------------------------------------------------
# Source → urgency affinity tables (deterministic, no LLM calls)
# ---------------------------------------------------------------------------

# Sources that always produce IMMEDIATE-eligible signals when urgent.
# Keep this set TIGHT — adding a source here means every high-urgency
# signal from that sensor will skip DW and go straight to Claude, which
# is the exact regression that burned $0.53 in bt-2026-04-13-011909 when
# seven sensors were copy-paste labeling themselves as `runtime_health`.
_IMMEDIATE_SOURCES = frozenset({
    "test_failure",
    "voice_human",
    "runtime_health",
})

# Sources that produce BACKGROUND-eligible signals.
# Per CLAUDE.md §"Urgency-Aware Provider Routing": "BACKGROUND route —
# DW only, no Claude fallback. When: OpportunityMiner, DocStaleness,
# TODOs, backlog." Any sensor whose work is cost-optimization-first
# belongs here so its ops stay off the Claude tier entirely.
_BACKGROUND_SOURCES = frozenset({
    "ai_miner",
    "exploration",
    "backlog",
    "architecture",
    "todo_scanner",
    "doc_staleness",
})

# Sources that produce SPECULATIVE-eligible signals
_SPECULATIVE_SOURCES = frozenset({
    "intent_discovery",
})

# Complexity levels that qualify for COMPLEX routing
_COMPLEX_COMPLEXITIES = frozenset({
    "heavy_code",
    "complex",
})

# Urgency levels that qualify for IMMEDIATE routing
_IMMEDIATE_URGENCIES = frozenset({
    "critical",
})

# Urgency levels that qualify for BACKGROUND routing
_BACKGROUND_URGENCIES = frozenset({
    "low",
})


# ---------------------------------------------------------------------------
# UrgencyRouter — the deterministic classifier
# ---------------------------------------------------------------------------


class UrgencyRouter:
    """Deterministic provider routing based on signal metadata.

    This is Manifesto §5 Tier 0: pure code routing, no LLM calls.
    Classification runs in <1ms — nanosecond-class compared to any
    model-based router.

    The router considers three axes:
        1. Signal urgency (critical/high/normal/low)
        2. Signal source (test_failure/voice_human/ai_miner/etc.)
        3. Task complexity (trivial/simple/moderate/heavy_code/complex)

    Priority order:
        1. IMMEDIATE — urgency trumps everything (critical ops can't wait)
        2. SPECULATIVE — intent_discovery with low urgency (fire and forget)
        3. BACKGROUND — low-urgency background sources (DW only)
        4. COMPLEX — heavy_code/complex tasks (Claude plan + DW execute)
        5. STANDARD — everything else (default DW → Claude cascade)
    """

    # Routing decision cache for telemetry
    _last_decision_reason: str = ""

    def classify(
        self,
        ctx: "OperationContext",
    ) -> Tuple[ProviderRoute, str]:
        """Classify an operation into a provider route.

        Parameters
        ----------
        ctx : OperationContext
            Must have signal_urgency, signal_source, and task_complexity
            fields populated (stamped at intake and CLASSIFY phases).

        Returns
        -------
        Tuple[ProviderRoute, str]
            The route and a human-readable reason string for telemetry.
        """
        urgency = getattr(ctx, "signal_urgency", "") or "normal"
        source = getattr(ctx, "signal_source", "") or ""
        complexity = getattr(ctx, "task_complexity", "") or "moderate"
        file_count = len(ctx.target_files) if ctx.target_files else 1
        cross_repo = getattr(ctx, "cross_repo", False)

        # ── Priority 1: IMMEDIATE ──
        # Critical urgency ALWAYS routes to Claude direct.
        # Speed is the only metric that matters for critical ops.
        if urgency in _IMMEDIATE_URGENCIES:
            reason = f"critical_urgency:{source or 'unknown'}"
            return ProviderRoute.IMMEDIATE, reason

        # Voice commands are always immediate — human is waiting.
        if source == "voice_human":
            reason = "voice_command:human_waiting"
            return ProviderRoute.IMMEDIATE, reason

        # High-urgency test failures and runtime health — fast reflex.
        if urgency == "high" and source in _IMMEDIATE_SOURCES:
            reason = f"high_urgency_immediate_source:{source}"
            return ProviderRoute.IMMEDIATE, reason

        # Cross-repo operations need Claude's reliable multi-file handling.
        if cross_repo:
            reason = f"cross_repo:{file_count}_files"
            return ProviderRoute.IMMEDIATE, reason

        # ── Priority 2: SPECULATIVE ──
        # Intent discovery with non-urgent signals — fire and forget.
        if source in _SPECULATIVE_SOURCES and urgency in ("low", "normal"):
            reason = f"speculative_source:{source}:{urgency}"
            return ProviderRoute.SPECULATIVE, reason

        # ── Priority 3: BACKGROUND ──
        # Low-urgency signals from background sources — DW only.
        if urgency in _BACKGROUND_URGENCIES and source in _BACKGROUND_SOURCES:
            reason = f"background_source:{source}:low_urgency"
            return ProviderRoute.BACKGROUND, reason

        # Background sources with normal urgency — still route to DW
        # unless complexity demands Claude involvement.
        if source in _BACKGROUND_SOURCES and complexity not in _COMPLEX_COMPLEXITIES:
            reason = f"background_source:{source}:{complexity}"
            return ProviderRoute.BACKGROUND, reason

        # ── Priority 4: COMPLEX ──
        # Heavy/complex tasks benefit from Claude's planning + DW's cheap execution.
        if complexity in _COMPLEX_COMPLEXITIES:
            reason = f"complex_task:{complexity}:{file_count}_files"
            return ProviderRoute.COMPLEX, reason

        # Multi-file operations (3+) benefit from planning even at moderate complexity.
        if file_count >= 3 and complexity not in ("trivial", "simple"):
            reason = f"multi_file_complex:{file_count}_files:{complexity}"
            return ProviderRoute.COMPLEX, reason

        # ── Priority 5: STANDARD (default) ──
        # Normal operations: DW 397B primary with Claude fallback.
        reason = f"standard:{urgency}:{source or 'unknown'}:{complexity}"
        return ProviderRoute.STANDARD, reason

    @staticmethod
    def route_budget_profile(route: ProviderRoute) -> Dict[str, float]:
        """Return budget allocation hints for a given route.

        These are advisory — CandidateGenerator uses them to tune
        timeout allocation between DW and Claude.

        Returns dict with:
            tier0_fraction: fraction of total budget for DW (0.0-1.0)
            tier1_reserve_s: minimum seconds reserved for Claude
            max_dw_wait_s: hard cap on DW wait time
        """
        if route is ProviderRoute.IMMEDIATE:
            return {
                "tier0_fraction": 0.0,
                "tier1_reserve_s": 0.0,  # Claude gets everything
                "max_dw_wait_s": 0.0,
            }
        if route is ProviderRoute.STANDARD:
            return {
                "tier0_fraction": 0.65,
                "tier1_reserve_s": 25.0,
                "max_dw_wait_s": 90.0,
            }
        if route is ProviderRoute.COMPLEX:
            return {
                "tier0_fraction": 0.80,  # DW executes, gets most budget
                "tier1_reserve_s": 20.0,  # Claude already planned
                "max_dw_wait_s": 120.0,
            }
        if route is ProviderRoute.BACKGROUND:
            return {
                "tier0_fraction": 1.0,   # DW only
                "tier1_reserve_s": 0.0,   # No Claude fallback
                "max_dw_wait_s": 180.0,   # Relaxed — no urgency
            }
        if route is ProviderRoute.SPECULATIVE:
            return {
                "tier0_fraction": 1.0,
                "tier1_reserve_s": 0.0,
                "max_dw_wait_s": 300.0,   # Very relaxed — fire and forget
            }
        # Fallback: standard profile
        return {
            "tier0_fraction": 0.65,
            "tier1_reserve_s": 25.0,
            "max_dw_wait_s": 90.0,
        }

    @staticmethod
    def describe_route(route: ProviderRoute) -> str:
        """Human-readable one-liner for SerpentFlow / CommProtocol."""
        _descriptions = {
            ProviderRoute.IMMEDIATE: "Claude direct (fast reflex)",
            ProviderRoute.STANDARD: "DW primary → Claude fallback",
            ProviderRoute.COMPLEX: "Claude plans → DW executes",
            ProviderRoute.BACKGROUND: "DW only (cost-optimized)",
            ProviderRoute.SPECULATIVE: "DW batch (fire-and-forget)",
        }
        return _descriptions.get(route, "unknown route")
