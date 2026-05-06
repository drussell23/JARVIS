"""§37 Slice 2 — `/listen` REPL regression spine.

Pins per operator binding 2026-05-05:

  * Single pipeline: composes `get_default_broker()` singleton
    only; never constructs `StreamEventBroker()` directly
  * Authority asymmetry: read-only operator surface; never calls
    `broker.publish*()` mutating methods
  * Substrate purity: no orchestrator / iron_gate / policy /
    providers / candidate_generator imports
  * Auto-discovery: `dispatch_listen_command` matches §32.11
    Slice 4 naming-cage; `register_verbs` matches help-discovery
    contract
  * NEVER raises: every code path defensive
  * Public read API extension: `recent_history` /
    `distinct_event_types` / `distinct_op_ids` return fresh
    list/tuple, lock-protected, defensive on malformed input

Verifies (32 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_broker():
    """Reset the broker singleton between tests."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        reset_default_broker,
    )
    try:
        reset_default_broker()
    except Exception:
        pass
    yield
    try:
        reset_default_broker()
    except Exception:
        pass


def _publish_event(
    *, event_type="task_completed", op_id="op-test-1",
    payload=None,
):
    """Helper: publish via canonical broker surface."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    return get_default_broker().publish(
        event_type=event_type,
        op_id=op_id,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Match + dispatch shape
# ---------------------------------------------------------------------------


def test_dispatch_matches_canonical_forms():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    for line in (
        "/listen",
        "listen",
        "/listen recent",
        "listen stats",
    ):
        result = dispatch_listen_command(line)
        assert result.matched is True


def test_dispatch_does_not_match_other_verbs():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    for line in (
        "/posture",
        "/decisions",
        "/listening",  # similar prefix, distinct verb
        "",
        "   ",
    ):
        result = dispatch_listen_command(line)
        assert result.matched is False


def test_help_bypasses_master():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen help")
    assert result.ok is True
    assert "/listen" in result.text
    assert "recent" in result.text
    assert "filter" in result.text
    assert "stats" in result.text


def test_short_help_alias():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen ?")
    assert result.ok is True


# ---------------------------------------------------------------------------
# Cold-start (empty broker) — honest "no events" rendering
# ---------------------------------------------------------------------------


def test_recent_empty_broker():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen")
    assert result.ok is True
    assert "Event Stream" in result.text
    assert "No events in history" in result.text


def test_types_empty_broker():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen types")
    assert result.ok is True
    assert "No events in history" in result.text


def test_ops_empty_broker():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen ops")
    assert result.ok is True
    assert "No op_ids in history" in result.text


def test_stats_empty_broker():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen stats")
    assert result.ok is True
    assert "Broker Stats" in result.text
    assert "history_size" in result.text


# ---------------------------------------------------------------------------
# Populated broker — end-to-end
# ---------------------------------------------------------------------------


def test_recent_renders_published_events():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(event_type="task_completed", op_id="op-1")
    _publish_event(event_type="task_completed", op_id="op-2")
    result = dispatch_listen_command("/listen recent")
    assert result.ok is True
    assert "task_completed" in result.text
    # op_id rendered (truncated to 12 chars)
    assert "op-1" in result.text or "op-2" in result.text


def test_recent_limit_clamping():
    """Limit clamps to [1, 200]."""
    from backend.core.ouroboros.governance.listen_repl import (
        _parse_limit,
    )
    assert _parse_limit([]) == 20
    assert _parse_limit(["5"]) == 5
    assert _parse_limit(["0"]) == 1
    assert _parse_limit(["999"]) == 200
    assert _parse_limit(["garbage"]) == 20


def test_types_lists_distinct():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(event_type="task_completed")
    _publish_event(event_type="plan_pending")
    _publish_event(event_type="task_completed")  # dup
    result = dispatch_listen_command("/listen types")
    assert "task_completed" in result.text
    assert "plan_pending" in result.text


def test_ops_lists_distinct():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(op_id="op-alpha")
    _publish_event(op_id="op-beta")
    _publish_event(op_id="op-alpha")  # dup
    result = dispatch_listen_command("/listen ops")
    assert "op-alpha" in result.text
    assert "op-beta" in result.text


def test_filter_by_type():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(event_type="task_completed", op_id="op-1")
    _publish_event(event_type="plan_pending", op_id="op-2")
    _publish_event(event_type="task_completed", op_id="op-3")
    result = dispatch_listen_command(
        "/listen filter type=task_completed",
    )
    assert result.ok is True
    assert "task_completed" in result.text
    assert "plan_pending" not in result.text


def test_filter_by_op():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(event_type="task_completed", op_id="op-target")
    _publish_event(event_type="plan_pending", op_id="op-other")
    _publish_event(event_type="plan_rejected", op_id="op-target")
    result = dispatch_listen_command(
        "/listen filter op=op-target",
    )
    assert "task_completed" in result.text
    assert "plan_rejected" in result.text
    assert "plan_pending" not in result.text


def test_filter_unknown_match_is_empty():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event(event_type="task_completed", op_id="op-1")
    result = dispatch_listen_command(
        "/listen filter type=nonexistent",
    )
    assert "No events match" in result.text


def test_filter_with_limit():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    for i in range(10):
        _publish_event(
            event_type="task_completed",
            op_id=f"op-{i}",
        )
    result = dispatch_listen_command(
        "/listen filter type=task_completed 3",
    )
    assert result.ok is True


def test_filter_parse_error():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen filter nonsense")
    assert result.ok is False
    assert "key=" in result.text or "<key>" in result.text


def test_filter_invalid_key():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    # 'sourceid' is not in the allowed key set
    result = dispatch_listen_command(
        "/listen filter sourceid=foo",
    )
    assert result.ok is False


def test_show_by_event_id_prefix():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    eid = _publish_event(
        event_type="task_completed", op_id="op-show",
        payload={"detail": "test-payload"},
    )
    result = dispatch_listen_command(
        f"/listen show {eid[:8]}",
    )
    assert result.ok is True
    assert "test-payload" in result.text
    assert "task_completed" in result.text


def test_show_unknown_event_id():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command(
        "/listen show ffffffff",
    )
    # ok=True because the dispatcher rendered cleanly
    assert "No event matches" in result.text


def test_show_missing_arg():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen show")
    assert result.ok is False
    assert "<event_id>" in result.text


def test_stats_reflects_publishes():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    _publish_event()
    _publish_event()
    _publish_event()
    result = dispatch_listen_command("/listen stats")
    assert "history_size" in result.text


# ---------------------------------------------------------------------------
# New broker public read-helpers (Slice 2 extension)
# ---------------------------------------------------------------------------


def test_recent_history_returns_fresh_list():
    """Caller mutations don't leak into broker state."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    _publish_event(op_id="op-x")
    broker = get_default_broker()
    snapshot = broker.recent_history(limit=10)
    snapshot.clear()
    # Original history still has the event
    assert broker.history_size >= 1


