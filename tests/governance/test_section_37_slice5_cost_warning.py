"""§37 Slice 5 — cost-band-crossing observer regression spine.

Pins per operator binding 2026-05-05:

  * Closed taxonomy: 5-value `CostBand` enum (OK / NOTICE /
    WARN / CRITICAL / BREACH); AST-pinned
  * Pure-function classifier: `classify_band(fraction)` returns
    correct band at every boundary; defensive on malformed input
  * Chatter-suppression structural: same-band ticks return None;
    AST-pinned via early-return check
  * First-observation discipline: streams that boot at OK don't
    emit spurious OK→OK; streams that boot at higher band DO
    emit immediately
  * Single-pipeline: SSE emission via `get_default_broker()` only;
    AST-pinned no parallel `StreamEventBroker()` construction
  * Authority asymmetry: substrate purity; AST-pinned
  * NEVER raises: every code path defensive
  * Status-line render path wires observer (single producer)
  * Status-line wiring is defensive (observer error doesn't
    break TUI)

Verifies (29 tests).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_observer():
    """Reset the singleton between tests."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_cost_band_taxonomy_exactly_5_values():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        CostBand,
    )
    values = {b.value for b in CostBand}
    assert values == {
        "ok", "notice", "warn", "critical", "breach",
    }


# ---------------------------------------------------------------------------
# Pure-function classifier — boundary cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fraction,expected", [
    (0.0, "ok"),
    (0.49, "ok"),
    (0.5, "notice"),    # exact threshold = NOTICE
    (0.7, "notice"),
    (0.79, "notice"),
    (0.8, "warn"),       # exact WARN threshold
    (0.94, "warn"),
    (0.95, "critical"),  # exact CRITICAL threshold
    (0.99, "critical"),
    (1.0, "breach"),     # exact BREACH threshold
    (1.5, "breach"),
])
def test_classify_band_boundaries(fraction, expected):
    from backend.core.ouroboros.governance.cost_warning_observer import (
        classify_band,
    )
    assert classify_band(fraction).value == expected


def test_classify_band_handles_negative():
    """Defensive: negative fraction (impossible in practice)
    treated as OK."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        classify_band,
    )
    assert classify_band(-0.5).value == "ok"


def test_classify_band_handles_nan():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        classify_band,
    )
    assert classify_band(float("nan")).value == "ok"


def test_classify_band_handles_non_numeric():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        classify_band,
    )
    # type: ignore intentional
    assert classify_band("not a number").value == "ok"  # type: ignore
    assert classify_band(None).value == "ok"  # type: ignore


def test_classify_band_caller_injected_thresholds():
    """Caller-injected thresholds override env defaults — used
    for testing band boundaries without env mocking."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        classify_band,
    )
    # Custom: notice=10 / warn=20 / critical=30
    assert classify_band(
        0.05, notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "ok"
    assert classify_band(
        0.15, notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "notice"
    assert classify_band(
        0.25, notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "warn"
    assert classify_band(
        0.35, notice_pct=10, warn_pct=20, critical_pct=30,
    ).value == "critical"


# ---------------------------------------------------------------------------
# Env-tunable thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_var,raw,expected,fn_name", [
    ("JARVIS_COST_WARN_BAND_NOTICE_PCT", "", 50,
     "notice_threshold_pct"),
    ("JARVIS_COST_WARN_BAND_NOTICE_PCT", "30", 30,
     "notice_threshold_pct"),
    ("JARVIS_COST_WARN_BAND_NOTICE_PCT", "0", 1,
     "notice_threshold_pct"),  # clamp low
    ("JARVIS_COST_WARN_BAND_NOTICE_PCT", "150", 99,
     "notice_threshold_pct"),  # clamp high
    ("JARVIS_COST_WARN_BAND_NOTICE_PCT", "garbage", 50,
     "notice_threshold_pct"),  # parse fail = default
    ("JARVIS_COST_WARN_BAND_WARN_PCT", "", 80,
     "warn_threshold_pct"),
    ("JARVIS_COST_WARN_BAND_CRITICAL_PCT", "", 95,
     "critical_threshold_pct"),
])
def test_threshold_env_vars(env_var, raw, expected, fn_name):
    from backend.core.ouroboros.governance import (
        cost_warning_observer as cwo,
    )
    fn = getattr(cwo, fn_name)
    with patch.dict(os.environ, {env_var: raw}):
        assert fn() == expected


# ---------------------------------------------------------------------------
# Chatter-suppression — same-band ticks return None
# ---------------------------------------------------------------------------


def test_same_band_returns_none():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    # First observation at NOTICE → emits
    c1 = obs.record(
        spent_usd=0.30, budget_usd=0.50, publish_sse=False,
    )
    assert c1 is not None
    # Same band → None
    for spent in [0.31, 0.32, 0.35, 0.39]:
        c = obs.record(
            spent_usd=spent, budget_usd=0.50, publish_sse=False,
        )
        assert c is None, (
            f"spent={spent} should be same NOTICE band"
        )


def test_band_crossing_emits():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    # OK → NOTICE
    c1 = obs.record(
        spent_usd=0.30, budget_usd=0.50, publish_sse=False,
    )
    assert c1 is not None
    assert c1.from_band.value == "ok"
    assert c1.to_band.value == "notice"
    # NOTICE → WARN
    c2 = obs.record(
        spent_usd=0.42, budget_usd=0.50, publish_sse=False,
    )
    assert c2 is not None
    assert c2.from_band.value == "notice"
    assert c2.to_band.value == "warn"
    # WARN → CRITICAL
    c3 = obs.record(
        spent_usd=0.475, budget_usd=0.50, publish_sse=False,
    )
    assert c3 is not None
    assert c3.from_band.value == "warn"
    assert c3.to_band.value == "critical"
    # CRITICAL → BREACH
    c4 = obs.record(
        spent_usd=0.51, budget_usd=0.50, publish_sse=False,
    )
    assert c4 is not None
    assert c4.from_band.value == "critical"
    assert c4.to_band.value == "breach"


# ---------------------------------------------------------------------------
# First-observation discipline
# ---------------------------------------------------------------------------


def test_first_observation_at_ok_does_not_emit():
    """A fresh stream that boots at OK should NOT emit a
    spurious OK→OK transition."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record(
        spent_usd=0.10, budget_usd=0.50, publish_sse=False,
    )
    assert c is None


def test_first_observation_at_higher_band_emits():
    """A fresh stream that boots AT a higher band (e.g.,
    session resumption) DOES emit so operator sees context
    immediately."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record(
        spent_usd=0.45, budget_usd=0.50, publish_sse=False,
    )
    assert c is not None
    assert c.from_band.value == "ok"
    assert c.to_band.value == "warn"


