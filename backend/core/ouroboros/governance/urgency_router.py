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
import os
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

    INFORMATIONAL = "informational"
    """§41.3 #26 Phase 2 D3b — read-only knowledge-lookup route.
    Used for: fast-path Q&A (operator-typed ``/ask`` queries).
    Contract: NO code generation, NO file mutations, NO DW
    cascade (semantic_index grounds Claude; nothing leaves the
    knowledge surface). Per-route sub-budget via
    ``JARVIS_INFORMATIONAL_BUDGET_USD``. Operator-signed
    2026-05-11 per §41.3.1 D3b. Adding this route to the
    closed-5→6 taxonomy is the structural recognition that Q&A
    traffic is its own first-class route with isolated cost
    accounting."""

    WIRING_VALIDATION = "wiring_validation"
    """Slice 12AD — budget-aware route for wiring-validation fixtures.
    Used for: SWE-Bench-Pro smoke fixtures + any future envelope
    declaring ``metadata.purpose == "wiring_validation"`` AND
    ``metadata.real_benchmark == False``. Contract: trivially-passing
    structural fixtures get a deeply-reduced CostGov factor
    (``JARVIS_OP_COST_ROUTE_WIRING_VALIDATION`` default 0.1, so the
    derived per-op cap lands ~$0.05-0.10 vs ~$2.00 for COMPLEX) and
    skip the Venom multi-round tool loop via
    ``route_predicates.VENOM_SKIP_ROUTES`` (a no-op patch is the
    structurally-correct answer; multi-round exploration burns budget
    on a fixture that needs none). Real benchmarks MUST NEVER take
    this route — defense via 2-signal AND in
    :func:`envelope_metadata.is_route_wiring_validation_envelope`
    (``fixture_purpose == "wiring_validation"`` AND
    ``real_benchmark is False``). Master flag
    ``JARVIS_WIRING_VALIDATION_ROUTE_ENABLED`` default-FALSE per
    §33.1 — when off, classify falls through to the existing
    Priority 1-5 matrix and the route is never stamped. Adding this
    route to the closed-6→7 taxonomy is the structural answer to
    the bt-2026-05-24-033510 finding: governance-pipeline minimum-
    spend floor ($1.81+ for any op through IronGate exploration +
    Venom rounds + retry headroom) exceeds runbook Phase-1 estimates
    for fixtures that don't need any of that work."""


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

# Complexity levels that qualify for COMPLEX routing.
# Architectural ops are the *most* complex class ComplexityClassifier
# emits ("new capability", "design", "protocol", "schema", "migration")
# — they need Claude's planning strength paired with DW's cheap
# streaming execution. Missing this entry caused every architectural
# single-file op to fall through to STANDARD (DW primary, no Claude
# plan), contradicting CLAUDE.md §"Urgency-Aware Provider Routing".
_COMPLEX_COMPLEXITIES = frozenset({
    "heavy_code",
    "complex",
    "architectural",
})

# Urgency levels that qualify for IMMEDIATE routing
_IMMEDIATE_URGENCIES = frozenset({
    "critical",
})

# Urgency levels that qualify for BACKGROUND routing
_BACKGROUND_URGENCIES = frozenset({
    "low",
})


# Enumerated route values used by the pre-stamp override path below.
_VALID_ROUTE_VALUES = frozenset(r.value for r in ProviderRoute)