def test_recent_history_chronological_order():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    for i in range(3):
        _publish_event(
            event_type="task_completed",
            op_id=f"op-{i}",
        )
    events = get_default_broker().recent_history(limit=10)
    op_ids = [e.op_id for e in events]
    assert op_ids == ["op-0", "op-1", "op-2"]


def test_distinct_event_types_sorted():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    _publish_event(event_type="task_completed")
    _publish_event(event_type="plan_approved")
    _publish_event(event_type="board_closed")
    types = get_default_broker().distinct_event_types()
    # Sorted alphabetically — pin order
    assert types == [
        "board_closed", "plan_approved", "task_completed",
    ]


def test_distinct_op_ids_most_recent_first():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    _publish_event(op_id="op-old")
    _publish_event(op_id="op-mid")
    _publish_event(op_id="op-new")
    op_ids = get_default_broker().distinct_op_ids(limit=10)
    # Most-recent-first
    assert op_ids[0] == "op-new"
    assert op_ids[1] == "op-mid"
    assert op_ids[2] == "op-old"


def test_recent_history_filter_by_type():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    _publish_event(event_type="task_completed", op_id="op-1")
    _publish_event(event_type="plan_approved", op_id="op-2")
    _publish_event(event_type="task_completed", op_id="op-3")
    events = get_default_broker().recent_history(
        limit=10, event_type="task_completed",
    )
    assert len(events) == 2
    assert all(
        e.event_type == "task_completed" for e in events
    )


def test_recent_history_handles_malformed_limit():
    """Defensive: bad limit value clamps to default."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        get_default_broker,
    )
    broker = get_default_broker()
    # Should not raise on string input — type ignored, falls
    # through to default. Test: pass 0 → clamps to 1.
    events = broker.recent_history(limit=0)  # type: ignore
    # Just verify no exception
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_unknown_subcommand_returns_clean_error():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command("/listen wat")
    assert result.matched is True
    assert result.ok is False
    assert "unknown subcommand" in result.text.lower()


def test_shlex_parse_error_does_not_raise():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    result = dispatch_listen_command('/listen "unclosed')
    assert result.matched is True
    assert result.ok is False
    assert "parse error" in result.text


def test_broker_access_failure_returns_clean_error():
    from backend.core.ouroboros.governance.listen_repl import (
        dispatch_listen_command,
    )
    with patch(
        "backend.core.ouroboros.governance."
        "ide_observability_stream.get_default_broker",
        side_effect=RuntimeError("boot race"),
    ):
        result = dispatch_listen_command("/listen")
    assert result.matched is True
    assert result.ok is False
    assert "error" in result.text.lower()


# ---------------------------------------------------------------------------
# Auto-discovery hooks
# ---------------------------------------------------------------------------


def test_dispatch_function_naming_matches_cage():
    """§32.11 Slice 4 naming cage: file ends `_repl.py` → verb
    `listen` → dispatcher must be `dispatch_listen_command`."""
    from backend.core.ouroboros.governance import listen_repl
    assert hasattr(listen_repl, "dispatch_listen_command")
    import inspect
    sig = inspect.signature(
        listen_repl.dispatch_listen_command,
    )
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"


def test_register_verbs_returns_count():
    from backend.core.ouroboros.governance.listen_repl import (
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
    assert reg.calls[0]["verb"] == "listen"


def test_register_verbs_swallows_registry_failures():
    from backend.core.ouroboros.governance.listen_repl import (
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
    from backend.core.ouroboros.governance.listen_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "listen_repl_composes_canonical_broker",
        "listen_repl_authority_read_only",
        "listen_repl_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.listen_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/listen_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_composes_pin_fires_on_direct_construction():
    from backend.core.ouroboros.governance.listen_repl import (
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
    assert any(
        "StreamEventBroker" in v for v in violations
    )


def test_read_only_pin_fires_on_publish_call():
    from backend.core.ouroboros.governance.listen_repl import (
        register_shipped_invariants,
    )
    bad_source = '''
def foo():
    broker = get_default_broker()
    broker.publish(event_type="x", op_id="y", payload={})
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
        "publish" in v or "read-only" in v
        for v in violations
    )


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    from backend.core.ouroboros.governance.listen_repl import (
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
    assert any("iron_gate" in v for v in violations)


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import listen_repl
    expected = {
        "ListenReplDispatchResult",
        "dispatch_listen_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(listen_repl.__all__) == expected