# ---------------------------------------------------------------------------
# Multiple stream-keys are independent
# ---------------------------------------------------------------------------


def test_independent_streams():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    # Stream "session" crosses to NOTICE
    c1 = obs.record(
        spent_usd=0.30, budget_usd=0.50,
        stream_key="session", publish_sse=False,
    )
    assert c1 is not None
    # Stream "op-1" is fresh — its first obs at NOTICE also
    # emits independently
    c2 = obs.record(
        spent_usd=0.06, budget_usd=0.10,
        stream_key="op-1", publish_sse=False,
    )
    assert c2 is not None
    # Same band on session → None
    c3 = obs.record(
        spent_usd=0.31, budget_usd=0.50,
        stream_key="session", publish_sse=False,
    )
    assert c3 is None


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_record_zero_budget_returns_none():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record(
        spent_usd=0.10, budget_usd=0.0, publish_sse=False,
    )
    assert c is None


def test_record_negative_budget_returns_none():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record(
        spent_usd=0.10, budget_usd=-1.0, publish_sse=False,
    )
    assert c is None


def test_record_non_numeric_returns_none():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    c = obs.record(
        spent_usd="not a number",  # type: ignore
        budget_usd=0.50,
        publish_sse=False,
    )
    assert c is None


def test_reset_clears_specific_stream():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        spent_usd=0.30, budget_usd=0.50,
        stream_key="session", publish_sse=False,
    )
    obs.record(
        spent_usd=0.06, budget_usd=0.10,
        stream_key="op-1", publish_sse=False,
    )
    obs.reset(stream_key="session")
    assert obs.last_band("session") is None
    assert obs.last_band("op-1") is not None


def test_reset_clears_all():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    obs = get_default_observer()
    obs.record(
        spent_usd=0.30, budget_usd=0.50,
        stream_key="session", publish_sse=False,
    )
    obs.record(
        spent_usd=0.06, budget_usd=0.10,
        stream_key="op-1", publish_sse=False,
    )
    obs.reset()
    assert obs.last_band("session") is None
    assert obs.last_band("op-1") is None


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_get_default_observer_returns_same_instance():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    a = get_default_observer()
    b = get_default_observer()
    assert a is b


# ---------------------------------------------------------------------------
# §33.5 versioned artifact
# ---------------------------------------------------------------------------


def test_band_crossing_to_dict_round_trip():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        BandCrossing, CostBand,
    )
    c = BandCrossing(
        stream_key="test",
        from_band=CostBand.NOTICE,
        to_band=CostBand.WARN,
        fraction=0.85,
        spent_usd=0.42,
        budget_usd=0.50,
    )
    d = c.to_dict()
    assert d["stream_key"] == "test"
    assert d["from_band"] == "notice"
    assert d["to_band"] == "warn"
    assert d["fraction"] == 0.85
    assert d["schema_version"] == "cost_warning_observer.1"


# ---------------------------------------------------------------------------
# SSE broker integration (Slice 2 territory)
# ---------------------------------------------------------------------------


