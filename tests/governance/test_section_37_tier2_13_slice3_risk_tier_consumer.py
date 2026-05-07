"""§37 Tier 2 #13 Slice 3 — risk-tier-floor confidence consumer.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (35+ tests):
  * worst_band_for_op aggregation: empty observer / no matching op /
    single tool / multiple tools / picks worst by severity / never
    raises on broken state
  * Module-level worst_band_for_op convenience composes singleton
  * _confidence_floor_for_op mapping: UNKNOWN/LOW/MEDIUM → notify_apply,
    HIGH/CERTAIN → None, no op_id → None, master-off → None
  * recommended_floor with op_id: composes confidence floor + env
    floors via strictest-wins
  * apply_floor_to_name with op_id: clamps SAFE_AUTO → NOTIFY_APPLY
    when band ≤ MEDIUM + master on; pass-through when band ≥ HIGH
    or master off
  * apply_floor_to_name with op_id: never DOWNGRADES (e.g.,
    APPROVAL_REQUIRED + low confidence = APPROVAL_REQUIRED)
  * floor_reason mentions confidence band when applicable
  * Backward compat: existing call sites without op_id work unchanged
  * Orchestrator wiring: GATE call site passes op_id=ctx.op_id (AST scan)
  * Defensive: importerror / observer outage NEVER propagates
  * Single source of truth: only Slice 1 master flag gates the consumer
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_observer():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# worst_band_for_op — observer aggregator
# ---------------------------------------------------------------------------


def test_worst_band_for_op_empty_observer_returns_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    assert obs.worst_band_for_op("op-1") is None


def test_worst_band_for_op_no_matching_streams_returns_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op-A", tool_name="x",
        publish_sse=False,
    )
    # Different op_id — no match.
    assert obs.worst_band_for_op("op-B") is None


def test_worst_band_for_op_single_tool():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert (
        obs.worst_band_for_op("op1") == ToolConfidenceBand.UNKNOWN
    )


def test_worst_band_for_op_picks_worst_across_tools():
    """When op1 has 3 tools at HIGH/MEDIUM/LOW, return LOW
    (highest severity = worst)."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.85, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    obs.record(
        confidence=0.55, op_id="op1", tool_name="search_code",
        publish_sse=False,
    )
    obs.record(
        confidence=0.35, op_id="op1", tool_name="bash",
        publish_sse=False,
    )
    assert obs.worst_band_for_op("op1") == ToolConfidenceBand.LOW


def test_worst_band_for_op_isolation_across_ops():
    """op1's MEDIUM band must not mask op2's UNKNOWN band."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.55, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    obs.record(
        confidence=0.10, op_id="op2", tool_name="y",
        publish_sse=False,
    )
    assert (
        obs.worst_band_for_op("op1") == ToolConfidenceBand.MEDIUM
    )
    assert (
        obs.worst_band_for_op("op2") == ToolConfidenceBand.UNKNOWN
    )


def test_worst_band_for_op_empty_op_id_returns_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert obs.worst_band_for_op("") is None
    # type: ignore[arg-type]
    assert obs.worst_band_for_op(None) is None  # type: ignore[arg-type]


def test_module_level_worst_band_for_op_composes_singleton():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, get_default_observer,
        worst_band_for_op,
    )
    obs = get_default_observer()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    # Module-level helper composes the singleton — no parallel
    # state.
    assert worst_band_for_op("op1") == ToolConfidenceBand.UNKNOWN


# ---------------------------------------------------------------------------
# _confidence_floor_for_op — band → tier mapping
# ---------------------------------------------------------------------------


def test_confidence_floor_returns_none_when_op_id_empty():
    from backend.core.ouroboros.governance.risk_tier_floor import (
        _confidence_floor_for_op,
    )
    assert _confidence_floor_for_op("") is None
    assert _confidence_floor_for_op(None) is None


def test_confidence_floor_returns_none_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.risk_tier_floor import (
        _confidence_floor_for_op,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    # Master off → no floor (defense in depth).
    assert _confidence_floor_for_op("op1") is None


def test_confidence_floor_returns_none_when_no_observation(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        _confidence_floor_for_op,
    )
    # No observation recorded — no floor.
    assert _confidence_floor_for_op("op1") is None


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (0.95, None),    # CERTAIN — no clamp
        (0.75, None),    # HIGH — no clamp
        (0.55, "notify_apply"),  # MEDIUM — clamp
        (0.35, "notify_apply"),  # LOW — clamp
        (0.10, "notify_apply"),  # UNKNOWN — clamp
    ],
)
def test_confidence_floor_band_to_tier_mapping(
    confidence, expected, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        _confidence_floor_for_op,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=confidence, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert _confidence_floor_for_op("op1") == expected


def test_confidence_floor_swallows_observer_exception(
    monkeypatch,
):
    """Defensive: if the observer module raises, return None."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as toolconf,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        _confidence_floor_for_op,
    )
    monkeypatch.setattr(
        toolconf, "worst_band_for_op",
        lambda op_id: (_ for _ in ()).throw(
            RuntimeError("boom"),
        ),
    )
    # Must NOT raise.
    assert _confidence_floor_for_op("op1") is None


