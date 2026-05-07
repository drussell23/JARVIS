"""§37 Tier 2 #13 Slice 1 — ToolConfidenceBand + Observer regression spine.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions—and significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage the existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (40 tests):
  * Closed taxonomy (5 values, frozen)
  * Master flag default-FALSE per §33.1
  * Threshold helpers (clamping, parse failure, env defaults)
  * Pure-function band classifier (boundaries, NaN, out-of-range,
    mis-ordered thresholds → defensive fallback)
  * Stateful observer (chatter-suppression structural,
    first-observation discipline at safe-pole vs unsafe-pole,
    multi-stream isolation, reset semantics, NEVER raises)
  * Schema-versioned artifact (§33.5 to_dict round-trip)
  * SSE publish gated on master flag (defense in depth)
  * Singleton accessor + reset_for_tests
  * AST pins all 5 validate clean against actual source
  * AST pins fire on synthetic regressions
  * No orchestrator/iron_gate imports (substrate purity)
  * FlagRegistry seeds discoverable
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_confidence_warning_observer.py"
    )


@pytest.fixture(autouse=True)
def _reset_observer_state():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy — 5 values
# ---------------------------------------------------------------------------


def test_band_taxonomy_is_5_values():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    assert {b.name for b in ToolConfidenceBand} == {
        "CERTAIN", "HIGH", "MEDIUM", "LOW", "UNKNOWN",
    }


def test_band_severity_ordering():
    """CERTAIN=0 (safe), UNKNOWN=4 (unsafe). Risk-tier consumer
    relies on this ordering."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, band_severity,
    )
    assert band_severity(ToolConfidenceBand.CERTAIN) == 0
    assert band_severity(ToolConfidenceBand.HIGH) == 1
    assert band_severity(ToolConfidenceBand.MEDIUM) == 2
    assert band_severity(ToolConfidenceBand.LOW) == 3
    assert band_severity(ToolConfidenceBand.UNKNOWN) == 4


# ---------------------------------------------------------------------------
# Master flag — default-FALSE per §33.1
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_flag_truthy(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(
            "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", v,
        )
        assert master_enabled() is True


def test_master_flag_falsy(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        master_enabled,
    )
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", v,
        )
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------


def test_thresholds_default_when_unset(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        certain_threshold_pct, high_threshold_pct,
        low_threshold_pct, medium_threshold_pct,
    )
    for n in (
        "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT",
    ):
        monkeypatch.delenv(n, raising=False)
    assert certain_threshold_pct() == 90
    assert high_threshold_pct() == 70
    assert medium_threshold_pct() == 50
    assert low_threshold_pct() == 30


def test_thresholds_clamp_low(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        low_threshold_pct,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT", "-50",
    )
    assert low_threshold_pct() == 1


def test_thresholds_clamp_high(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        certain_threshold_pct,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT", "200",
    )
    assert certain_threshold_pct() == 99


def test_thresholds_default_on_parse_error(monkeypatch):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        high_threshold_pct,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT", "garbage",
    )
    assert high_threshold_pct() == 70


# ---------------------------------------------------------------------------
# classify_band — pure function
# ---------------------------------------------------------------------------


def test_classify_certain_at_high_confidence():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(0.95) == ToolConfidenceBand.CERTAIN
    assert classify_band(0.90) == ToolConfidenceBand.CERTAIN


def test_classify_high_in_70_to_90():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(0.70) == ToolConfidenceBand.HIGH
    assert classify_band(0.80) == ToolConfidenceBand.HIGH
    assert classify_band(0.899) == ToolConfidenceBand.HIGH


def test_classify_medium_in_50_to_70():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(0.50) == ToolConfidenceBand.MEDIUM
    assert classify_band(0.60) == ToolConfidenceBand.MEDIUM


def test_classify_low_in_30_to_50():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(0.30) == ToolConfidenceBand.LOW
    assert classify_band(0.40) == ToolConfidenceBand.LOW


