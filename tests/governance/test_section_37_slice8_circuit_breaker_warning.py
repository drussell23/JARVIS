"""§37 Slice 8 — circuit-breaker approach-to-trip observer regression.

Pins per operator binding 2026-05-05:

  * Reuses Slice 5 CostBand 5-value taxonomy (no parallel
    enum); AST-pinned
  * Pure-function classifier handles boundaries + defensive
    paths
  * Chatter-suppression structural via same-band early-return
  * First-observation discipline (fresh OK doesn't emit
    spurious OK→OK)
  * Multi-breaker-id independence
  * Composes canonical SSE broker (Slice 2 territory)
  * AST-pinned authority asymmetry
  * NEVER raises
  * CircuitBreaker.record_failure wired to observer

Verifies (28 tests).
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
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Reuses Slice 5 CostBand taxonomy
# ---------------------------------------------------------------------------


def test_imports_cost_band_from_slice5():
    """Module MUST import CostBand from cost_warning_observer
    — no parallel taxonomy."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        CostBand as Slice5CostBand,
    )
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    band = classify_breaker_band(2, 3)
    assert isinstance(band, Slice5CostBand)


# ---------------------------------------------------------------------------
# Pure-function classifier — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fc,thr,expected", [
    (0, 3, "ok"),       # zero failures = OK
    (1, 3, "notice"),   # 33% — exact NOTICE threshold
    (2, 3, "warn"),     # 66% — exact WARN threshold
    (3, 3, "breach"),   # 100% — BREACH at threshold
    (5, 3, "breach"),   # over threshold — still BREACH
    (1, 10, "ok"),      # 10% < 33% notice
    (4, 10, "notice"),  # 40% — at NOTICE band
    (7, 10, "warn"),    # 70% — at WARN band
    (9, 10, "critical"),  # 90% — at CRITICAL band
])
def test_classify_breaker_band_boundaries(fc, thr, expected):
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    assert classify_breaker_band(fc, thr).value == expected


def test_classify_handles_zero_threshold():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    assert classify_breaker_band(5, 0).value == "ok"


def test_classify_handles_negative():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    assert classify_breaker_band(-5, 3).value == "ok"


def test_classify_handles_non_numeric():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    assert classify_breaker_band("x", 3).value == "ok"  # type: ignore
    assert classify_breaker_band(2, None).value == "ok"  # type: ignore


