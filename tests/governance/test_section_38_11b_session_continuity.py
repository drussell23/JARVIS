"""Section 38.11-B (PRD v2.65 to v2.66, 2026-05-07) -
session continuity (graduation ticker + cross-session memory diff)
regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11b(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", raising=False,
    )
    for sub in (
        "JARVIS_SESSION_CONTINUITY_TICKER_ENABLED",
        "JARVIS_SESSION_CONTINUITY_MEMORY_DIFF_ENABLED",
    ):
        monkeypatch.delenv(sub, raising=False)
    from backend.core.ouroboros.governance import (
        session_continuity as sc,
    )
    sc.reset_ticker_for_tests()
    yield
    sc.reset_ticker_for_tests()


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.session_continuity import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.session_continuity import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_SESSION_CONTINUITY_ENABLED", value)
    assert master_enabled() is True


# Closed taxonomies


def test_transition_taxonomy_4_values():
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTransition,
    )
    assert {m.name for m in GraduationTransition} == {
        "BECAME_READY", "BACKED_OFF", "UNCHANGED", "NEW",
    }


# GraduationTicker


def test_ticker_master_off_returns_empty():
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker,
    )
    t = GraduationTicker()
    assert t.tick() == ()


def test_ticker_first_tick_with_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker, GraduationTransition,
    )
    t = GraduationTicker()
    events = t.tick()
    # First tick — could include NEW transitions for any
    # already-READY flags. The exact count depends on
    # graduation_ledger state, so we just assert shape.
    for ev in events:
        assert isinstance(ev.transition, GraduationTransition)
        assert ev.flag_name


def test_ticker_second_tick_steady_state(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker, GraduationTransition,
    )
    t = GraduationTicker()
    t.tick()  # warm
    events = t.tick()
    # Second tick on stable state — only meaningful transitions
    # (BECAME_READY / BACKED_OFF / NEW); UNCHANGED is NOT in
    # the returned tuple. So most flags should be silent.
    # With no real-world transitions between ticks, expect 0
    # or very few events.
    for ev in events:
        assert ev.transition in (
            GraduationTransition.BECAME_READY,
            GraduationTransition.BACKED_OFF,
            GraduationTransition.NEW,
        )


def test_ticker_classify_transition_taxonomy(monkeypatch):
    """Verify the transition classification is structurally
    correct for each taxonomy value."""
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker, GraduationTransition,
    )
    t = GraduationTicker()
    # NEW: previous=None, current=ready
    assert t._classify_transition(
        previous=None, current="ready",
    ) == GraduationTransition.NEW
    # NEW non-ready: classify as UNCHANGED (no transition)
    assert t._classify_transition(
        previous=None, current="evidence_gathering",
    ) == GraduationTransition.UNCHANGED
    # BECAME_READY: previous=evidence_gathering, current=ready
    assert t._classify_transition(
        previous="evidence_gathering", current="ready",
    ) == GraduationTransition.BECAME_READY
    # BACKED_OFF: previous=ready, current=evidence_failed
    assert t._classify_transition(
        previous="ready", current="evidence_failed",
    ) == GraduationTransition.BACKED_OFF
    # UNCHANGED: same value
    assert t._classify_transition(
        previous="ready", current="ready",
    ) == GraduationTransition.UNCHANGED
    assert t._classify_transition(
        previous="evidence_gathering",
        current="evidence_gathering",
    ) == GraduationTransition.UNCHANGED


def test_ticker_history_bounded():
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker, GraduationEvent,
        GraduationTransition,
    )
    t = GraduationTicker()
    # Inject more events than _MAX_HISTORY directly via
    # private state (tests-only).
    for i in range(80):
        t._history.append(GraduationEvent(
            flag_name=f"flag_{i}",
            transition=GraduationTransition.BECAME_READY,
        ))
    # Manually clamp like ticker logic would; verify history
    # accessor respects max history cap.
    h = t.history(limit=100)
    assert len(h) <= t._MAX_HISTORY


def test_ticker_singleton(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        get_default_ticker,
    )
    t1 = get_default_ticker()
    t2 = get_default_ticker()
    assert t1 is t2


# format_graduation_ticker


def test_format_ticker_master_off():
    from backend.core.ouroboros.governance.session_continuity import (
        format_graduation_ticker,
    )
    assert format_graduation_ticker() == ""


def test_format_ticker_with_no_transitions(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        format_graduation_ticker,
    )
    # Pass empty transitions explicitly.
    assert format_graduation_ticker(transitions=()) == ""


def test_format_ticker_with_meaningful_transitions(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        format_graduation_ticker, GraduationEvent,
        GraduationTransition,
    )
    transitions = (
        GraduationEvent(
            flag_name="JARVIS_TEST_FLAG",
            transition=GraduationTransition.BECAME_READY,
            previous_verdict="evidence_gathering",
            current_verdict="ready",
            diagnostic="clean=3/3 runner=0",
        ),
    )
    out = format_graduation_ticker(transitions=transitions)
    assert "JARVIS_TEST_FLAG" in out
    assert "READY" in out
    assert "✨" in out


def test_format_ticker_backed_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        format_graduation_ticker, GraduationEvent,
        GraduationTransition,
    )
    transitions = (
        GraduationEvent(
            flag_name="JARVIS_BACKOFF_FLAG",
            transition=GraduationTransition.BACKED_OFF,
            previous_verdict="ready",
            current_verdict="evidence_failed",
            diagnostic="runner failure",
        ),
    )
    out = format_graduation_ticker(transitions=transitions)
    assert "backed off" in out
    assert "⚠" in out


# CrossSessionDiff


def test_aggregate_diff_master_off_empty():
    from backend.core.ouroboros.governance.session_continuity import (
        aggregate_cross_session_diff,
    )
    diff = aggregate_cross_session_diff()
    assert diff.has_previous is False


def test_aggregate_diff_with_real_repo_state(monkeypatch):
    """Master-on integration with real LSS — accept either
    outcome (env may have prior bt-* dirs); assert shape
    invariants on the artifact instead of exact values."""
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        aggregate_cross_session_diff, CrossSessionDiff,
    )
    diff = aggregate_cross_session_diff()
    assert isinstance(diff, CrossSessionDiff)
    if diff.has_previous:
        assert diff.previous_session_id
        assert diff.previous_attempted >= 0
        assert diff.previous_cost_total >= 0.0


def test_format_diff_no_previous_returns_empty():
    from backend.core.ouroboros.governance.session_continuity import (
        format_cross_session_diff, CrossSessionDiff,
    )
    d = CrossSessionDiff(has_previous=False)
    assert format_cross_session_diff(d) == ""


def test_format_diff_with_previous(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        format_cross_session_diff, CrossSessionDiff,
    )
    d = CrossSessionDiff(
        has_previous=True,
        previous_session_id="bt-2026-05-07-120000",
        previous_attempted=23,
        previous_completed=20,
        previous_failed=3,
        previous_cost_total=0.12,
        previous_duration_s=2400.0,
        previous_stop_reason="idle_timeout",
    )
    out = format_cross_session_diff(d)
    assert "Since last session" in out
    assert "23 ops attempted" in out
    assert "20 completed" in out
    assert "3 failed" in out
    assert "$0.12 spent" in out
    assert "idle_timeout" in out


# Composite panel


def test_composite_panel_master_off():
    from backend.core.ouroboros.governance.session_continuity import (
        format_session_continuity_panel,
    )
    assert format_session_continuity_panel() == ""


# Sub-flag granularity


def test_sub_flag_disables_ticker(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_TICKER_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        GraduationTicker, format_graduation_ticker,
    )
    assert GraduationTicker().tick() == ()
    assert format_graduation_ticker() == ""


def test_sub_flag_disables_diff(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_MEMORY_DIFF_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.session_continuity import (
        aggregate_cross_session_diff, format_cross_session_diff,
    )
    d = aggregate_cross_session_diff()
    assert d.has_previous is False
    assert format_cross_session_diff(d) == ""


# /continuity REPL


def test_continuity_repl_unmatched():
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/something_else")
    assert r.matched is False


def test_continuity_repl_help_master_off():
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity help")
    assert r.ok is True
    assert "session continuity" in r.text.lower()


def test_continuity_repl_panel_master_off_blocks():
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity panel")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_continuity_repl_panel_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity panel")
    assert r.ok is True


def test_continuity_repl_diff(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity diff")
    assert r.ok is True


def test_continuity_repl_ticker(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity ticker")
    assert r.ok is True


def test_continuity_repl_history(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity history 5")
    assert r.ok is True


def test_continuity_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_continuity_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_CONTINUITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.continuity_repl import (
        dispatch_continuity_command,
    )
    r = dispatch_continuity_command("/continuity gibberish")
    assert r.ok is False


# AST pins


def _continuity_pins():
    from backend.core.ouroboros.governance.session_continuity import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _continuity_source():
    return Path(
        "backend/core/ouroboros/governance/session_continuity.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _continuity_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _continuity_pins()
    src = _continuity_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires():
    pins = _continuity_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires():
    pins = _continuity_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_taxonomy_fires_on_missing():
    pins = _continuity_pins()
    pin = next(
        p for p in pins
        if "transition_taxonomy_4_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class GraduationTransition(str, enum.Enum):\n"
        "    NEW = 'new'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_graduation_dashboard_fires():
    pins = _continuity_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_graduation_dashboard"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_last_session_summary_fires():
    pins = _continuity_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_last_session_summary"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.session_continuity import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 2 sub-flags.
    assert n == 3


# Composition + SSE


def test_canonical_unified_graduation_dashboard_importable():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (
        aggregate_dashboard,
    )
    assert callable(aggregate_dashboard)


def test_canonical_last_session_summary_importable():
    from backend.core.ouroboros.governance.last_session_summary import (
        get_default_summary, LastSessionSummary,
    )
    assert callable(get_default_summary)
    assert LastSessionSummary is not None


def test_canonical_event_type_flag_graduated_registered():
    """The new SSE event type MUST be in canonical
    _VALID_EVENT_TYPES frozenset."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES, EVENT_TYPE_FLAG_GRADUATED,
    )
    assert EVENT_TYPE_FLAG_GRADUATED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_FLAG_GRADUATED == "flag_graduated"