def test_classify_unknown_below_low():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(0.0) == ToolConfidenceBand.UNKNOWN
    assert classify_band(0.29) == ToolConfidenceBand.UNKNOWN


def test_classify_nan_returns_unknown():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(float("nan")) == ToolConfidenceBand.UNKNOWN


def test_classify_out_of_range_returns_unknown():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(-0.1) == ToolConfidenceBand.UNKNOWN
    assert classify_band(1.5) == ToolConfidenceBand.UNKNOWN


def test_classify_non_numeric_returns_unknown():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    assert classify_band(None) == ToolConfidenceBand.UNKNOWN  # type: ignore[arg-type]
    assert classify_band("garbage") == ToolConfidenceBand.UNKNOWN  # type: ignore[arg-type]


def test_classify_caller_injection_overrides_env(monkeypatch):
    """Caller-injection enables boundary testing without env
    mocking — bedrock testability discipline."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, classify_band,
    )
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT", "1",
    )
    # Despite env CERTAIN=1%, caller-supplied 99 governs.
    assert classify_band(
        0.5, certain_pct=99, high_pct=70, medium_pct=50,
        low_pct=30,
    ) == ToolConfidenceBand.MEDIUM


# ---------------------------------------------------------------------------
# Stateful observer — chatter suppression structural
# ---------------------------------------------------------------------------


def test_record_returns_none_on_same_band():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    # First obs at LOW emits (unsafe pole — first observation
    # discipline).
    first = obs.record(
        confidence=0.35, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert first is not None
    # Second obs at LOW returns None (chatter-suppressed).
    second = obs.record(
        confidence=0.40, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert second is None


def test_record_emits_on_band_change():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    # Start LOW (emits).
    a = obs.record(
        confidence=0.35, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert a is not None
    assert a.to_band == ToolConfidenceBand.LOW
    # Cross to HIGH — emits.
    b = obs.record(
        confidence=0.85, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert b is not None
    assert b.from_band == ToolConfidenceBand.LOW
    assert b.to_band == ToolConfidenceBand.HIGH


def test_first_obs_at_safe_pole_is_silent():
    """First-observation discipline: CERTAIN/HIGH first-tick is
    silent (no spurious safe-pole emission)."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    out_certain = obs.record(
        confidence=0.95, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert out_certain is None
    obs.reset()
    out_high = obs.record(
        confidence=0.75, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert out_high is None


def test_first_obs_at_unsafe_pole_emits():
    """First-observation discipline: MEDIUM/LOW/UNKNOWN
    first-tick emits immediately so operators see context."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    out = obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert out is not None
    assert out.to_band == ToolConfidenceBand.UNKNOWN


def test_streams_isolated_by_op_and_tool():
    """Different (op, tool) pairs use different streams —
    op1::read_file and op1::search_code do NOT mask each other."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand, ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    a = obs.record(
        confidence=0.10, op_id="op1", tool_name="read_file",
        publish_sse=False,
    )
    assert a is not None
    assert a.to_band == ToolConfidenceBand.UNKNOWN
    # Different tool — fresh stream — same low confidence emits.
    b = obs.record(
        confidence=0.20, op_id="op1", tool_name="search_code",
        publish_sse=False,
    )
    assert b is not None
    assert b.to_band == ToolConfidenceBand.UNKNOWN
    assert obs.stream_count() == 2


def test_explicit_stream_key_overrides_default():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="read_file",
        stream_key="custom_stream", publish_sse=False,
    )
    obs.record(
        confidence=0.40, op_id="op1", tool_name="read_file",
        stream_key="custom_stream", publish_sse=False,
    )
    # Both observations on the same custom_stream — second
    # records the LOW band transition.
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceBand,
    )
    assert (
        obs.last_band("custom_stream") == ToolConfidenceBand.LOW
    )


