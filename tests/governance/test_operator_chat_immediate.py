"""A typed goal is a human asking, not a task-list entry.

`dispatch_backlog` wrote the goal to backlog.json, so it carried
`source="backlog"` — which sits in `_BACKGROUND_SOURCES` ("DW only, no Claude
fallback... cost-optimization-first") and is collected on a later sensor
sweep. Correct for the Backlog SENSOR mining a list. Wrong for someone who
just pressed Enter and is watching the screen.

The token described where the goal was STORED, not who ASKED for it — the same
class of defect `intent_envelope.py` already names: "Honest-source token:
decoupled test-coverage work MUST NOT masquerade as `test_failure`/`backlog`."
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    SOVEREIGN_SOURCES,
    _VALID_SOURCES,
)
from backend.core.ouroboros.governance.intent.signals import SignalSource
from backend.core.ouroboros.governance.urgency_router import (
    _BACKGROUND_SOURCES,
    _IMMEDIATE_SOURCES,
)


# --------------------------------------------------------------------------
# 1. the honest token
# --------------------------------------------------------------------------

def test_a_typed_goal_has_its_own_source() -> None:
    assert SignalSource.OPERATOR_CHAT.value == "operator_chat"
    assert "operator_chat" in _VALID_SOURCES


def test_it_routes_IMMEDIATE_like_every_other_human_origin() -> None:
    """§5: human-originated signals route IMMEDIATE because a person is
    waiting on the answer. A typed goal is that, exactly as a spoken one is."""
    assert "operator_chat" in _IMMEDIATE_SOURCES
    assert "voice_human" in _IMMEDIATE_SOURCES


def test_it_is_no_longer_background() -> None:
    """THE defect: `backlog` is cost-optimized and polled. A watched goal
    must not inherit that."""
    assert "operator_chat" not in _BACKGROUND_SOURCES
    assert "backlog" in _BACKGROUND_SOURCES, "the sensor route must remain"


def test_it_holds_sovereign_primacy() -> None:
    """The host keeps ultimate control — a typed goal outranks a resurrected
    op for the same reason a spoken one does."""
    assert "operator_chat" in SOVEREIGN_SOURCES


def test_it_is_distinct_from_voice_and_from_backlog() -> None:
    """Attributable separately, so observability can tell a spoken goal from
    a typed one from a mined one."""
    assert len({"operator_chat", "voice_human", "backlog"}) == 3


def test_the_immediate_set_stays_tight() -> None:
    """The bt-2026-04-13 regression was seven autonomous sensors mislabelling
    themselves and firing unattended. This source can only be produced by a
    keystroke, but the set must still not sprawl."""
    assert len(_IMMEDIATE_SOURCES) <= 5


# --------------------------------------------------------------------------
# 2. the executor dispatches now, and never loses the goal
# --------------------------------------------------------------------------

class _Turn:
    turn_id = "chat-1dc4650228e7"
    session_id = "repl"


def _executor(tmp_path: Path, submit: Any = None) -> Any:
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        BacklogChatActionExecutor,
    )
    return BacklogChatActionExecutor(tmp_path, submit_now=submit)


def test_a_wired_submitter_dispatches_immediately(tmp_path: Path) -> None:
    seen: List[Any] = []

    def _submit(msg: str, turn: Any) -> str:
        seen.append((msg, turn.turn_id))
        return "op-019fa4d2-246e-7759-86"

    out = _executor(tmp_path, _submit).dispatch_backlog("fix the tests", _Turn())
    assert out.startswith("op-")
    assert seen and seen[0][0] == "fix the tests"
    assert not (tmp_path / "backlog.json").exists(), (
        "it filed the goal as well as running it — duplicate work"
    )


def test_without_a_submitter_the_old_path_is_untouched(tmp_path: Path) -> None:
    """Injected, not imported: this module must stay usable with no intake,
    no event loop and no daemon."""
    out = _executor(tmp_path).dispatch_backlog("fix the tests", _Turn())
    assert out == "chat:chat-1dc4650228e7"


def test_an_intake_fault_falls_back_rather_than_dropping(tmp_path: Path) -> None:
    """A queued goal is a degradation. A dropped one is a betrayal."""
    def _boom(_msg: str, _turn: Any) -> str:
        raise RuntimeError("intake unreachable")

    out = _executor(tmp_path, _boom).dispatch_backlog("fix the tests", _Turn())
    assert out == "chat:chat-1dc4650228e7", "the goal was lost"


def test_a_submitter_returning_nothing_falls_back(tmp_path: Path) -> None:
    """Intake declining is not an exception — it must still not lose the
    goal."""
    out = _executor(tmp_path, lambda _m, _t: None).dispatch_backlog("x", _Turn())
    assert out == "chat:chat-1dc4650228e7"


def test_an_empty_goal_is_never_dispatched(tmp_path: Path) -> None:
    """Refusing empty input predates this and must survive it — otherwise a
    stray Enter fires an IMMEDIATE Claude op."""
    calls: List[Any] = []
    out = _executor(tmp_path, lambda m, t: calls.append(m) or "op-x").dispatch_backlog(
        "   ", _Turn(),
    )
    assert calls == [], "an empty goal reached intake"
    assert out.startswith("error-empty-message")


# --------------------------------------------------------------------------
# 3. the seam is armed by the daemon, not assumed everywhere
# --------------------------------------------------------------------------

def test_the_registry_arms_and_disarms(tmp_path: Path) -> None:
    """ONE seam rather than a parameter threaded through three nested
    factories — an intermediate factory that forgets to forward produces an
    executor that silently files goals, which is the wired-but-inert failure
    this codebase keeps paying for."""
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        set_operator_dispatcher,
    )
    try:
        assert _executor(tmp_path).dispatch_backlog("x", _Turn()).startswith("chat:")
        set_operator_dispatcher(lambda _m, _t: "op-019fa4d2-246e-7759-86")
        assert _executor(tmp_path).dispatch_backlog("x", _Turn()).startswith("op-")
    finally:
        set_operator_dispatcher(None)
    assert _executor(tmp_path).dispatch_backlog("x", _Turn()).startswith("chat:")


def test_explicit_injection_outranks_the_registry(tmp_path: Path) -> None:
    """Tests and knowing callers must be able to override the daemon's
    standing answer."""
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        set_operator_dispatcher,
    )
    try:
        set_operator_dispatcher(lambda _m, _t: "op-from-registry")
        out = _executor(tmp_path, lambda _m, _t: "op-explicit").dispatch_backlog(
            "x", _Turn(),
        )
        assert out == "op-explicit"
    finally:
        set_operator_dispatcher(None)


def test_the_daemon_arms_it_at_boot() -> None:
    """Structural: an unarmed seam files every goal — correct, but not what
    was built."""
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/battle_test/harness.py").read_text()
    assert "set_operator_dispatcher(self._submit_operator_goal)" in src


def test_the_submitter_uses_the_router_every_sensor_uses() -> None:
    """DRY, and governance: no private lane for operator input, so one set of
    dedup / WAL / priority rules covers autonomous and human work alike."""
    import inspect

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    src = inspect.getsource(BattleTestHarness._submit_operator_goal)
    assert '"_intake_router", "intake_router", "_router"' in src
    assert 'source="operator_chat"' in src


def test_urgency_is_high_not_critical() -> None:
    """The operator is waiting, but a typed goal is not an emergency and must
    not outrank a runtime alarm. IMMEDIATE eligibility comes from the SOURCE
    being human-origin — inflating urgency to buy it would distort every
    priority queue the envelope passes through."""
    import inspect

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    src = inspect.getsource(BattleTestHarness._submit_operator_goal)
    assert 'urgency="high"' in src
    assert 'urgency="critical"' not in src


def test_an_unreachable_intake_returns_none_rather_than_raising() -> None:
    """So the caller files the goal and says so."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    bare = BattleTestHarness.__new__(BattleTestHarness)
    assert bare._submit_operator_goal("fix the tests", _Turn()) is None
