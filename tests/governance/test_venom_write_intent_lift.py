from __future__ import annotations

from backend.core.ouroboros.governance.exploration_engine import (
    compute_tool_loop_suppressed,
)


def _base(**over):
    kw = dict(
        complexity="moderate", route="background",
        is_bg_terminal_worker=False, has_repair_context=False,
        gate_enabled=True,
    )
    kw.update(over)
    return kw


def test_background_write_intent_gets_tools(monkeypatch):
    monkeypatch.delenv("JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED", raising=False)
    assert compute_tool_loop_suppressed(**_base(), is_read_only=False) is False


def test_background_read_only_keeps_preload_credit_skip():
    assert compute_tool_loop_suppressed(**_base(), is_read_only=True) is True


def test_speculative_write_intent_still_skipped():
    assert compute_tool_loop_suppressed(
        **_base(route="speculative"), is_read_only=False,
    ) is True


def test_kill_switch_reverts_to_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_WRITE_INTENT_LIFT_ENABLED", "false")
    assert compute_tool_loop_suppressed(**_base(), is_read_only=False) is True


def test_default_param_is_byte_identical_legacy():
    # No is_read_only passed -> defaults True -> legacy decision for all callers
    assert compute_tool_loop_suppressed(**_base()) is True


def test_trivial_write_intent_stays_skipped():
    # Complexity skip is untouched -- the lift is route-skip-only
    assert compute_tool_loop_suppressed(
        **_base(complexity="trivial"), is_read_only=False,
    ) is True