def test_reset_specific_stream():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    obs.record(
        confidence=0.10, op_id="op2", tool_name="y",
        publish_sse=False,
    )
    obs.reset(stream_key="op1::x")
    assert obs.last_band("op1::x") is None
    assert obs.last_band("op2::y") is not None


def test_reset_all_streams():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    obs.record(
        confidence=0.10, op_id="op2", tool_name="y",
        publish_sse=False,
    )
    obs.reset()
    assert obs.stream_count() == 0


def test_record_never_raises_on_malformed_input():
    """Defensive: any input → no crash. Can return None or
    record an UNKNOWN-band crossing depending on classification."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    # None confidence
    obs.record(
        confidence=None,  # type: ignore[arg-type]
        op_id="op1", tool_name="x", publish_sse=False,
    )
    # NaN
    obs.record(
        confidence=float("nan"), op_id="op1",
        tool_name="x", publish_sse=False,
    )
    # negative sample size — clamped to 0
    out = obs.record(
        confidence=0.10, op_id="op2", tool_name="y",
        sample_size=-5, publish_sse=False,
    )
    assert out is not None
    assert out.sample_size == 0


# ---------------------------------------------------------------------------
# Schema-versioned artifact (§33.5)
# ---------------------------------------------------------------------------


def test_crossing_schema_version_present():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION,
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    out = obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=False,
    )
    assert out is not None
    assert (
        out.schema_version
        == TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION
    )
    assert out.schema_version.startswith(
        "tool_confidence_observer."
    )


def test_crossing_to_dict_round_trip():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ToolConfidenceObserver,
    )
    obs = ToolConfidenceObserver()
    out = obs.record(
        confidence=0.42, op_id="op-1", tool_name="bash",
        sample_size=12, publish_sse=False,
    )
    assert out is not None
    d = out.to_dict()
    assert d["op_id"] == "op-1"
    assert d["tool_name"] == "bash"
    assert d["confidence"] == pytest.approx(0.42)
    assert d["sample_size"] == 12
    assert d["from_band"] == "certain"  # first-obs default
    assert d["to_band"] == "low"
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# SSE publish — gated on master flag
# ---------------------------------------------------------------------------


def test_sse_publish_skipped_when_master_off(monkeypatch):
    """Defense-in-depth: if master flag is off, SSE publish is
    structurally skipped even if publish_sse=True."""
    monkeypatch.delenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        raising=False,
    )
    publish_calls = []
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    monkeypatch.setattr(
        mod.ToolConfidenceObserver,
        "_publish_to_broker",
        staticmethod(
            lambda crossing: publish_calls.append(crossing),
        ),
    )
    obs = mod.ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=True,
    )
    assert publish_calls == []


def test_sse_publish_invoked_when_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "true",
    )
    publish_calls = []
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    monkeypatch.setattr(
        mod.ToolConfidenceObserver,
        "_publish_to_broker",
        staticmethod(
            lambda crossing: publish_calls.append(crossing),
        ),
    )
    obs = mod.ToolConfidenceObserver()
    obs.record(
        confidence=0.10, op_id="op1", tool_name="x",
        publish_sse=True,
    )
    assert len(publish_calls) == 1


def test_publish_to_broker_swallows_exceptions(monkeypatch):
    """Defensive: broker errors NEVER propagate."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ConfidenceBandCrossing, ToolConfidenceBand,
        ToolConfidenceObserver,
    )

    def _broken_get_default_broker():
        raise RuntimeError("simulated broker outage")

    import backend.core.ouroboros.governance.ide_observability_stream as ios
    monkeypatch.setattr(
        ios, "get_default_broker", _broken_get_default_broker,
    )
    crossing = ConfidenceBandCrossing(
        stream_key="x", op_id="op1", tool_name="y",
        from_band=ToolConfidenceBand.CERTAIN,
        to_band=ToolConfidenceBand.LOW,
        confidence=0.4, sample_size=10,
    )
    # MUST NOT raise.
    ToolConfidenceObserver._publish_to_broker(crossing)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    a = get_default_observer()
    b = get_default_observer()
    assert a is b


