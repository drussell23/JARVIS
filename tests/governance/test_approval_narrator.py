"""The Iron Gate asks, and an attached operator can answer.

An APPROVAL_REQUIRED op pauses at APPROVE and waits — correctly.
`CLIApprovalProvider.await_decision` already wraps that wait in
`asyncio.wait_for` and stamps EXPIRED on timeout, so nothing ever hung and the
daemon has never blocked on stdin.

What was missing is that NOBODY WAS EVER ASKED. The gate emitted a comm
heartbeat with `phase="approve"` that no cockpit surface renders, then sat
silently until it expired.

Two finished components had never been joined:

  * `OperatorPromptBridge` (#70085) — a pending-prompt registry whose future
    resolves from attached-terminal input, already consulted by
    `harness._on_input` BEFORE the REPL. It had ZERO callers of `begin()`: a
    receive path with no sender, so `waiting` was permanently False.
  * `_mirror_markup` — the chokepoint every ⏺/⎿ line already reaches the
    cockpit through.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.battle_test.operator_prompt_bridge import (
    get_operator_prompt_bridge,
    reset_bridge_for_tests,
)
from backend.core.ouroboros.governance.approval_narrator import (
    approval_narration_enabled,
    await_decision_with_operator,
    interpret_answer,
    render_gate_prompt,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean():
    reset_bridge_for_tests()
    yield
    reset_bridge_for_tests()


class _Provider:
    """Mirrors CLIApprovalProvider's contract: an Event the wait observes."""

    def __init__(self) -> None:
        self.decided: Optional[str] = None
        self.event = asyncio.Event()
        self.calls: List[str] = []

    async def await_decision(self, _rid: str, timeout_s: float) -> str:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            self.decided = "EXPIRED"
        return f"result:{self.decided}"

    async def approve(self, _rid: str, who: str) -> None:
        self.calls.append(f"approve:{who}")
        self.decided = "APPROVED"
        self.event.set()

    async def reject(self, _rid: str, who: str, why: str) -> None:
        self.calls.append(f"reject:{who}:{why}")
        self.decided = "REJECTED"
        self.event.set()


async def _answer(text: str, delay: float = 0.03) -> None:
    await asyncio.sleep(delay)
    get_operator_prompt_bridge().resolve(text)


# --------------------------------------------------------------------------
# 1. the question is asked
# --------------------------------------------------------------------------

def test_the_prompt_names_the_op_and_the_deadline() -> None:
    """Stating the expiry is what makes the silence honest: the operator
    learns both that a question is open and that not answering IS an answer."""
    out = render_gate_prompt("op-019fa4d2-246e-7759-86", "APPROVAL_REQUIRED",
                             "credential shape introduced", 300)
    assert "7759-86" in out
    assert "approve? [y/n]" in out
    assert "300s" in out
    assert "credential shape introduced" in out


def test_the_op_ref_is_the_distinguishing_tail() -> None:
    """UUIDv7 shares its prefix — the same lesson as the op digest."""
    out = render_gate_prompt("op-019fa4d2-246e-7759-86")
    assert "019fa4d2" not in out


def test_it_opens_and_closes_the_glyph_pair() -> None:
    out = render_gate_prompt("op-1-2-3", timeout_s=60)
    assert out.startswith("⏺ Iron Gate(")
    assert "⎿" in out


async def test_the_prompt_reaches_the_cockpit() -> None:
    lines: List[str] = []
    provider = _Provider()
    task = asyncio.ensure_future(await_decision_with_operator(
        provider, "op-1", 5, emit=lines.append,
    ))
    await _answer("y")
    await task
    assert lines and "approve?" in lines[0]


# --------------------------------------------------------------------------
# 2. the operator's answer decides it
# --------------------------------------------------------------------------

async def test_yes_approves_through_the_providers_own_method() -> None:
    """One source of truth for the outcome: the operator path calls the
    provider's approve(), which sets the event await_decision is waiting on."""
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await _answer("y")
    assert await task == "result:APPROVED"
    assert provider.calls == ["approve:operator"]


async def test_no_rejects() -> None:
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await _answer("n")
    assert await task == "result:REJECTED"


@pytest.mark.parametrize("word,expected", [
    ("y", True), ("yes", True), ("approve", True), ("  OK  ", True),
    ("n", False), ("no", False), ("reject", False), ("abort", False),
])
def test_the_vocabulary_is_forgiving(word: str, expected: bool) -> None:
    assert interpret_answer(word) is expected