def test_sse_event_type_in_broker_whitelist():
    """The event type the observer emits MUST be in the
    canonical whitelist; otherwise broker.publish silently
    drops it."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_COST_BAND_CROSSED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_COST_BAND_CROSSED in _VALID_EVENT_TYPES


def test_sse_emit_on_band_crossing(monkeypatch):
    """When publish_sse=True, the observer publishes the event
    to the canonical broker."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        get_default_observer,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker, reset_default_broker,
    )
    reset_default_broker()
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    obs = get_default_observer()
    # Trigger OK → NOTICE
    obs.record(
        spent_usd=0.30, budget_usd=0.50, publish_sse=True,
    )
    broker = get_default_broker()
    events = broker.recent_history(
        limit=10, event_type="cost_band_crossed",
    )
    assert len(events) == 1
    assert events[0].payload["from_band"] == "ok"
    assert events[0].payload["to_band"] == "notice"
    reset_default_broker()


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 4
    names = {i.invariant_name for i in invs}
    assert names == {
        "cost_warning_observer_band_taxonomy_5_values",
        "cost_warning_observer_chatter_suppression",
        "cost_warning_observer_authority_asymmetry",
        "cost_warning_observer_composes_canonical_broker",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance"
        / "cost_warning_observer.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_taxonomy_pin_fires_on_extra_value():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    bad_source = '''
import enum
class CostBand(str, enum.Enum):
    OK = "ok"
    NOTICE = "notice"
    WARN = "warn"
    CRITICAL = "critical"
    BREACH = "breach"
    EXTRA = "extra"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "band_taxonomy" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("EXTRA" in v for v in violations)


def test_chatter_suppression_pin_fires_on_missing_check():
    """Synthetic regression: if a future refactor drops the
    `if prev == new: return None` early-return, the pin fires."""
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    bad_source = '''
def record(self, *, spent, budget):
    new_band = classify_band(spent / budget)
    return new_band  # always emit — chatter-suppression dropped
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "chatter_suppression" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    bad_source = '''
from backend.core.ouroboros.governance.iron_gate import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


def test_composes_pin_fires_on_direct_broker_construction():
    from backend.core.ouroboros.governance.cost_warning_observer import (
        register_shipped_invariants,
    )
    bad_source = '''
def foo():
    return StreamEventBroker()
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "composes_canonical_broker" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


# ---------------------------------------------------------------------------
# Status-line wiring — observer fires from snapshot()
# ---------------------------------------------------------------------------


def test_status_line_wires_observer():
    """The status-line render path MUST call the observer.
    AST regression — find `get_default_observer()` reference
    in status_line.py source."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/status_line.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "get_default_observer" in source, (
        "status_line.py MUST wire cost_warning_observer "
        "(§37 Slice 5 regression)"
    )
    # And the call must be inside snapshot() — find the function
    tree = ast.parse(source)
    snapshot_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "snapshot":
                snapshot_fn = node
                break
    assert snapshot_fn is not None
    # Walk snapshot fn body for the observer call
    found = False
    for sub in ast.walk(snapshot_fn):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Name)
                and func.id == "get_default_observer"
            ):
                found = True
                break
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "record"
            ):
                # Could be obs.record(...) — accept
                receiver = func.value
                if isinstance(receiver, ast.Call):
                    inner_func = receiver.func
                    if (
                        isinstance(inner_func, ast.Name)
                        and inner_func.id == "get_default_observer"
                    ):
                        found = True
                        break
    assert found, (
        "snapshot() MUST call get_default_observer().record(...)"
    )


def test_status_line_wiring_is_defensive():
    """The status-line wiring MUST be wrapped in a try/except
    so observer errors don't break the TUI."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/status_line.py"
    )
    source = target.read_text(encoding="utf-8")
    # Find the cost_warning_observer mention; verify it's
    # within a try/except block
    tree = ast.parse(source)
    snapshot_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "snapshot":
                snapshot_fn = node
                break
    assert snapshot_fn is not None
    # Walk Try nodes
    has_defensive_try = False
    for sub in ast.walk(snapshot_fn):
        if not isinstance(sub, ast.Try):
            continue
        # Check try body for observer reference
        for body_stmt in sub.body:
            for inner in ast.walk(body_stmt):
                if isinstance(inner, ast.ImportFrom):
                    if (
                        inner.module
                        and "cost_warning_observer" in inner.module  # noqa: E501
                    ):
                        has_defensive_try = True
                        break
    assert has_defensive_try, (
        "status_line.py snapshot() MUST wrap observer call in "
        "try/except (defensive — observer error must not "
        "break TUI)"
    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import cost_warning_observer
    expected = {
        "BandCrossing",
        "COST_WARNING_OBSERVER_SCHEMA_VERSION",
        "CostBand",
        "CostWarningObserver",
        "classify_band",
        "critical_threshold_pct",
        "get_default_observer",
        "notice_threshold_pct",
        "register_shipped_invariants",
        "reset_default_observer_for_tests",
        "warn_threshold_pct",
    }
    assert set(cost_warning_observer.__all__) == expected
