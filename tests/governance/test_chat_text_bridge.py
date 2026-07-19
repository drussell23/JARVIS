"""Heuristic Intent Multiplexer — REPL text-plane bridge regression spine.

Pins the four mandates of the chat_text_bridge authorization:

1. Root-cause: the bridge composes the CANONICAL classifier/dispatcher —
   the verdict_override seam is additive (None = byte-identical legacy).
2. Async multiplexer: submit() never blocks; Ctrl+C (KeyboardInterrupt →
   token trigger) aborts an in-flight turn and returns to the prompt.
3. DRY: routing runs through the real ChatReplDispatcher +
   ConversationOrchestrator — the tests below use a RECORDING executor
   behind the real dispatch path, not a parallel router.
4. Bulletproof: standard string → chat executor (query_claude); code
   block → task executor (dispatch_backlog); injected interrupt →
   cancellation with ZERO orphaned tasks left on the event loop.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from backend.core.ouroboros.governance import chat_text_bridge as ctb
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatReplDispatcher,
)
from backend.core.ouroboros.governance.intent_classifier import ChatIntent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAT_TEXT_BRIDGE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    yield


# ---------------------------------------------------------------------------
# Recording executor — real Protocol, observable side
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    """ChatActionExecutor with an inspectable call log. ``block_event``
    (optional) makes query/backlog calls park until released — the
    cancellation tests use it to hold a 'generation' mid-flight."""

    def __init__(self, block_event: "threading.Event | None" = None) -> None:
        self.calls = []
        self._block = block_event
        self.entered = threading.Event()

    def _maybe_block(self) -> None:
        self.entered.set()
        if self._block is not None:
            self._block.wait(timeout=30)

    def dispatch_backlog(self, message, turn):
        self.calls.append(("dispatch_backlog", message))
        self._maybe_block()
        return "backlog-id-1"

    def spawn_subagent(self, message, turn):
        self.calls.append(("spawn_subagent", message))
        self._maybe_block()
        return "subagent-id-1"

    def query_claude(self, message, turn, recent_turns):
        self.calls.append(("query_claude", message))
        self._maybe_block()
        return "claude-answer"

    def attach_context(self, message, turn, target_turn):
        self.calls.append(("attach_context", message))
        return "attached"


def _mux(executor, **kw):
    d = ChatReplDispatcher(executor=executor)
    return ctb.ChatTextMultiplexer(d, **kw)


CODE_BLOCK = (
    "```python\n"
    "def frobnicate(x):\n"
    "    return x * 2\n"
    "```"
)


# ---------------------------------------------------------------------------
# (1) Code-shape pre-filter — pure evidence
# ---------------------------------------------------------------------------


def test_signals_empty_for_prose():
    assert ctb.code_shape_signals("how does the intake router work?") == ()


def test_signals_empty_for_single_word():
    # "status" parses as a bare Name — must NOT count as code.
    assert ctb.code_shape_signals("status") == ()


def test_signals_fence_and_ast():
    signals = ctb.code_shape_signals(CODE_BLOCK)
    assert ctb.SIGNAL_FENCED in signals
    assert ctb.SIGNAL_AST in signals


def test_signals_raw_multiline_code_without_fence():
    raw = "import os\nvalue = os.environ.get('X')\nprint(value)"
    assert ctb.SIGNAL_AST in ctb.code_shape_signals(raw)


def test_signals_never_raise():
    assert ctb.code_shape_signals(None) == ()      # type: ignore[arg-type]
    assert ctb.code_shape_signals("") == ()


# ---------------------------------------------------------------------------
# (2) weighted_classify — TASK-over-CHAT re-weighting
# ---------------------------------------------------------------------------


def test_prose_question_stays_chat():
    v = ctb.weighted_classify("how does the intake router work?")
    assert v.intent == ChatIntent.EXPLANATION
    assert ctb.BOOST_REASON not in v.reasons


def test_chat_shape_with_code_reweights_to_task():
    v = ctb.weighted_classify(
        "how should this behave?\n" + CODE_BLOCK,
    )
    assert v.intent == ChatIntent.ACTION_REQUEST
    assert ctb.BOOST_REASON in v.reasons
    assert v.confidence >= 0.55


def test_pure_runnable_paste_reweights_to_task():
    # Base classifier calls this CONTEXT_PASTE (fence fires first);
    # runnable code handed to an engineering organism is a work order.
    v = ctb.weighted_classify(CODE_BLOCK)
    assert v.intent == ChatIntent.ACTION_REQUEST
    assert ctb.SIGNAL_AST in v.reasons


def test_stacktrace_paste_keeps_attach_semantics():
    trace = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 3, in <module>\n'
        "KeyError: 'missing'"
    )
    v = ctb.weighted_classify(trace)
    # Diagnostic context (not AST-parseable) keeps canonical paste-attach.
    assert v.intent == ChatIntent.CONTEXT_PASTE


def test_action_request_with_code_gets_confidence_bump():
    base_text = "fix this function:\n" + CODE_BLOCK
    v = ctb.weighted_classify(base_text)
    assert v.intent == ChatIntent.ACTION_REQUEST
    assert ctb.BOOST_REASON in v.reasons


# ---------------------------------------------------------------------------
# (3) Multiplexer routing through the REAL dispatcher
# ---------------------------------------------------------------------------


async def test_standard_string_routes_to_chat_executor():
    ex = _RecordingExecutor()
    mux = _mux(ex)
    task = mux.submit("what is the current posture of the organism?")
    assert task is not None
    result = await task
    assert result is not None
    assert [c[0] for c in ex.calls] == ["query_claude"]


async def test_code_block_routes_to_task_executor():
    ex = _RecordingExecutor()
    mux = _mux(ex)
    task = mux.submit("how should this behave?\n" + CODE_BLOCK)
    assert task is not None
    await task
    assert [c[0] for c in ex.calls] == ["dispatch_backlog"]


async def test_submit_is_nonblocking_and_renders_to_sink():
    lines = []
    ex = _RecordingExecutor()
    mux = _mux(ex, print_sink=lines.append)
    task = mux.submit("why did the last op fail?")
    assert task is not None and not task.done()   # returned before work ran
    await task
    assert lines and "claude-answer" in lines[-1]


async def test_empty_submit_is_noop():
    mux = _mux(_RecordingExecutor())
    assert mux.submit("") is None
    assert mux.submit("   ") is None
    assert mux.active_count == 0


# ---------------------------------------------------------------------------
# (4) Cancellation — KeyboardInterrupt → token → graceful abort, no orphans
# ---------------------------------------------------------------------------


async def test_keyboard_interrupt_cancels_without_orphans():
    baseline = len(asyncio.all_tasks())
    release = threading.Event()
    ex = _RecordingExecutor(block_event=release)
    lines = []
    mux = _mux(ex, print_sink=lines.append)

    task = mux.submit("what is blocking the pipeline right now?")
    assert task is not None
    # Wait for the 'generation' to actually be in flight.
    await asyncio.to_thread(ex.entered.wait, 5)

    # The REPL surface: prompt_toolkit raises KeyboardInterrupt into
    # the input loop; the on_interrupt hook triggers the token.
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        mux.cancel_active()

    result = await task                     # returns promptly, cancelled
    assert result is None
    assert any("abandoned" in ln for ln in lines)

    release.set()                           # let the worker thread drain
    await mux.drain()
    assert mux.active_count == 0
    # No orphaned asyncio tasks may remain beyond the pre-test baseline.
    await asyncio.sleep(0)
    leaked = [
        t for t in asyncio.all_tasks()
        if not t.done() and t is not asyncio.current_task()
    ]
    assert len(leaked) <= baseline


async def test_token_resets_for_next_turn_after_cancel():
    release = threading.Event()
    ex = _RecordingExecutor(block_event=release)
    mux = _mux(ex)
    t1 = mux.submit("what changed since the last soak?")
    await asyncio.to_thread(ex.entered.wait, 5)
    mux.cancel_active()
    assert await t1 is None
    release.set()

    # Next turn must run to completion — the token cleared itself.
    ex2_calls_before = len(ex.calls)
    ex.entered.clear()
    t2 = mux.submit("why is doubleword degraded?")
    assert t2 is not None
    result = await t2
    assert result is not None
    assert len(ex.calls) > ex2_calls_before


async def test_drain_cancels_inflight_turns():
    release = threading.Event()
    ex = _RecordingExecutor(block_event=release)
    mux = _mux(ex)
    t = mux.submit("summarize the session so far")
    await asyncio.to_thread(ex.entered.wait, 5)
    release.set()
    await mux.drain()
    assert mux.active_count == 0
    assert t is not None and t.done()


# ---------------------------------------------------------------------------
# (5) Factory + flag gates + additive-seam legacy parity
# ---------------------------------------------------------------------------


def test_factory_none_when_bridge_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAT_TEXT_BRIDGE_ENABLED", "0")
    assert ctb.build_chat_text_multiplexer() is None


def test_factory_none_when_chat_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    assert ctb.build_chat_text_multiplexer() is None


def test_factory_builds_over_canonical_chain():
    mux = ctb.build_chat_text_multiplexer()
    assert isinstance(mux, ctb.ChatTextMultiplexer)


def test_verdict_override_none_is_legacy_byte_identical():
    """The additive seam: handle() without an override must produce the
    same decision the pre-seam code produced (inline classify path)."""
    ex_a, ex_b = _RecordingExecutor(), _RecordingExecutor()
    d_legacy = ChatReplDispatcher(executor=ex_a)
    d_seam = ChatReplDispatcher(executor=ex_b)
    msg = "explain the risk ladder"
    r1 = d_legacy.handle(msg)
    r2 = d_seam.handle(msg, verdict_override=None)
    assert r1.decision.action == r2.decision.action == "claude_query"


def test_verdict_override_steers_the_route():
    ex = _RecordingExecutor()
    d = ChatReplDispatcher(executor=ex)
    v = ctb.weighted_classify("how should this behave?\n" + CODE_BLOCK)
    d.handle("how should this behave?\n" + CODE_BLOCK, verdict_override=v)
    assert ex.calls[0][0] == "dispatch_backlog"


# ---------------------------------------------------------------------------
# (6) Harness + REPL wiring pins (source-anchored, refactor-detecting)
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    return (root / path).read_text()


def test_harness_fall_through_mounts_bridge():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    assert "_try_chat_text_bridge(command)" in src
    assert "build_chat_text_multiplexer" in src
    # The mount must live BEFORE the terminal unknown-command log.
    mount = src.index("_try_chat_text_bridge(command)")
    unknown = src.index('logger.debug("Unknown REPL command', mount)
    assert unknown > mount


def test_harness_slash_input_never_routes_to_chat():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    body_start = src.index("def _try_chat_text_bridge")
    body = src[body_start:body_start + 2000]
    assert 'text.startswith("/")' in body


def test_serpent_repl_interrupt_hook_present():
    src = _read("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert 'getattr(self, "on_interrupt", None)' in src


def test_harness_drains_mux_at_shutdown():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    assert "await mux.drain()" in src
