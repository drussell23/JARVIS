"""§37 Slice 6 — `/show_plan` REPL regression spine.

Pins per operator binding 2026-05-05:
  * Single pipeline (canonical broker only)
  * Authority asymmetry / read-only
  * Substrate purity AST-pinned
  * Auto-discovery via §32.11 Slice 4 naming-cage
  * Honest empty-state
  * NEVER raises
  * PlanGenerator publishes plan_generated event at completion

Verifies (24 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_broker(monkeypatch):
    from backend.core.ouroboros.governance.ide_observability_stream import (
        reset_default_broker,
    )
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    try:
        reset_default_broker()
    except Exception:
        pass
    yield
    try:
        reset_default_broker()
    except Exception:
        pass


def _publish_plan_event(
    *, op_id="op-test", complexity="moderate",
    skipped=False, skip_reason="",
    n_changes=2, n_risks=1,
    approach="Description of strategy",
    duration=2.5,
):
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_PLAN_GENERATED, get_default_broker,
    )
    payload = {
        "approach": approach,
        "complexity": complexity,
        "ordered_changes": [
            {
                "file_path": f"path/to/file_{i}.py",
                "description": f"change {i}",
            }
            for i in range(n_changes)
        ],
        "risk_factors": [f"risk {i}" for i in range(n_risks)],
        "test_strategy": "Run pytest",
        "architectural_notes": "",
        "planning_duration_s": duration,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "ui_affected": False,
    }
    return get_default_broker().publish(
        event_type=EVENT_TYPE_PLAN_GENERATED,
        op_id=op_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Match + dispatch
# ---------------------------------------------------------------------------


def test_dispatch_matches_canonical_forms():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    for line in (
        "/show_plan", "show_plan",
        "/show_plan recent", "show_plan op abc",
    ):
        assert dispatch_show_plan_command(line).matched is True


def test_dispatch_does_not_match_other_verbs():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    for line in (
        "/show", "/showplan", "/posture",
        "", "   ",
    ):
        assert dispatch_show_plan_command(line).matched is False


def test_help_bypasses_master():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan help")
    assert r.ok is True
    assert "/show_plan" in r.text
    assert "recent" in r.text


def test_short_help_alias():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    assert dispatch_show_plan_command("/show_plan ?").ok is True


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_bare_empty_broker():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan")
    assert r.ok is True
    assert "No plan_generated events" in r.text


def test_recent_empty_broker():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan recent")
    assert "No plan_generated" in r.text


def test_complexity_empty_broker():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan complexity")
    assert "No plan_generated" in r.text


# ---------------------------------------------------------------------------
# Populated broker
# ---------------------------------------------------------------------------


def test_bare_renders_most_recent_plan():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(op_id="op-1", complexity="trivial")
    _publish_plan_event(op_id="op-2", complexity="complex")
    r = dispatch_show_plan_command("/show_plan")
    assert r.ok is True
    # Most recent = op-2
    assert "op-2" in r.text
    assert "complex" in r.text


def test_recent_lists_one_per_event():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    for i in range(5):
        _publish_plan_event(
            op_id=f"op-{i}", complexity="moderate",
        )
    r = dispatch_show_plan_command("/show_plan recent 5")
    assert r.ok is True
    for i in range(5):
        assert f"op-{i}" in r.text


def test_recent_limit_clamping():
    from backend.core.ouroboros.governance.show_plan_repl import (
        _parse_limit,
    )
    assert _parse_limit([]) == 10
    assert _parse_limit(["3"]) == 3
    assert _parse_limit(["0"]) == 1
    assert _parse_limit(["999"]) == 100
    assert _parse_limit(["garbage"]) == 10


def test_op_lookup_exact():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(
        op_id="op-target",
        approach="Specific approach text",
    )
    r = dispatch_show_plan_command("/show_plan op op-target")
    assert r.ok is True
    assert "op-target" in r.text
    assert "Specific approach text" in r.text


def test_op_lookup_prefix():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(
        op_id="op-019dfa52-630e-79c3", complexity="architectural",
    )
    r = dispatch_show_plan_command(
        "/show_plan op op-019dfa52",
    )
    assert r.ok is True
    assert "architectural" in r.text


def test_op_lookup_unknown():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(op_id="op-1")
    r = dispatch_show_plan_command(
        "/show_plan op nonexistent",
    )
    # ok=True because dispatcher rendered cleanly
    assert "No plan event matches" in r.text


def test_op_lookup_missing_arg():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan op")
    assert r.ok is False
    assert "<op_id>" in r.text


def test_skipped_plan_renders_correctly():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(
        op_id="op-skipped",
        skipped=True,
        skip_reason="trivial op",
    )
    r = dispatch_show_plan_command("/show_plan op op-skipped")
    assert r.ok is True
    assert "SKIPPED" in r.text
    assert "trivial op" in r.text


def test_complexity_distribution():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    _publish_plan_event(complexity="trivial")
    _publish_plan_event(complexity="moderate")
    _publish_plan_event(complexity="moderate")
    _publish_plan_event(complexity="complex")
    _publish_plan_event(skipped=True, skip_reason="too small")
    r = dispatch_show_plan_command("/show_plan complexity")
    assert r.ok is True
    assert "trivial" in r.text
    assert "moderate" in r.text
    assert "complex" in r.text
    assert "skipped" in r.text


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_unknown_subcommand():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command("/show_plan garbage")
    assert r.matched is True
    assert r.ok is False
    assert "unknown" in r.text.lower()


def test_shlex_parse_error():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    r = dispatch_show_plan_command('/show_plan "unclosed')
    assert r.matched is True
    assert r.ok is False


def test_broker_failure_returns_clean_error():
    from backend.core.ouroboros.governance.show_plan_repl import (
        dispatch_show_plan_command,
    )
    with patch(
        "backend.core.ouroboros.governance."
        "ide_observability_stream.get_default_broker",
        side_effect=RuntimeError("broker broken"),
    ):
        r = dispatch_show_plan_command("/show_plan")
    # Should NOT raise; observer renders empty state cleanly
    assert r.matched is True


# ---------------------------------------------------------------------------
# SSE event integration
# ---------------------------------------------------------------------------


def test_event_type_in_broker_whitelist():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_PLAN_GENERATED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_PLAN_GENERATED in _VALID_EVENT_TYPES


def test_plan_generator_emits_event_at_completion():
    """PlanGenerator MUST publish plan_generated at the end of
    its run path. AST regression — find publish call site in
    plan_generator.py."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/plan_generator.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "EVENT_TYPE_PLAN_GENERATED" in source, (
        "plan_generator.py MUST publish plan_generated event "
        "at PLAN-phase completion (§37 Slice 6 regression)"
    )
    assert "get_default_broker" in source, (
        "plan_generator.py MUST compose canonical broker"
    )


def test_plan_generator_publish_is_defensive():
    """The publish hook MUST be wrapped in try/except so PLAN
    phase doesn't break on broker failure."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/plan_generator.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    has_defensive_try = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for body_stmt in node.body:
            for inner in ast.walk(body_stmt):
                if (
                    isinstance(inner, ast.Name)
                    and inner.id == "EVENT_TYPE_PLAN_GENERATED"
                ):
                    has_defensive_try = True
                    break
    assert has_defensive_try, (
        "plan_generator.py MUST wrap the publish call in "
        "try/except (defensive — broker error must not "
        "break PLAN phase)"
    )


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def test_dispatch_function_naming_matches_cage():
    from backend.core.ouroboros.governance import show_plan_repl
    assert hasattr(
        show_plan_repl, "dispatch_show_plan_command",
    )


def test_register_verbs_returns_count():
    from backend.core.ouroboros.governance.show_plan_repl import (
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
    assert reg.calls[0]["verb"] == "show_plan"


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.show_plan_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 3


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.show_plan_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/show_plan_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == ()


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import show_plan_repl
    expected = {
        "ShowPlanReplDispatchResult",
        "dispatch_show_plan_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(show_plan_repl.__all__) == expected