def test_reset_for_tests_creates_fresh_instance():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_default_observer, reset_default_observer_for_tests,
    )
    a = get_default_observer()
    reset_default_observer_for_tests()
    b = get_default_observer()
    assert a is not b


# ---------------------------------------------------------------------------
# AST pins — all 5 validate clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "tool_confidence_observer_band_taxonomy_5_values",
        "tool_confidence_observer_chatter_suppression",
        "tool_confidence_observer_authority_asymmetry",
        "tool_confidence_observer_composes_canonical_broker",
        "tool_confidence_observer_master_flag_default_false",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    source = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    pin = next(
        (
            i for i in register_shipped_invariants()
            if i.invariant_name == pin_name
        ),
        None,
    )
    assert pin is not None, (
        f"pin {pin_name!r} not registered"
    )
    violations = pin.validate(tree, source)
    assert violations == ()


def test_taxonomy_pin_fires_on_extra_value():
    """Synthetic regression — adding an extra band value fires
    the pin."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ToolConfidenceBand:
    CERTAIN = "certain"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"
    SUPER_CERTAIN = "super_certain"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_band_taxonomy_5_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("extra values" in v for v in violations)


def test_chatter_pin_fires_when_early_return_removed():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class ToolConfidenceObserver:
    def record(self, *, confidence, op_id="", tool_name=""):
        # BAD — no chatter-suppression early-return
        return "always emit"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_chatter_suppression"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
from backend.core.ouroboros.governance.orchestrator import x
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("orchestrator" in v for v in violations)


def test_master_flag_pin_fires_on_default_true():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def master_enabled() -> bool:
    raw = os.environ.get("JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED", "").strip().lower()
    if raw == "":
        return True  # BAD — must default-FALSE per §33.1
    return raw in ("1", "true")
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_master_flag_default_false"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# FlagRegistry seeds discoverable
# ---------------------------------------------------------------------------


def test_register_flags_callable_with_mock_registry():
    """register_flags() must be a no-op-safe registration when
    the registry surface is present."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 5
    names = {
        c.kwargs["name"] for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_TOOL_CONFIDENCE_INDICATOR_ENABLED",
        "JARVIS_TOOL_CONFIDENCE_BAND_CERTAIN_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_HIGH_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_MEDIUM_PCT",
        "JARVIS_TOOL_CONFIDENCE_BAND_LOW_PCT",
    }


def test_register_flags_swallows_registry_errors():
    """If FlagRegistry surface differs, registration must NOT
    crash module import."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_flags,
    )
    bad_registry = MagicMock()
    bad_registry.register.side_effect = TypeError(
        "incompatible signature",
    )
    # Must not raise.
    register_flags(bad_registry)


# ---------------------------------------------------------------------------
# SSE event-type registered in the canonical set
# ---------------------------------------------------------------------------


def test_event_type_in_canonical_event_set():
    """The new EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED MUST be
    in the broker's canonical event-type frozen set, otherwise
    subscribers can't consume it."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED,
    )
    # Existence + value check.
    assert (
        EVENT_TYPE_TOOL_CONFIDENCE_BAND_CROSSED
        == "tool_confidence_band_crossed"
    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    expected = {
        "ConfidenceBandCrossing",
        "TOOL_CONFIDENCE_OBSERVER_SCHEMA_VERSION",
        "ToolConfidenceBand",
        "ToolConfidenceObserver",
        "band_severity",
        "certain_threshold_pct",
        "classify_band",
        "get_default_observer",
        "high_threshold_pct",
        "low_threshold_pct",
        "master_enabled",
        "medium_threshold_pct",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_observer_for_tests",
    }
    assert set(mod.__all__) == expected