def test_an_unrecognised_answer_is_not_a_yes() -> None:
    """Approval must be affirmative and deliberate: ambiguity resolves to
    rejection, never to the more convenient reading."""
    for junk in ("maybe", "later", "?", "sure thing pal", None, 42):
        assert interpret_answer(junk) is not True


# --------------------------------------------------------------------------
# 3. an unrelated command is not a verdict
# --------------------------------------------------------------------------

async def test_typing_a_verb_while_a_gate_is_open_is_not_an_answer() -> None:
    """Consuming a keystroke that was never a decision would make the cockpit
    feel possessed — and could approve a patch by accident."""
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await _answer("/posture status")
    await asyncio.sleep(0.05)
    assert not task.done(), "an unrelated command resolved the gate"
    assert provider.calls == []
    await _answer("y")
    assert await task == "result:APPROVED"


async def test_the_gate_stays_armed_after_a_non_answer() -> None:
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await _answer("hello")
    await asyncio.sleep(0.05)
    assert get_operator_prompt_bridge().waiting is True
    await _answer("n")
    await task


# --------------------------------------------------------------------------
# 4. it can only ADD a way to answer
# --------------------------------------------------------------------------

async def test_expiry_still_wins_when_nobody_answers() -> None:
    """The orphan prevention predates this and must be untouched."""
    provider = _Provider()
    assert await await_decision_with_operator(
        provider, "op-1", 0.05,
    ) == "result:EXPIRED"


async def test_a_decision_made_elsewhere_ends_the_wait() -> None:
    """Another terminal, a verb, or the ledger — the gate is not owned by the
    attached cockpit."""
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await asyncio.sleep(0.02)
    await provider.approve("op-1", "another-surface")
    assert await task == "result:APPROVED"


async def test_the_switch_off_falls_back_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_APPROVAL_NARRATION_ENABLED", "0")
    assert approval_narration_enabled() is False
    provider = _Provider()
    assert await await_decision_with_operator(
        provider, "op-1", 0.05,
    ) == "result:EXPIRED"


async def test_an_emit_fault_cannot_break_the_gate() -> None:
    def _boom(_line: str) -> None:
        raise RuntimeError("cockpit gone")

    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5, emit=_boom),
    )
    await _answer("y")
    assert await task == "result:APPROVED"


async def test_the_slot_is_released_afterwards() -> None:
    """A gate that leaves the bridge armed would swallow the next keystroke."""
    provider = _Provider()
    task = asyncio.ensure_future(
        await_decision_with_operator(provider, "op-1", 5),
    )
    await _answer("y")
    await task
    assert get_operator_prompt_bridge().waiting is False


# --------------------------------------------------------------------------
# 5. wiring
# --------------------------------------------------------------------------

def test_the_approve_phase_uses_it() -> None:
    src = (_REPO / "backend/core/ouroboros/governance/orchestrator.py").read_text()
    assert "_await_approval_with_operator(" in src


def test_the_orchestrator_degrades_to_the_plain_wait() -> None:
    """With nobody attached the gate must behave byte-identically."""
    import ast

    src = (_REPO / "backend/core/ouroboros/governance/orchestrator.py").read_text()
    # AST, not a character window: a fixed slice measures how much prose sits
    # between the def and the fallback, which is not the invariant.
    body = ""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and \
                node.name == "_await_approval_with_operator":
            body = ast.unparse(node)
            break
    assert body, "the wrapper is gone"
    assert "await_decision(request_id, timeout_s)" in body


def test_the_bridges_begin_finally_has_a_caller() -> None:
    """#70085 shipped a receive path with no sender — `waiting` was
    permanently False and every keystroke fell through to the REPL."""
    src = (_REPO / "backend/core/ouroboros/governance/"
           "approval_narrator.py").read_text()
    assert "bridge.begin(" in src
    assert "get_operator_prompt_bridge" in src


def test_the_timeout_is_not_reimplemented() -> None:
    """A second timeout that disagrees with the first is worse than no second
    path at all — await_decision stays the authority."""
    import ast

    src = (_REPO / "backend/core/ouroboros/governance/"
           "approval_narrator.py").read_text()
    # Checked on CODE, not on the file text. The module docstring explains
    # that `await_decision` already wraps the wait in `asyncio.wait_for` —
    # and a substring search cannot tell that explanation from a second
    # implementation of it. (Use-vs-mention: the fourth time this trap has
    # caught a test in this codebase.)
    tree = ast.parse(src)
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "asyncio.wait_for" not in calls, "a competing timeout was added"
    assert any("await_decision" in c for c in calls)