def test_classify_caller_injected_thresholds():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        classify_breaker_band,
    )
    # Custom: notice=10 / warn=20 / critical=30
    assert classify_breaker_band(
        1, 10,
        notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "notice"
    assert classify_breaker_band(
        2, 10,
        notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "warn"
    assert classify_breaker_band(
        3, 10,
        notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "critical"


# ---------------------------------------------------------------------------
# Chatter-suppression — same-band ticks return None
# ---------------------------------------------------------------------------


def test_same_band_returns_none():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    # First emit at NOTICE
    c1 = obs.record_failure(
        breaker_id="x", failure_count=1, threshold=3,
        publish_sse=False,
    )
    assert c1 is not None
    # Same NOTICE band → no emission
    c2 = obs.record_failure(
        breaker_id="x", failure_count=1, threshold=3,
        publish_sse=False,
    )
    assert c2 is None


def test_band_crossing_emits_full_ladder():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    # OK → NOTICE
    c1 = obs.record_failure(
        breaker_id="x", failure_count=4, threshold=10,
        publish_sse=False,
    )
    assert c1 is not None
    assert c1.from_band.value == "ok"
    assert c1.to_band.value == "notice"
    # NOTICE → WARN
    c2 = obs.record_failure(
        breaker_id="x", failure_count=7, threshold=10,
        publish_sse=False,
    )
    assert c2 is not None
    assert c2.to_band.value == "warn"
    # WARN → CRITICAL
    c3 = obs.record_failure(
        breaker_id="x", failure_count=9, threshold=10,
        publish_sse=False,
    )
    assert c3 is not None
    assert c3.to_band.value == "critical"
    # CRITICAL → BREACH
    c4 = obs.record_failure(
        breaker_id="x", failure_count=10, threshold=10,
        publish_sse=False,
    )
    assert c4 is not None
    assert c4.to_band.value == "breach"


# ---------------------------------------------------------------------------
# First-observation discipline
# ---------------------------------------------------------------------------


def test_first_observation_at_ok_does_not_emit():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record_failure(
        breaker_id="fresh", failure_count=0, threshold=3,
        publish_sse=False,
    )
    assert c is None


def test_first_observation_at_higher_band_emits():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record_failure(
        breaker_id="resumed", failure_count=2, threshold=3,
        publish_sse=False,
    )
    assert c is not None
    assert c.from_band.value == "ok"
    assert c.to_band.value == "warn"


# ---------------------------------------------------------------------------
# Multi-breaker independence
# ---------------------------------------------------------------------------


def test_multiple_breaker_ids_independent():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    c1 = obs.record_failure(
        breaker_id="claude", failure_count=1, threshold=3,
        publish_sse=False,
    )
    c2 = obs.record_failure(
        breaker_id="dw", failure_count=1, threshold=3,
        publish_sse=False,
    )
    assert c1 is not None
    assert c2 is not None
    # Same band on claude → None
    c3 = obs.record_failure(
        breaker_id="claude", failure_count=1, threshold=3,
        publish_sse=False,
    )
    assert c3 is None


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_zero_threshold_returns_none():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record_failure(
        breaker_id="x", failure_count=5, threshold=0,
        publish_sse=False,
    )
    assert c is None


def test_non_numeric_returns_none():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record_failure(
        breaker_id="x",
        failure_count="bad",  # type: ignore
        threshold=3,
        publish_sse=False,
    )
    assert c is None


def test_reset_clears_specific_breaker():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record_failure(
        breaker_id="a", failure_count=1, threshold=3,
        publish_sse=False,
    )
    obs.record_failure(
        breaker_id="b", failure_count=1, threshold=3,
        publish_sse=False,
    )
    obs.reset(breaker_id="a")
    assert obs.last_band("a") is None
    assert obs.last_band("b") is not None


def test_reset_clears_all():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record_failure(
        breaker_id="a", failure_count=1, threshold=3,
        publish_sse=False,
    )
    obs.reset()
    assert obs.last_band("a") is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    a = get_default_observer()
    b = get_default_observer()
    assert a is b


# ---------------------------------------------------------------------------
# §33.5 versioned artifact
# ---------------------------------------------------------------------------


def test_breaker_band_crossing_to_dict():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        BreakerBandCrossing,
    )
    from backend.core.ouroboros.governance.cost_warning_observer import (
        CostBand,
    )
    c = BreakerBandCrossing(
        breaker_id="test",
        from_band=CostBand.NOTICE,
        to_band=CostBand.WARN,
        failure_count=2,
        threshold=3,
        ratio=0.667,
    )
    d = c.to_dict()
    assert d["breaker_id"] == "test"
    assert d["from_band"] == "notice"
    assert d["to_band"] == "warn"
    assert d["failure_count"] == 2
    assert d["threshold"] == 3
    assert (
        d["schema_version"]
        == "circuit_breaker_warning_observer.1"
    )


# ---------------------------------------------------------------------------
# SSE broker integration
# ---------------------------------------------------------------------------


def test_sse_event_in_broker_whitelist():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING,
        _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING
        in _VALID_EVENT_TYPES
    )


def test_sse_emit_on_band_crossing(monkeypatch):
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker, reset_default_broker,
    )
    reset_default_broker()
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    obs = get_default_observer()
    obs.record_failure(
        breaker_id="claude_circuit_breaker",
        failure_count=1,
        threshold=3,
        publish_sse=True,
    )
    broker = get_default_broker()
    events = broker.recent_history(
        limit=10,
        event_type="circuit_breaker_approaching",
    )
    assert len(events) == 1
    assert (
        events[0].payload["breaker_id"]
        == "claude_circuit_breaker"
    )
    assert events[0].payload["from_band"] == "ok"
    assert events[0].payload["to_band"] == "notice"
    reset_default_broker()


