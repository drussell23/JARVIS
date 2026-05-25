"""Slice 10A — SWE-Bench-Pro envelope routes to STANDARD (DW primary).

Closes the cost-architecture drift surfaced by soak bt-2026-05-25-215404:

  Cost: claude=$1.4304 (99.83%) vs doubleword=$0.0024 (0.17%)
  Routes: 2 IMMEDIATE + 1 COMPLEX + 2 BACKGROUND
  DW→Claude fallback events: 7

The trinity manifesto's cost intent is "DW Tier 0 preferred, Claude
Tier 1 fallback" — 87% savings on STANDARD route. But the
UrgencyRouter (§5) was correctly classifying SWE-Bench-Pro Ansible
ops as IMMEDIATE (test_failure source + high urgency) — sending
EVERY repair generation straight to Claude direct.

# Architectural diagnosis

§5 urgency routing was designed for the HUMAN-REFLEX case:
  - voice_command:human_waiting
  - high_urgency_immediate_source:test_failure (mid-typing in IDE)
  - runtime_health (alarm)

SWE-Bench-Pro ops aren't human-reflex — they're benchmark fixtures.
No human is waiting on the Ansible repair. They route IMMEDIATE
only because they masquerade as test_failures through the signal
source classification.

# Fix mechanism

Add Priority 0.7 in UrgencyRouter.classify(), AFTER the existing
Priority 0.6 (WIRING_VALIDATION) and BEFORE the Priority 1-5
matrix. When ``envelope_is_swe_bench_pro(ctx) is True`` (using the
already-existing helper from envelope_metadata.py), downgrade to
STANDARD route (DW primary, Claude fallback).

This preserves:
  - Reflex routing for genuine human signals (voice/IDE/runtime)
    — they don't carry the swe_bench_pro envelope tag.
  - WIRING_VALIDATION for fixtures (Priority 0.6 fires first).
  - Cost fallback (STANDARD = DW primary, Claude fallback if DW
    exhausts — capability never compromised).

# Test surface (2 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "urgency_router.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_classify_calls_envelope_is_swe_bench_pro() -> None:
    """UrgencyRouter.classify MUST consult ``envelope_is_swe_bench_pro``
    BEFORE the Priority 1-5 matrix. Without this hook, real SWE-Bench-Pro
    ops continue to classify IMMEDIATE and burn Claude."""
    src = ROUTER_FILE.read_text()
    # The Slice 10A guard must call the canonical helper
    assert "envelope_is_swe_bench_pro" in src, (
        "UrgencyRouter does NOT consult envelope_is_swe_bench_pro — "
        "Slice 10A reverted. SWE-bench ops will continue to route "
        "IMMEDIATE → Claude."
    )
    # Slice 10A attribution + soak link present
    assert "Slice 10A" in src
    assert "bt-2026-05-25-215404" in src, (
        "Missing soak attribution — future readers can't trace which "
        "cost-forensics surfaced this gap"
    )
    # The reason string must be structured (operators read it in logs)
    assert "swe_bench_pro_envelope" in src, (
        "Missing structured reason string — operators can't grep route "
        "decisions"
    )


def test_ast_pin_swe_bench_check_precedes_priority_matrix() -> None:
    """The Slice 10A guard must appear BEFORE the Priority 1-5 matrix
    starts (i.e., before the urgency/source variable reads). Otherwise
    a SWE-bench op already classified IMMEDIATE in Priority 1 wouldn't
    get re-routed."""
    src = ROUTER_FILE.read_text()
    lines = src.split("\n")

    # Find line numbers
    swe_check_lines = [
        idx + 1 for idx, line in enumerate(lines)
        if "envelope_is_swe_bench_pro(ctx)" in line
    ]
    priority1_lines = [
        idx + 1 for idx, line in enumerate(lines)
        if "Priority 1: IMMEDIATE" in line
    ]

    assert swe_check_lines, "envelope_is_swe_bench_pro call not found"
    assert priority1_lines, "Priority 1 marker not found"

    # Slice 10A guard must come before Priority 1
    assert swe_check_lines[0] < priority1_lines[0], (
        f"Slice 10A guard at line {swe_check_lines[0]} is AFTER "
        f"Priority 1 IMMEDIATE block at line {priority1_lines[0]} — "
        "IMMEDIATE will fire first and the downgrade never executes."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4 (functional via stub envelope-evidence)
# ──────────────────────────────────────────────────────────────────────


class _StubCtx:
    """Minimal OperationContext stand-in for routing decisions."""
    def __init__(
        self,
        *,
        signal_urgency: str = "high",
        signal_source: str = "test_failure",
        task_complexity: str = "moderate",
        target_files=None,
        cross_repo: bool = False,
        intake_evidence_json: str = "",
        provider_route: str = "",
        provider_route_reason: str = "",
    ) -> None:
        self.signal_urgency = signal_urgency
        self.signal_source = signal_source
        self.task_complexity = task_complexity
        self.target_files = target_files or ["lib/ansible/cli/doc.py"]
        self.cross_repo = cross_repo
        self.intake_evidence_json = intake_evidence_json
        self.provider_route = provider_route
        self.provider_route_reason = provider_route_reason


def test_spine_swe_bench_pro_envelope_downgrades_to_standard() -> None:
    """A SWE-Bench-Pro envelope with signal_urgency=high +
    signal_source=test_failure (the EXACT shape that produced the
    bt-2026-05-25-215404 cost catastrophe) must route to STANDARD,
    not IMMEDIATE."""
    import json
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )
    router = UrgencyRouter()
    ctx = _StubCtx(
        signal_urgency="high",
        signal_source="test_failure",
        intake_evidence_json=json.dumps({
            "swe_bench_pro": True,
            "real_benchmark": True,  # NOT wiring_validation fixture
        }),
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.STANDARD, (
        f"Real SWE-Bench-Pro op routed to {route} instead of STANDARD — "
        f"Slice 10A inert; reason={reason}"
    )
    assert "swe_bench_pro_envelope" in reason, (
        f"Reason missing structured tag: {reason}"
    )


def test_spine_non_swe_bench_voice_command_still_immediate() -> None:
    """Reflex routing for real human signals MUST be preserved.
    A voice_human signal without the swe_bench_pro envelope tag
    still routes IMMEDIATE."""
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )
    router = UrgencyRouter()
    ctx = _StubCtx(
        signal_urgency="normal",
        signal_source="voice_human",
        intake_evidence_json="",  # no swe_bench_pro tag
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.IMMEDIATE, (
        f"Voice command no longer routes IMMEDIATE — Slice 10A broke "
        f"reflex routing for human signals; got route={route}"
    )


def test_spine_non_swe_bench_test_failure_still_immediate() -> None:
    """A real IDE test_failure (no swe_bench_pro envelope) MUST still
    route IMMEDIATE — this is the operator-typing-in-the-IDE reflex
    case the §5 router was originally designed for."""
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )
    router = UrgencyRouter()
    ctx = _StubCtx(
        signal_urgency="high",
        signal_source="test_failure",
        intake_evidence_json="",  # no swe_bench_pro tag
    )
    route, reason = router.classify(ctx)
    assert route is ProviderRoute.IMMEDIATE, (
        f"IDE test_failure (no swe_bench tag) lost IMMEDIATE routing — "
        f"got {route}; reason={reason}. Reflex case broken."
    )


def test_spine_envelope_inspection_failure_falls_through() -> None:
    """If the envelope JSON is malformed or envelope_metadata raises
    unexpectedly, the router MUST fall through to the Priority 1-5
    matrix — NEVER bubble the exception. Defensive try/except discipline.
    """
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )
    router = UrgencyRouter()
    ctx = _StubCtx(
        signal_urgency="high",
        signal_source="test_failure",
        # Malformed JSON — would normally make envelope_is_swe_bench_pro
        # return False, but if it raised it MUST be caught
        intake_evidence_json="not valid json {",
    )
    # Must not raise
    route, reason = router.classify(ctx)
    # Without the swe_bench tag, the existing IMMEDIATE classification
    # is the correct fallthrough behavior
    assert route is ProviderRoute.IMMEDIATE, (
        f"Malformed envelope should fall through to Priority 1 "
        f"IMMEDIATE; got {route}"
    )