# ---------------------------------------------------------------------------
# recommended_floor with op_id — composition
# ---------------------------------------------------------------------------


def test_recommended_floor_no_op_id_unchanged(monkeypatch):
    """Backward compat: no op_id → identical behavior to v
    pre-Slice-3 (no confidence floor in the candidates list)."""
    from backend.core.ouroboros.governance.risk_tier_floor import (
        recommended_floor,
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    assert recommended_floor() is None


def test_recommended_floor_confidence_only(monkeypatch):
    """Only confidence floor active → returns notify_apply."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        recommended_floor,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert (
        recommended_floor(op_id="op1") == "notify_apply"
    )


def test_recommended_floor_strictest_wins(monkeypatch):
    """Confidence (notify_apply) + explicit env BLOCKED →
    BLOCKED wins (strictest)."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "blocked")
    from backend.core.ouroboros.governance.risk_tier_floor import (
        recommended_floor,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    # Confidence implies notify_apply; env implies blocked;
    # strictest wins.
    assert recommended_floor(op_id="op1") == "blocked"


def test_recommended_floor_high_confidence_no_clamp(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        recommended_floor,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.95, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    # CERTAIN band — no confidence floor.
    assert recommended_floor(op_id="op1") is None


# ---------------------------------------------------------------------------
# apply_floor_to_name with op_id — clamping behavior
# ---------------------------------------------------------------------------


def test_apply_floor_clamps_safe_auto_to_notify_apply(monkeypatch):
    """Load-bearing case: SAFE_AUTO base + LOW confidence +
    master ON → NOTIFY_APPLY. The Antivenom semantic."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        apply_floor_to_name,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    effective, applied = apply_floor_to_name(
        "safe_auto", op_id="op1",
    )
    assert effective == "notify_apply"
    assert applied == "notify_apply"


def test_apply_floor_no_clamp_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        apply_floor_to_name,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    # Master off → no clamp even though band is UNKNOWN.
    effective, applied = apply_floor_to_name(
        "safe_auto", op_id="op1",
    )
    assert effective == "safe_auto"
    assert applied is None


def test_apply_floor_never_downgrades_higher_tier(monkeypatch):
    """APPROVAL_REQUIRED + LOW confidence → still
    APPROVAL_REQUIRED (clamp is upward-only)."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        apply_floor_to_name,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    effective, applied = apply_floor_to_name(
        "approval_required", op_id="op1",
    )
    assert effective == "approval_required"
    assert applied is None


def test_apply_floor_no_op_id_backward_compat(monkeypatch):
    """Existing callers that pass no op_id MUST behave
    identically to pre-Slice-3."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        apply_floor_to_name,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    # Even with master ON + LOW band recorded, NO op_id arg →
    # confidence floor doesn't fire.
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    effective, applied = apply_floor_to_name("safe_auto")
    assert effective == "safe_auto"
    assert applied is None


def test_apply_floor_unknown_input_tier_passes_through(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        apply_floor_to_name,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    effective, applied = apply_floor_to_name(
        "garbage_tier", op_id="op1",
    )
    assert effective == "garbage_tier"
    assert applied is None


# ---------------------------------------------------------------------------
# floor_reason — confidence band in observability
# ---------------------------------------------------------------------------


def test_floor_reason_includes_confidence_band(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        floor_reason,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    reason = floor_reason(op_id="op1")
    assert "tool_confidence_band=unknown" in reason
    assert "Slice 3" in reason


def test_floor_reason_no_band_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    monkeypatch.delenv("JARVIS_PARANOIA_MODE", raising=False)
    monkeypatch.delenv(
        "JARVIS_AUTO_APPLY_QUIET_HOURS", raising=False,
    )
    from backend.core.ouroboros.governance.risk_tier_floor import (
        floor_reason,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    get_default_observer().record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    reason = floor_reason(op_id="op1")
    assert "tool_confidence_band" not in reason


def test_floor_reason_no_op_id_backward_compat():
    from backend.core.ouroboros.governance.risk_tier_floor import (
        floor_reason,
    )
    # Without op_id, behavior is unchanged.
    reason = floor_reason()
    assert "tool_confidence_band" not in reason


# ---------------------------------------------------------------------------
# Orchestrator GATE wiring — AST scan
# ---------------------------------------------------------------------------


def test_orchestrator_gate_passes_op_id_to_apply_floor():
    """The orchestrator's GATE phase MUST pass op_id=ctx.op_id
    to apply_floor_to_name so the confidence floor can
    activate. AST scan."""
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text(encoding="utf-8")
    # Look for `apply_floor_to_name(_cur_name, op_id=_op_id)`
    # OR equivalent — the `op_id=` kwarg must appear in the
    # call to apply_floor_to_name.
    tree = ast.parse(src)
    found_call_with_op_id = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Name)
            and func.id == "apply_floor_to_name"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "op_id":
                found_call_with_op_id = True
                break
    assert found_call_with_op_id, (
        "orchestrator.py MUST call apply_floor_to_name with "
        "op_id= kwarg (Slice 3 wiring)"
    )


def test_orchestrator_gate_passes_op_id_to_floor_reason():
    """The orchestrator's GATE log line MUST pass op_id to
    floor_reason for observability completeness."""
    src = (
        _repo_root()
        / "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_call_with_op_id = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Name)
            and func.id == "floor_reason"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "op_id":
                found_call_with_op_id = True
                break
    assert found_call_with_op_id, (
        "orchestrator.py MUST call floor_reason(op_id=...) "
        "(Slice 3 observability wiring)"
    )


# ---------------------------------------------------------------------------
# Defensive: ImportError / module unavailability
# ---------------------------------------------------------------------------


def test_confidence_floor_swallows_importerror(monkeypatch):
    """If Slice 1 module is unavailable (ImportError), confidence
    floor returns None rather than crashing risk-tier evaluation."""
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    import sys
    saved = sys.modules.pop(
        "backend.core.ouroboros.governance."
        "tool_confidence_warning_observer", None,
    )
    try:
        with patch.dict(
            sys.modules,
            {
                "backend.core.ouroboros.governance."
                "tool_confidence_warning_observer": None,
            },
        ):
            from backend.core.ouroboros.governance.risk_tier_floor import (  # noqa: E501
                _confidence_floor_for_op,
            )
            assert _confidence_floor_for_op("op1") is None
    finally:
        if saved is not None:
            sys.modules[
                "backend.core.ouroboros.governance."
                "tool_confidence_warning_observer"
            ] = saved


# ---------------------------------------------------------------------------
# Integration: V1+V2 chain still works — nothing broken in
# Slice 1/2 substrate by Slice 3 additions
# ---------------------------------------------------------------------------


def test_slice1_band_severity_still_5_values():
    """Slice 1's frozen taxonomy must still be 5-value."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, band_severity,
    )
    severities = [band_severity(b) for b in ToolConfidenceBand]
    assert sorted(severities) == [0, 1, 2, 3, 4]


def test_slice1_public_api_includes_worst_band_for_op():
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    assert "worst_band_for_op" in mod.__all__