def _respect_pre_stamped_route() -> bool:
    """Return True when the classifier should honor a pre-stamped route.

    Opt-in via ``JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED``. Default OFF so
    production routing stays fully deterministic; the switch exists for
    isolation harnesses (e.g. the Qwen 397B benchmark) that need to force
    a specific route without monkey-patching the classifier.
    """
    raw = os.environ.get(
        "JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", "",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# F2 Slice 2 — priority-0.5 clause gate.
# Distinct from the priority-0 harness flag above: this one consumes
# ONLY provider_route stamped by the UnifiedIntakeRouter from an
# envelope's ``routing_override`` (disambiguated by the reason prefix
# ``envelope_routing_override:``). Keeps F2 orthogonal to the harness
# knob so neither flag can accidentally consume the other's pre-stamp.
_ENVELOPE_ROUTING_OVERRIDE_REASON_PREFIX = "envelope_routing_override"


def _envelope_routing_override_enabled() -> bool:
    """Re-read ``JARVIS_BACKLOG_URGENCY_HINT_ENABLED`` at call-time.

    Shares the master flag with F2 Slice 1's per-entry urgency_hint
    (single operator knob for the full F2 arc). Default OFF.
    """
    raw = os.environ.get(
        "JARVIS_BACKLOG_URGENCY_HINT_ENABLED", "",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Slice 12AD — master flag for budget-aware wiring-validation route.
WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR = (
    "JARVIS_WIRING_VALIDATION_ROUTE_ENABLED"
)


def _wiring_validation_route_enabled() -> bool:
    """Slice 12AD — re-read
    ``JARVIS_WIRING_VALIDATION_ROUTE_ENABLED`` at call-time.

    Default-FALSE per §33.1 (operator's binding for any new route
    behavior). When OFF, ``UrgencyRouter.classify`` ignores the
    wiring-validation envelope detector entirely and the
    ``WIRING_VALIDATION`` route is never stamped — every fixture
    falls through to the existing Priority 1-5 matrix (byte-identical
    legacy behavior).

    Re-reads from env at every call (not cached at boot) so tests
    can flip the flag mid-process and so operators can toggle via
    ``/help flags`` / SSE flag-changed without restart.
    """
    raw = os.environ.get(
        WIRING_VALIDATION_ROUTE_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Slice 22 — Dynamic Tier Degradation Engine
# ---------------------------------------------------------------------------
#
# v16 forensic (bt-2026-05-26-220930) exposed a structural gap: when
# ``JARVIS_PROVIDER_CLAUDE_DISABLED=true`` is set, ClaudeProvider is not
# constructed (Slice 19a). But the UrgencyRouter still routes ops to
# ``IMMEDIATE`` per §5 ("Claude direct, skip DW"). With Claude absent,
# the cascade exhausts at the dispatcher with ``fallback_skipped:
# no_fallback_configured`` (Slice 19b) — the op dies before any provider
# call lands. v16 burned 22 minutes and $0 of useful spend exactly this
# way: SWE-Bench-Pro envelopes that didn't match the narrow
# ``envelope_is_swe_bench_pro`` downgrade got classified IMMEDIATE and
# vanished.
#
# Slice 22 fixes this STRUCTURALLY at the router rather than tagging
# individual envelopes: when the router resolves to IMMEDIATE AND the
# Claude tier is structurally absent (Slice 19a env), demote to
# STANDARD (DW-primary). The healing matrix (Slices 20B/20C/20D +
# Phase 3) is now actually reachable for the demoted op.
#
# The signal "Claude structurally absent" mirrors Slice 19a's contract
# verbatim — same env var, same parsing rules — so the demotion fires
# exactly when ClaudeProvider construction was skipped.
#
# §5 Manifesto transparency: every demotion logs the operator-attested
# message at WARNING so the routing change is visible without grep.

#: Slice 22 master switch — default TRUE because a) the only failure
#: mode it has is the SAME failure mode we have today (cascade exhausts
#: at dispatcher), b) the demotion path is deterministic + bounded, and
#: c) Slice 19a's env var is itself opt-in, so this only fires when the
#: operator has already structurally removed Claude.
TIER_DECAY_ENABLED_ENV_VAR = "JARVIS_TIER_DECAY_ENABLED"

#: Slice 19a contract — same env var the GovernedLoopService reads at
#: ClaudeProvider construction time. Reading it here gives us the
#: structural truth: when this is set, Claude is NOT in the registry.
CLAUDE_DISABLED_ENV_VAR = "JARVIS_PROVIDER_CLAUDE_DISABLED"


def _tier_decay_enabled() -> bool:
    """Slice 22 master flag — re-read at every classify() call.

    Default-TRUE: in a Claude-disabled environment the router MUST
    demote IMMEDIATE → STANDARD or the op is structurally lost.
    Setting to false restores legacy behavior (the v16 failure mode)
    for forensic comparison.
    """
    raw = os.environ.get(TIER_DECAY_ENABLED_ENV_VAR, "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def _claude_tier_structurally_absent() -> bool:
    """Slice 22 — non-blocking check against the active provider tier.

    Mirrors Slice 19a's env-var contract verbatim
    (``JARVIS_PROVIDER_CLAUDE_DISABLED``) so the structural signal
    we read matches the structural signal GovernedLoopService used
    to skip ClaudeProvider construction.

    Pure env-var lookup — no provider registry import, no network,
    no async, sub-microsecond. Safe to call inside the router hot
    path.
    """
    raw = os.environ.get(CLAUDE_DISABLED_ENV_VAR, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _apply_immediate_tier_decay(
    reason: str,
) -> Tuple["ProviderRoute", str]:
    """Slice 22 — post-classification IMMEDIATE-to-STANDARD demotion.

    Called by every ``classify()`` return site that would otherwise
    emit ``ProviderRoute.IMMEDIATE``. When tier decay is enabled
    AND Claude is structurally absent, demotes to STANDARD with a
    forensic-trail reason string that preserves the ORIGINAL routing
    rationale (so postmortems can still attribute "why did this op
    want to be IMMEDIATE in the first place").

    Logs the §5-attested transition message verbatim at WARNING level
    so the routing change is visible without grep.

    Returns the (route, reason) tuple the caller passes through
    unchanged in the no-decay path.
    """
    if not _tier_decay_enabled():
        return ProviderRoute.IMMEDIATE, reason
    if not _claude_tier_structurally_absent():
        return ProviderRoute.IMMEDIATE, reason
    # Decay fires — operator-attested §5 transparency message
    logger.warning(
        "[UrgencyRouter] Adaptive tier decay activated: "
        "IMMEDIATE → STANDARD. Reason: Claude infrastructure "
        "tier structurally absent."
    )
    return (
        ProviderRoute.STANDARD,
        f"tier_decay:immediate_to_standard:claude_absent:{reason}",
    )


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
        # ── Priority 0: pre-stamped route override (opt-in, tests only) ──
        # When JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED is on, an op that
        # already carries a valid ``provider_route`` skips classification
        # entirely. This is the supported hook for isolation harnesses to
        # force a specific route without monkey-patching the router.
        if _respect_pre_stamped_route():
            forced = (getattr(ctx, "provider_route", "") or "").strip().lower()
            if forced and forced in _VALID_ROUTE_VALUES:
                return ProviderRoute(forced), f"forced_pre_stamped:{forced}"

        # ── Priority 0.5: F2 envelope_routing_override ──
        # When JARVIS_BACKLOG_URGENCY_HINT_ENABLED is on AND the ctx was
        # pre-stamped by UnifiedIntakeRouter from an envelope carrying a
        # valid ``routing_override`` (disambiguated by the reason prefix
        # ``envelope_routing_override:``), honor it immediately. This is
        # how F2 lets individual backlog.json entries declare their own
        # routing — unblocking the source=backlog → BACKGROUND trap for
        # graduation seeds without coupling to the harness priority-0
        # knob above.
        #
        # Validation is defensive: empty / bogus / case-variant values
        # silently fall through to the normal priority 1-5 matrix. The
        # intake router emits only lowercase enum values, but we still
        # normalize to be robust against manual ctx construction in tests.
        if _envelope_routing_override_enabled():
            _reason_raw = (getattr(ctx, "provider_route_reason", "") or "")
            if _reason_raw.startswith(_ENVELOPE_ROUTING_OVERRIDE_REASON_PREFIX):
                _route_raw = (getattr(ctx, "provider_route", "") or "").strip().lower()
                if _route_raw and _route_raw in _VALID_ROUTE_VALUES:
                    return (
                        ProviderRoute(_route_raw),
                        f"{_ENVELOPE_ROUTING_OVERRIDE_REASON_PREFIX}:{_route_raw}",
                    )

        # ── Priority 0.6: WIRING_VALIDATION (Slice 12AD) ──
        # When ``JARVIS_WIRING_VALIDATION_ROUTE_ENABLED`` is on AND the
        # envelope carries operator's two-signal wiring-validation
        # criteria (``fixture_purpose=="wiring_validation"`` AND
        # ``real_benchmark is False``), short-circuit to the budget-
        # aware WIRING_VALIDATION route. This bypasses the Priority 1-5
        # matrix entirely — the IronGate exploration mandate (Slice 12P)
        # + Venom tool loop (route_predicates.VENOM_SKIP_ROUTES) + low
        # CostGov factor (route_factors["wiring_validation"]=0.1) all
        # compose downstream off the route name alone, with no further
        # envelope inspection.
        #
        # Master flag default-FALSE per §33.1: when OFF, this entire
        # block is byte-identical to pre-Slice-12AD behavior — every
        # fixture falls through to the existing Priority 1-5 matrix
        # (typically STANDARD / COMPLEX) and burns the full pipeline
        # budget (~$1.81 floor per bt-2026-05-24-033510). Real benchmarks
        # are PERMANENTLY excluded from this route — the detector's
        # ``real_benchmark is False`` clause is exact-False, not falsy,
        # so missing key / None / "false" all read as "assume real
        # benchmark" and the detector returns False. Defense in depth.
        if _wiring_validation_route_enabled():
            try:
                from backend.core.ouroboros.governance.envelope_metadata import (  # noqa: E501
                    is_route_wiring_validation_envelope,
                )
                if is_route_wiring_validation_envelope(ctx):
                    return (
                        ProviderRoute.WIRING_VALIDATION,
                        "wiring_validation_envelope:purpose=wiring_validation,"
                        "real_benchmark=false",
                    )
            except Exception:  # noqa: BLE001 — defensive
                # NEVER let envelope inspection break the route — fall
                # through to the Priority 1-5 matrix on any failure.
                pass

        # ── Priority 0.7: SWE-Bench-Pro real-benchmark downgrade (Slice 10A) ──
        # bt-2026-05-25-215404 root: real SWE-Bench-Pro ops were classifying
        # as IMMEDIATE (test_failure source + high urgency) and routing every
        # repair generation through Claude direct. The result: 99.83% of soak
        # spend ($1.43 / $1.43) landed on Claude while DW saw $0.002, the
        # exact INVERSE of the trinity manifesto's cost-optimization intent
        # ("DW Tier 0 preferred, Claude Tier 1 fallback").
        #
        # Architectural diagnosis: §5 urgency routing was designed for the
        # human-reflex case (voice command, IDE test failure mid-typing,
        # runtime health alarm). SWE-Bench-Pro evaluations are BENCHMARK
        # FIXTURES — no human is waiting on the Ansible repair. They are
        # urgency-1 signals only because they masquerade as test failures
        # through the test_failure signal source.
        #
        # Fix: if the envelope was emitted by the SWE-Bench-Pro builder
        # (``envelope_is_swe_bench_pro(ctx) is True``) AND it did NOT
        # qualify for the narrower WIRING_VALIDATION route above
        # (i.e., this is a real benchmark or non-wiring-validation
        # fixture), downgrade to STANDARD. STANDARD = DW primary + Claude
        # fallback — preserves capability (Claude still steps in if DW
        # exhausts) while restoring the 87% cost savings the trinity
        # cascade was designed to deliver.
        #
        # Reflex routing for genuine human signals is UNCHANGED — the
        # SWE-Bench-Pro envelope tag is the ONLY discriminator. Voice,
        # IDE test failure, runtime health continue to route IMMEDIATE
        # because their envelopes don't carry ``swe_bench_pro=True``.
        # Defensive try/except so envelope-inspection failures fall
        # through to the existing Priority 1-5 matrix.
        try:
            from backend.core.ouroboros.governance.envelope_metadata import (  # noqa: E501
                envelope_is_swe_bench_pro,
            )
            if envelope_is_swe_bench_pro(ctx):
                return (
                    ProviderRoute.STANDARD,
                    "swe_bench_pro_envelope:not_human_blocking:"
                    "downgrade_to_dw_primary",
                )
        except Exception:  # noqa: BLE001 — defensive (envelope inspection)
            # NEVER let envelope inspection break the route — fall through.
            pass

        # ── Priority 0.75: headless-soak sensor demotion (Slice 223) ──
        # The Slice-10A pattern, generalized. §5 urgency routing was designed
        # for the HUMAN-REFLEX case; in a headless soak NO HUMAN IS WAITING on
        # a sensor alarm. Live evidence (2026-06-12): the TestFailure sensor's
        # storm over 3 known pre-existing failures routed IMMEDIATE ->
        # Claude-direct, saturating the worker pool + burning premium tokens
        # on unfixable noise while the operator-signed GOAL-001 starved 40min
        # in queue. Demote test_failure-source ops to STANDARD (DW primary,
        # Claude per-round rescue preserved — the 10A choice) when BOTH:
        #   * JARVIS_SOAK_SENSOR_DEMOTION_ENABLED (gate, default FALSE)
        #   * OUROBOROS_BATTLE_HEADLESS truthy (no human present)
        # Source-discriminated (signal_source, stamped by the sensor), NOT a
        # priority label a generated patch could self-elevate (S208 concern).
        # Interactive sessions byte-identical: a human watching a test fail
        # mid-typing keeps the reflex lane.
        try:
            _demote_on = os.environ.get(
                "JARVIS_SOAK_SENSOR_DEMOTION_ENABLED", "",
            ).strip().lower() in ("1", "true", "yes", "on")
            _headless = os.environ.get(
                "OUROBOROS_BATTLE_HEADLESS", "",
            ).strip().lower() in ("1", "true", "yes", "on")
            if (
                _demote_on and _headless
                and (getattr(ctx, "signal_source", "") or "") == "test_failure"
            ):
                return (
                    ProviderRoute.STANDARD,
                    "headless_soak_sensor_demotion:test_failure:"
                    "no_human_waiting:dw_primary",
                )
        except Exception:  # noqa: BLE001 — defensive
            pass

        urgency = getattr(ctx, "signal_urgency", "") or "normal"
        source = getattr(ctx, "signal_source", "") or ""
        complexity = getattr(ctx, "task_complexity", "") or "moderate"
        file_count = len(ctx.target_files) if ctx.target_files else 1
        cross_repo = getattr(ctx, "cross_repo", False)

        # ── Priority 1: IMMEDIATE ──
        # Critical urgency ALWAYS routes to Claude direct.
        # Speed is the only metric that matters for critical ops.
        # Slice 22: each IMMEDIATE return flows through
        # ``_apply_immediate_tier_decay`` which transparently demotes
        # to STANDARD when Claude is structurally absent. Legacy
        # behavior preserved when ``JARVIS_TIER_DECAY_ENABLED=false``
        # or when Claude IS available (the no-decay paths are
        # byte-identical to the original returns).
        if urgency in _IMMEDIATE_URGENCIES:
            reason = f"critical_urgency:{source or 'unknown'}"
            return _apply_immediate_tier_decay(reason)

        # Voice commands are always immediate — human is waiting.
        if source == "voice_human":
            reason = "voice_command:human_waiting"
            return _apply_immediate_tier_decay(reason)

        # High-urgency test failures and runtime health — fast reflex.
        if urgency == "high" and source in _IMMEDIATE_SOURCES:
            reason = f"high_urgency_immediate_source:{source}"
            return _apply_immediate_tier_decay(reason)

        # Cross-repo operations need Claude's reliable multi-file handling.
        if cross_repo:
            reason = f"cross_repo:{file_count}_files"
            return _apply_immediate_tier_decay(reason)

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
        if route is ProviderRoute.WIRING_VALIDATION:
            # Slice 12AF — wiring-validation fixtures bypass DW
            # entirely. The CostGov per-op cap (route_factor 0.1 →
            # cap ≈ $0.05-$0.10) is too tight for a DW-then-Claude
            # cascade. Single direct Claude call with the full
            # route budget; composes cleanly with VENOM_SKIP_ROUTES
            # (no tool loop) + Site 3 (no tool instructions in
            # prompt) so the model emits 2b.1-noop or 2b.1 patch
            # directly. Closes the bt-2026-05-24-065236 cosmetic
            # gap ("route_description='unknown route'") AND
            # eliminates the wasteful Tier-0 DW attempt that
            # produced nothing useful + the subsequent fallback to
            # Claude with tool instructions that triggered the
            # 2b.2-tool hallucination wedge.
            return {
                "tier0_fraction": 0.0,
                "tier1_reserve_s": 0.0,
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
            # Nervous-system reflex: when JARVIS_BACKGROUND_ALLOW_FALLBACK
            # is on, BACKGROUND must leave headroom for Claude so
            # CandidateGenerator._generate_background can cascade on DW
            # failure instead of raising. The DW cap here (150s) MUST
            # match the cap inside _generate_background — keep them in
            # sync. Default (flag off): legacy DW-only profile.
            _allow_fb = os.environ.get(
                "JARVIS_BACKGROUND_ALLOW_FALLBACK", "",
            ).strip().lower() in {"1", "true", "yes", "on"}
            if _allow_fb:
                return {
                    "tier0_fraction": 0.65,
                    "tier1_reserve_s": 25.0,
                    "max_dw_wait_s": 150.0,
                }
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
