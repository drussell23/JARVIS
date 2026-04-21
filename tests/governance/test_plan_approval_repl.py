"""Regression spine — PlanApproval REPL dispatcher (Slice 3).

Covers:
  1. Dispatcher recognizes /plan prefix; returns matched=False on
     unrelated lines.
  2. Subcommands: mode / pending / show / approve / reject /
     history / help.
  3. Arg parsing edge cases — missing op-id, missing reason,
     malformed shlex.
  4. render_plan_detail produces non-empty text with all plan fields.
  5. Reviewer propagation — approve/reject use the reviewer passed
     into dispatch_plan_command (defaults to "repl").
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalController,
    reset_default_controller,
)
from backend.core.ouroboros.governance.plan_approval_repl import (
    PlanDispatchResult,
    dispatch_plan_command,
    render_plan_detail,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


_ENV_KEYS = [
    "JARVIS_PLAN_APPROVAL_MODE",
    "JARVIS_PLAN_APPROVAL_TIMEOUT_S",
]


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reset_default_controller()
    yield
    reset_default_controller()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _register(controller: PlanApprovalController, op_id: str,
              approach: str = "do the thing") -> None:
    async def _t():
        controller.request_approval(op_id, {
            "schema_version": "plan.1",
            "approach": approach,
            "complexity": "moderate",
            "ordered_changes": [
                {"file_path": "a.py", "action": "modify",
                 "reason": "add flag"},
                {"file_path": "b.py", "action": "create"},
            ],
            "risk_factors": ["breaks downstream X"],
            "test_strategy": "unit tests",
        })
    _run_async(_t())


# --------------------------------------------------------------------------
# 1. Prefix matching
# --------------------------------------------------------------------------


def test_dispatch_ignores_non_plan_lines():
    result = dispatch_plan_command("hello world")
    assert result.matched is False


def test_dispatch_matches_plan_prefix():
    result = dispatch_plan_command("/plan help")
    assert result.matched is True
    assert result.ok is True
    assert "plan" in result.text.lower()


def test_dispatch_help_is_default_when_no_subcommand():
    result = dispatch_plan_command("/plan")
    assert result.ok is True
    assert "commands" in result.text.lower()


def test_dispatch_unknown_subcommand_fails():
    result = dispatch_plan_command("/plan bogus")
    assert result.matched is True
    assert result.ok is False
    assert "unknown" in result.text.lower()


# --------------------------------------------------------------------------
# 2. /plan mode
# --------------------------------------------------------------------------


def test_plan_mode_shows_current_state(monkeypatch):
    monkeypatch.delenv("JARVIS_PLAN_APPROVAL_MODE", raising=False)
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan mode", controller=c)
    assert r.ok is True
    assert "off" in r.text.lower()


def test_plan_mode_on_sets_env(monkeypatch):
    monkeypatch.delenv("JARVIS_PLAN_APPROVAL_MODE", raising=False)
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan mode on", controller=c)
    assert r.ok is True
    import os
    assert os.environ.get("JARVIS_PLAN_APPROVAL_MODE") == "true"


def test_plan_mode_off_sets_env(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_MODE", "true")
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan mode off", controller=c)
    assert r.ok is True
    import os
    assert os.environ.get("JARVIS_PLAN_APPROVAL_MODE") == "false"


def test_plan_mode_rejects_garbage_toggle():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan mode maybe", controller=c)
    assert r.ok is False


# --------------------------------------------------------------------------
# 3. /plan pending
# --------------------------------------------------------------------------


def test_plan_pending_empty():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan pending", controller=c)
    assert r.ok is True
    assert "no pending" in r.text.lower()


def test_plan_pending_lists_all_op_ids():
    c = PlanApprovalController()
    _register(c, "op-a", approach="first")
    _register(c, "op-b", approach="second")
    r = dispatch_plan_command("/plan pending", controller=c)
    assert r.ok is True
    assert "op-a" in r.text
    assert "op-b" in r.text


def test_plan_pending_excludes_terminal():
    c = PlanApprovalController()
    _register(c, "op-a")
    _register(c, "op-b")
    c.approve("op-a", reviewer="x")
    r = dispatch_plan_command("/plan pending", controller=c)
    assert r.ok is True
    # op-b (still pending) should appear.
    assert "op-b" in r.text
    # op-a (terminal) should NOT appear in the /plan pending listing.
    # (it may still appear in /plan history)
    assert "op-a" not in r.text


# --------------------------------------------------------------------------
# 4. /plan show
# --------------------------------------------------------------------------


def test_plan_show_requires_op_id():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan show", controller=c)
    assert r.ok is False


def test_plan_show_unknown_op_id_fails():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan show op-missing", controller=c)
    assert r.ok is False
    assert "no plan" in r.text.lower() or "missing" in r.text.lower()


def test_plan_show_renders_full_detail():
    c = PlanApprovalController()
    _register(c, "op-a", approach="refactor the thing")
    r = dispatch_plan_command("/plan show op-a", controller=c)
    assert r.ok is True
    assert "refactor the thing" in r.text
    assert "op-a" in r.text
    assert "PENDING" in r.text


# --------------------------------------------------------------------------
# 5. /plan approve
# --------------------------------------------------------------------------


def test_plan_approve_requires_op_id():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan approve", controller=c)
    assert r.ok is False


def test_plan_approve_unknown_op_id_fails():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan approve op-none", controller=c)
    assert r.ok is False


def test_plan_approve_success():
    c = PlanApprovalController()
    _register(c, "op-a")
    r = dispatch_plan_command("/plan approve op-a", controller=c)
    assert r.ok is True
    assert "APPROVED" in r.text
    snap = c.snapshot("op-a")
    assert snap["state"] == "approved"
    assert snap["reviewer"] == "repl"


def test_plan_approve_reviewer_propagates():
    c = PlanApprovalController()
    _register(c, "op-a")
    r = dispatch_plan_command(
        "/plan approve op-a", controller=c, reviewer="alice",
    )
    assert r.ok is True
    assert c.snapshot("op-a")["reviewer"] == "alice"


# --------------------------------------------------------------------------
# 6. /plan reject
# --------------------------------------------------------------------------


def test_plan_reject_requires_op_id_and_reason():
    c = PlanApprovalController()
    r1 = dispatch_plan_command("/plan reject", controller=c)
    r2 = dispatch_plan_command("/plan reject op-a", controller=c)
    assert r1.ok is False
    assert r2.ok is False


def test_plan_reject_success():
    c = PlanApprovalController()
    _register(c, "op-a")
    r = dispatch_plan_command(
        "/plan reject op-a wrong approach", controller=c,
    )
    assert r.ok is True
    assert "REJECTED" in r.text
    snap = c.snapshot("op-a")
    assert snap["state"] == "rejected"
    assert snap["reason"] == "wrong approach"


def test_plan_reject_quoted_reason_works():
    c = PlanApprovalController()
    _register(c, "op-a")
    r = dispatch_plan_command(
        '/plan reject op-a "wrong approach — missing edge case"',
        controller=c,
    )
    assert r.ok is True
    assert "missing edge case" in c.snapshot("op-a")["reason"]


# --------------------------------------------------------------------------
# 7. /plan history
# --------------------------------------------------------------------------


def test_plan_history_empty():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan history", controller=c)
    assert r.ok is True
    assert "no resolved" in r.text.lower()


def test_plan_history_shows_resolved():
    c = PlanApprovalController()
    _register(c, "op-a")
    _register(c, "op-b")
    c.approve("op-a", reviewer="r1")
    c.reject("op-b", reason="bad", reviewer="r2")
    r = dispatch_plan_command("/plan history", controller=c)
    assert r.ok is True
    assert "approved" in r.text.lower()
    assert "rejected" in r.text.lower()
    assert "op-a" in r.text
    assert "op-b" in r.text


def test_plan_history_custom_limit():
    c = PlanApprovalController()
    for i in range(5):
        _register(c, "op-%d" % i)
        c.approve("op-%d" % i, reviewer="x")
    r = dispatch_plan_command("/plan history 2", controller=c)
    assert r.ok is True
    # Only the last 2 op_ids should appear in the rendered history.
    assert "op-4" in r.text
    assert "op-3" in r.text
    assert "op-0" not in r.text


def test_plan_history_rejects_non_integer():
    c = PlanApprovalController()
    r = dispatch_plan_command("/plan history bogus", controller=c)
    assert r.ok is False


# --------------------------------------------------------------------------
# 8. render_plan_detail direct tests
# --------------------------------------------------------------------------


def test_render_plan_detail_includes_all_fields():
    snap = {
        "op_id": "op-z",
        "state": "pending",
        "created_ts": 0.0,
        "expires_ts": 1000.0,
        "reviewer": "",
        "reason": "",
        "plan": {
            "approach": "this is the approach",
            "complexity": "complex",
            "ordered_changes": [
                {"file_path": "x.py", "action": "modify",
                 "reason": "because"},
            ],
            "risk_factors": ["risk-one"],
            "test_strategy": "pytest --cov",
            "architectural_notes": "cross-cutting",
        },
    }
    text = render_plan_detail(snap)
    assert "op-z" in text
    assert "this is the approach" in text
    assert "x.py" in text
    assert "risk-one" in text
    assert "pytest --cov" in text
    assert "cross-cutting" in text


def test_render_plan_detail_handles_empty_plan():
    snap = {
        "op_id": "op-z",
        "state": "pending",
        "created_ts": 0.0,
        "expires_ts": 1000.0,
        "reviewer": "",
        "reason": "",
        "plan": {},
    }
    text = render_plan_detail(snap)
    # Must not raise + must include the op_id.
    assert "op-z" in text


def test_render_plan_detail_shows_resolution_for_terminal():
    snap = {
        "op_id": "op-z",
        "state": "rejected",
        "created_ts": 0.0,
        "expires_ts": 1000.0,
        "reviewer": "alice",
        "reason": "bad plan",
        "plan": {"approach": "x"},
    }
    text = render_plan_detail(snap)
    assert "REJECTED" in text
    assert "alice" in text
    assert "bad plan" in text


# --------------------------------------------------------------------------
# 9. Malformed input
# --------------------------------------------------------------------------


def test_dispatch_rejects_non_string_input():
    result = dispatch_plan_command(None)  # type: ignore[arg-type]
    assert result.matched is False


def test_dispatch_handles_malformed_shlex():
    # Unclosed quote trips shlex.
    result = dispatch_plan_command('/plan reject op-a "unclosed')
    assert result.ok is False
    assert "malformed" in result.text.lower()