# ---------------------------------------------------------------------------
# CircuitBreaker integration
# ---------------------------------------------------------------------------


def test_circuit_breaker_calls_observer_on_record_failure():
    """When CircuitBreaker.record_failure() is called, the
    canonical observer's record_failure MUST be called too."""
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker,
    )
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        get_default_observer,
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_timeout_s=30.0,
    )
    breaker.record_failure()  # 1/3 → NOTICE
    obs = get_default_observer()
    last = obs.last_band(
        breaker_id=getattr(
            breaker, "_breaker_id", "circuit_breaker",
        ),
    )
    assert last is not None
    assert last.value == "notice"


def test_circuit_breaker_observer_wiring_is_defensive():
    """If the observer raises (boot race / etc.), the
    CircuitBreaker MUST NOT break."""
    from backend.core.ouroboros.governance.rate_limiter import (
        CircuitBreaker,
    )
    breaker = CircuitBreaker(failure_threshold=3)
    with patch(
        "backend.core.ouroboros.governance."
        "circuit_breaker_warning_observer."
        "get_default_observer",
        side_effect=RuntimeError("boot race"),
    ):
        # Should NOT raise
        breaker.record_failure()
    assert breaker.state.value == "CLOSED"


def test_rate_limiter_source_imports_observer():
    """AST regression: rate_limiter.py MUST import the
    canonical observer for the wiring."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/rate_limiter.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "circuit_breaker_warning_observer" in source, (
        "rate_limiter.py MUST wire the observer via canonical "
        "import (§37 Slice 8 regression)"
    )


def test_rate_limiter_wiring_is_defensive():
    """The observer call in CircuitBreaker.record_failure MUST
    be wrapped in try/except."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/rate_limiter.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    has_defensive_try = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for body_stmt in node.body:
            for inner in ast.walk(body_stmt):
                if isinstance(inner, ast.ImportFrom):
                    if (
                        inner.module
                        and "circuit_breaker_warning_observer"
                        in inner.module
                    ):
                        has_defensive_try = True
                        break
    assert has_defensive_try, (
        "rate_limiter.py MUST wrap observer call in "
        "try/except (defensive)"
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 4
    names = {i.invariant_name for i in invs}
    assert names == {
        "circuit_breaker_warning_observer_chatter_suppression",
        "circuit_breaker_warning_observer_authority_asymmetry",
        "circuit_breaker_warning_observer_composes_canonical_broker",  # noqa: E501
        "circuit_breaker_warning_observer_reuses_cost_band_taxonomy",  # noqa: E501
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance"
        / "circuit_breaker_warning_observer.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_reuses_cost_band_pin_fires_on_parallel_taxonomy():
    """Synthetic regression: if a future refactor defines a
    parallel CostBand class, the pin fires."""
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad_source = '''
import enum
class CostBand(str, enum.Enum):
    OK = "ok"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "reuses_cost_band" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


def test_chatter_pin_fires_on_missing_check():
    from backend.core.ouroboros.governance.circuit_breaker_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad_source = '''
def record_failure(self, *, breaker_id, failure_count, threshold):
    return classify_breaker_band(failure_count, threshold)
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "chatter_suppression" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        circuit_breaker_warning_observer as cbwo,
    )
    expected = {
        "BreakerBandCrossing",
        "CIRCUIT_BREAKER_WARNING_OBSERVER_SCHEMA_VERSION",
        "CircuitBreakerWarningObserver",
        "classify_breaker_band",
        "critical_threshold_pct",
        "get_default_observer",
        "notice_threshold_pct",
        "register_shipped_invariants",
        "reset_default_observer_for_tests",
        "warn_threshold_pct",
    }
    assert set(cbwo.__all__) == expected
