"""§37 Slice 3 — `/why_changed` REPL regression spine.

Pins per operator binding 2026-05-05:

  * Single pipeline: composes `get_default_engine()` singleton
    only; never constructs `AutonomyFeedbackEngine()` directly
  * Authority asymmetry: read-only operator surface; never calls
    mutating engine methods
  * Substrate purity: no orchestrator / iron_gate / policy /
    providers / candidate_generator imports
  * Auto-discovery: `dispatch_why_changed_command` matches §32.11
    Slice 4 naming-cage; `register_verbs` matches help-discovery
    contract
  * NEVER raises: every code path defensive
  * Public read API extension on `AutonomyFeedbackEngine`:
    `rollback_counts_snapshot` / `brain_hint_threshold` /
    `seen_files_snapshot` / `brains_at_threshold` return
    defensive snapshots; lock-free; NEVER raise

Verifies (32 tests).
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_engine():
    """Reset the engine singleton between tests."""
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
        reset_default_engine_for_tests,
    )
    reset_default_engine_for_tests()
    yield
    reset_default_engine_for_tests()


def _make_engine(*, threshold: int = 3):
    """Helper: construct + register a fresh engine in a tmp dir."""
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
        AutonomyFeedbackEngine, FeedbackEngineConfig,
    )
    from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
        CommandBus,
    )
    td = tempfile.mkdtemp()
    config = FeedbackEngineConfig(
        event_dir=Path(td), state_dir=Path(td),
    )
    bus = CommandBus()
    engine = AutonomyFeedbackEngine(
        command_bus=bus, config=config,
    )
    engine._brain_hint_threshold = threshold
    return engine


# ---------------------------------------------------------------------------
# Match + dispatch shape
# ---------------------------------------------------------------------------


def test_dispatch_matches_canonical_forms():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    for line in (
        "/why_changed",
        "why_changed",
        "/why_changed help",
        "why_changed brains",
    ):
        result = dispatch_why_changed_command(line)
        assert result.matched is True


def test_dispatch_does_not_match_other_verbs():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    for line in (
        "/posture",
        "/why-changed",  # hyphen form, not the canonical
        "/why_change",   # similar prefix, distinct verb
        "",
        "   ",
    ):
        result = dispatch_why_changed_command(line)
        assert result.matched is False


def test_help_bypasses_master():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed help")
    assert result.ok is True
    assert "/why_changed" in result.text
    assert "brains" in result.text
    assert "at_threshold" in result.text


def test_short_help_alias():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed ?")
    assert result.ok is True


# ---------------------------------------------------------------------------
# Cold-start (no engine registered) — honest "not booted" rendering
# ---------------------------------------------------------------------------


def test_overview_no_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed")
    assert result.ok is True
    assert "Feedback Engine" in result.text
    assert "No engine registered yet" in result.text


def test_brains_no_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed brains")
    assert "No engine registered" in result.text


def test_at_threshold_no_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command(
        "/why_changed at_threshold",
    )
    assert "No engine registered" in result.text


def test_files_no_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed files")
    assert "No engine registered" in result.text


def test_config_no_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed config")
    assert "No engine registered" in result.text


# ---------------------------------------------------------------------------
# Populated engine — end-to-end
# ---------------------------------------------------------------------------


def test_overview_populated_engine():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    engine = _make_engine(threshold=3)
    # Simulate accumulated rollbacks
    engine._rollback_counts["claude"] = 1
    engine._rollback_counts["dw_397b"] = 4  # over threshold
    result = dispatch_why_changed_command("/why_changed")
    assert result.ok is True
    assert "brains_tracked" in result.text
    assert "dw_397b" in result.text  # at-threshold brain visible
    assert "Brains at hint threshold" in result.text


def test_overview_idle_engine():
    """Engine constructed but no rollbacks / no signals → idle
    state rendered honestly."""
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    _make_engine()
    result = dispatch_why_changed_command("/why_changed")
    assert result.ok is True
    assert "idle" in result.text.lower() or (
        "No brains at hint threshold" in result.text
    )


def test_brains_renders_per_brain_counts():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    engine = _make_engine(threshold=3)
    engine._rollback_counts["alpha"] = 1
    engine._rollback_counts["bravo"] = 5
    engine._rollback_counts["charlie"] = 0
    result = dispatch_why_changed_command("/why_changed brains")
    assert "alpha" in result.text
    assert "bravo" in result.text
    # Sorted by count desc → bravo (5) before alpha (1) before charlie (0)
    bravo_idx = result.text.index("bravo")
    alpha_idx = result.text.index("alpha")
    assert bravo_idx < alpha_idx


def test_brains_no_counts_renders_empty_message():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    _make_engine()
    result = dispatch_why_changed_command("/why_changed brains")
    assert "No rollback counts recorded" in result.text


def test_at_threshold_filters_correctly():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    engine = _make_engine(threshold=3)
    engine._rollback_counts["below"] = 1
    engine._rollback_counts["at"] = 3
    engine._rollback_counts["over"] = 5
    result = dispatch_why_changed_command(
        "/why_changed at_threshold",
    )
    assert "at" in result.text
    assert "over" in result.text
    # Brain-id "below" should NOT show
    # Use word-boundary check since "below" is a substring of nothing else
    # in our render. Defensive: just check the `below` brain entry isn't
    # in the threshold-filtered list (it might appear elsewhere as text).
    # Simplest: the brain marker `● below` shouldn't show.
    assert "● below" not in result.text


def test_at_threshold_empty_when_none_breaching():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    engine = _make_engine(threshold=5)
    engine._rollback_counts["safe"] = 1
    result = dispatch_why_changed_command(
        "/why_changed at_threshold",
    )
    assert (
        "No brains at hint threshold" in result.text
        or "All brains within tolerance" in result.text
    )


def test_files_renders_seen_files():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    engine = _make_engine()
    engine._seen_files = {
        "curriculum_001.json",
        "reactor_002.json",
        "curriculum_003.json",
    }
    result = dispatch_why_changed_command("/why_changed files")
    assert result.ok is True
    assert "curriculum_001.json" in result.text
    assert "reactor_002.json" in result.text


def test_files_empty_renders_no_files_message():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    _make_engine()
    result = dispatch_why_changed_command("/why_changed files")
    assert "No curriculum/reactor signal files" in result.text


def test_files_limit_clamping():
    from backend.core.ouroboros.governance.why_changed_repl import (
        _parse_limit,
    )
    assert _parse_limit([]) == 10
    assert _parse_limit(["5"]) == 5
    assert _parse_limit(["0"]) == 1
    assert _parse_limit(["999"]) == 200
    assert _parse_limit(["garbage"]) == 10


def test_config_renders_engine_knobs():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    _make_engine(threshold=7)
    result = dispatch_why_changed_command("/why_changed config")
    assert result.ok is True
    assert "Engine Config" in result.text
    assert "brain_hint_threshold" in result.text
    assert "7" in result.text


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_first_engine_registered_as_singleton():
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
        get_default_engine,
    )
    assert get_default_engine() is None
    engine = _make_engine()
    assert get_default_engine() is engine


def test_second_engine_does_not_override_singleton():
    """First-engine-wins semantics so test fixtures don't
    silently shadow the production singleton."""
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
        get_default_engine,
    )
    first = _make_engine()
    second = _make_engine()  # noqa: F841
    assert get_default_engine() is first


def test_reset_clears_singleton():
    from backend.core.ouroboros.governance.autonomy.feedback_engine import (  # noqa: E501
        get_default_engine,
        reset_default_engine_for_tests,
    )
    _make_engine()
    assert get_default_engine() is not None
    reset_default_engine_for_tests()
    assert get_default_engine() is None


# ---------------------------------------------------------------------------
# Engine read API (defensive snapshot helpers)
# ---------------------------------------------------------------------------


def test_rollback_counts_snapshot_is_defensive_copy():
    engine = _make_engine()
    engine._rollback_counts["x"] = 5
    snap = engine.rollback_counts_snapshot()
    snap["x"] = 999
    snap["y"] = 99
    # Engine's internal state unchanged
    assert engine._rollback_counts["x"] == 5
    assert "y" not in engine._rollback_counts


def test_seen_files_snapshot_is_defensive_copy():
    engine = _make_engine()
    engine._seen_files = {"a.json", "b.json"}
    snap = engine.seen_files_snapshot()
    snap.append("c.json")
    assert "c.json" not in engine._seen_files


def test_seen_files_snapshot_sorted():
    engine = _make_engine()
    engine._seen_files = {"zulu.json", "alpha.json", "mike.json"}
    snap = engine.seen_files_snapshot()
    assert snap == ["alpha.json", "mike.json", "zulu.json"]


def test_brains_at_threshold_sorted_by_count_desc():
    engine = _make_engine(threshold=3)
    engine._rollback_counts["one"] = 3
    engine._rollback_counts["two"] = 5
    engine._rollback_counts["three"] = 4
    engine._rollback_counts["safe"] = 1
    at_threshold = engine.brains_at_threshold()
    # Sorted by count desc → two (5), three (4), one (3); safe excluded
    assert at_threshold == ["two", "three", "one"]


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_unknown_subcommand_returns_clean_error():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command("/why_changed wat")
    assert result.matched is True
    assert result.ok is False
    assert "unknown subcommand" in result.text.lower()


def test_shlex_parse_error_does_not_raise():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    result = dispatch_why_changed_command(
        '/why_changed "unclosed',
    )
    assert result.matched is True
    assert result.ok is False
    assert "parse error" in result.text


def test_engine_access_failure_returns_clean_error():
    from backend.core.ouroboros.governance.why_changed_repl import (
        dispatch_why_changed_command,
    )
    _make_engine()  # register a real engine first
    # Patch the get_default_engine accessor to raise
    with patch(
        "backend.core.ouroboros.governance.autonomy."
        "feedback_engine.get_default_engine",
        side_effect=RuntimeError("boot race"),
    ):
        result = dispatch_why_changed_command("/why_changed")
    # Defensive accessor catches the exception → returns None
    # → renders honest "no engine" message instead of error.
    # OR returns error envelope. Either way: NEVER raises.
    assert result.matched is True


# ---------------------------------------------------------------------------
# Auto-discovery hooks
# ---------------------------------------------------------------------------


def test_dispatch_function_naming_matches_cage():
    """§32.11 Slice 4 naming cage: file ends `_repl.py` → verb
    `why_changed` → dispatcher must be
    `dispatch_why_changed_command`."""
    from backend.core.ouroboros.governance import why_changed_repl
    assert hasattr(
        why_changed_repl, "dispatch_why_changed_command",
    )
    import inspect
    sig = inspect.signature(
        why_changed_repl.dispatch_why_changed_command,
    )
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"


def test_register_verbs_returns_count():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_verbs,
    )

    class FakeRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = FakeRegistry()
    n = register_verbs(reg)
    assert n == 1
    assert len(reg.calls) == 1
    assert reg.calls[0]["verb"] == "why_changed"


def test_register_verbs_swallows_registry_failures():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_verbs,
    )

    class BrokenRegistry:
        def register(self, **kwargs):
            raise RuntimeError("registry broken")

    n = register_verbs(BrokenRegistry())
    assert n == 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "why_changed_repl_composes_canonical_engine",
        "why_changed_repl_authority_read_only",
        "why_changed_repl_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/why_changed_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_composes_pin_fires_on_direct_construction():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
def foo():
    return AutonomyFeedbackEngine(bus, config)
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "composes_canonical_engine" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "AutonomyFeedbackEngine" in v for v in violations
    )


def test_read_only_pin_fires_on_mutating_call():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
async def foo():
    engine = get_default_engine()
    await engine.consume_curriculum_once()
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "read_only" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "consume_curriculum" in v or "read-only" in v
        for v in violations
    )


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    from backend.core.ouroboros.governance.why_changed_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
from backend.core.ouroboros.governance.providers import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("providers" in v for v in violations)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import why_changed_repl
    expected = {
        "WhyChangedReplDispatchResult",
        "dispatch_why_changed_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(why_changed_repl.__all__) == expected
