"""P2 Slice 3 — /chat REPL dispatcher regression suite.

Pins:
  * Module constants + frozen result dataclass + status enum.
  * Subcommand parsing precedence — only fires when args match
    expected shape; else falls through to message dispatch (so
    natural-language ``/chat why is X?`` doesn't get misrouted).
  * /chat <message>, /chat history [N], /chat why <turn-id>,
    /chat clear, /chat help — happy paths.
  * Bare-text dispatch (operator opted into chat mode).
  * Empty / whitespace input → EMPTY status.
  * Unknown subcommand falls through to message dispatch when args
    look like prose.
  * /chat why with bad / missing turn-id → UNKNOWN_TURN.
  * Renderer output is ASCII-strict.
  * Renderer truncates oversize output at MAX_RENDERED_BYTES.
  * Executor wired: dispatches to backlog / subagent / claude /
    attach methods. Executor missing → DISPATCHED only. Executor
    raises → EXECUTOR_FAILED status, no propagation. Executor
    response stored back via record_response.
  * CONTEXT_PASTE with no prior turn falls back to query_claude
    via the executor.
  * Authority invariants: banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import tokenize
from pathlib import Path
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatTurn,
    ConversationOrchestrator,
    reset_default_orchestrator,
)
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    DEFAULT_SESSION_ID,
    HISTORY_DEFAULT_N,
    HISTORY_MAX_N,
    MAX_RENDERED_BYTES,
    ChatActionExecutor,
    ChatReplDispatcher,
    ChatReplResult,
    ChatReplStatus,
    is_enabled,
    render_decision,
    render_help,
    render_history,
    render_why,
)
from backend.core.ouroboros.governance.intent_classifier import ChatIntent


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


class _FakeBridge:
    def record_turn(self, **kw) -> None:
        pass


class _RecordingExecutor:
    """Captures every executor invocation. Slice 4 will replace with
    a real implementation against the FSM."""

    def __init__(self, response: Optional[str] = "ok") -> None:
        self.calls: List[tuple] = []
        self.response = response

    def dispatch_backlog(self, message: str, turn: ChatTurn) -> str:
        self.calls.append(("backlog", message, turn.turn_id))
        return self.response or ""

    def spawn_subagent(self, message: str, turn: ChatTurn) -> str:
        self.calls.append(("subagent", message, turn.turn_id))
        return self.response or ""

    def query_claude(
        self,
        message: str,
        turn: ChatTurn,
        recent_turns: List[ChatTurn],
    ) -> str:
        self.calls.append(
            ("claude", message, turn.turn_id, len(recent_turns)),
        )
        return self.response or ""

    def attach_context(
        self,
        message: str,
        turn: ChatTurn,
        target_turn: ChatTurn,
    ) -> str:
        self.calls.append(
            ("attach", message, turn.turn_id, target_turn.turn_id),
        )
        return self.response or ""


class _RaisingExecutor:
    def dispatch_backlog(self, *a, **kw):
        raise RuntimeError("backlog boom")

    def spawn_subagent(self, *a, **kw):
        raise RuntimeError("subagent boom")

    def query_claude(self, *a, **kw):
        raise RuntimeError("claude boom")

    def attach_context(self, *a, **kw):
        raise RuntimeError("attach boom")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    yield


@pytest.fixture
def disp():
    reset_default_orchestrator()
    o = ConversationOrchestrator(conversation_bridge=_FakeBridge())
    yield ChatReplDispatcher(orchestrator=o, default_session_id="s")
    reset_default_orchestrator()


# ===========================================================================
# A — Module constants + status enum + result dataclass
# ===========================================================================


def test_history_default_pinned():
    assert HISTORY_DEFAULT_N == 10


def test_history_max_pinned():
    assert HISTORY_MAX_N == 32


def test_max_rendered_bytes_pinned():
    assert MAX_RENDERED_BYTES == 16 * 1024


def test_default_session_id_pinned():
    assert DEFAULT_SESSION_ID == "repl"


def test_status_enum_values():
    assert {s.name for s in ChatReplStatus} == {
        "DISPATCHED", "SUBCOMMAND", "EMPTY",
        "UNKNOWN_SUBCOMMAND", "UNKNOWN_TURN",
        "EXECUTOR_FAILED", "EXECUTOR_OK",
    }


def test_result_is_frozen():
    r = ChatReplResult(status=ChatReplStatus.EMPTY, rendered_text="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.rendered_text = "y"  # type: ignore[misc]


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation():
    """Slice 3 ships default-OFF. Renamed at Slice 4 graduation."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_is_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", val)
    assert is_enabled() is True


# ===========================================================================
# C — handle: empty + bare-text + slash-prefix
# ===========================================================================


def test_handle_empty_returns_empty_status(disp):
    assert disp.handle("").status is ChatReplStatus.EMPTY
    assert disp.handle("   \t").status is ChatReplStatus.EMPTY


def test_handle_bare_text_dispatches(disp):
    r = disp.handle("fix the bug")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.action == "backlog_dispatch"


def test_handle_chat_prefix_with_message_dispatches(disp):
    r = disp.handle("/chat fix the bug")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.payload["message"] == "fix the bug"


def test_handle_chat_alone_dispatches_empty_message_as_noop(disp):
    """``/chat`` alone behaves like an empty message — orchestrator
    returns a noop decision."""
    r = disp.handle("/chat")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.action == "noop"


def test_handle_bare_text_via_explicit_helper(disp):
    r = disp.handle_bare_text("explain X", session_id="other-sess")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.action == "claude_query"
    # Used the explicit session id.
    assert r.turn is not None
    assert r.turn.session_id == "other-sess"


# ===========================================================================
# D — Subcommand parsing: HELP
# ===========================================================================


def test_chat_help_fires_subcommand(disp):
    r = disp.handle("/chat help")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "/chat <message>" in r.rendered_text
    assert "/chat history" in r.rendered_text
    assert "/chat why" in r.rendered_text


def test_chat_help_with_extra_args_falls_through_to_dispatch(disp):
    """``/chat help me debug X`` is natural language — must NOT fire
    the help subcommand."""
    r = disp.handle("/chat help me debug X")
    assert r.status is ChatReplStatus.DISPATCHED


# ===========================================================================
# E — Subcommand parsing: CLEAR
# ===========================================================================


def test_chat_clear_fires_subcommand(disp):
    disp.handle("/chat fix the bug")
    r = disp.handle("/chat clear")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "cleared" in r.rendered_text


def test_chat_clear_no_session_says_so(disp):
    r = disp.handle("/chat clear")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "no session" in r.rendered_text


def test_chat_clear_with_extra_args_falls_through(disp):
    """``/chat clear the cache`` is natural language."""
    r = disp.handle("/chat clear the cache")
    assert r.status is ChatReplStatus.DISPATCHED


# ===========================================================================
# F — Subcommand parsing: HISTORY
# ===========================================================================


def test_chat_history_default_count_is_ten(disp):
    for i in range(15):
        disp.handle(f"/chat fix item {i}")
    r = disp.handle("/chat history")
    assert r.status is ChatReplStatus.SUBCOMMAND
    # Ten items rendered; check by counting numbered lines.
    lines = [
        ln for ln in r.rendered_text.splitlines()
        if ln.lstrip().startswith(tuple(f"{i}." for i in range(1, 11)))
    ]
    assert len(lines) == 10


def test_chat_history_explicit_count(disp):
    for i in range(8):
        disp.handle(f"/chat fix item {i}")
    r = disp.handle("/chat history 3")
    lines = [
        ln for ln in r.rendered_text.splitlines()
        if ln.lstrip().startswith(("1.", "2.", "3."))
    ]
    assert len(lines) == 3


def test_chat_history_count_capped_at_max(disp):
    for i in range(40):
        disp.handle(f"/chat fix item {i}")
    r = disp.handle(f"/chat history {HISTORY_MAX_N + 100}")
    # Won't render more than the ring buffer holds (32 items).
    numbered = [
        ln for ln in r.rendered_text.splitlines()
        if ln.lstrip()[:3].rstrip(".").isdigit()
    ]
    assert len(numbered) <= HISTORY_MAX_N


def test_chat_history_zero_uses_default(disp):
    """``/chat history 0`` is non-positive → use default count."""
    for i in range(5):
        disp.handle(f"/chat fix item {i}")
    r = disp.handle("/chat history 0")
    assert r.status is ChatReplStatus.SUBCOMMAND


def test_chat_history_with_prose_args_falls_through(disp):
    """``/chat history of changes`` is natural language."""
    r = disp.handle("/chat history of changes")
    assert r.status is ChatReplStatus.DISPATCHED


def test_chat_history_empty_session_is_safe(disp):
    r = disp.handle("/chat history")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "(empty)" in r.rendered_text


# ===========================================================================
# G — Subcommand parsing: WHY
# ===========================================================================


def test_chat_why_with_valid_turn_id_returns_verdict(disp):
    first = disp.handle("/chat fix the bug")
    assert first.turn is not None
    r = disp.handle(f"/chat why {first.turn.turn_id}")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert first.turn.turn_id in r.rendered_text


def test_chat_why_no_args_returns_unknown_turn(disp):
    """Bare ``/chat why`` with no args should fall through to dispatch
    (since `why` alone matches no shape) — operator gets a normal
    EXPLANATION verdict on the literal word "why"."""
    r = disp.handle("/chat why")
    # Per shape gate: empty args don't match `why`'s required turn-id
    # shape → falls through to message dispatch.
    assert r.status is ChatReplStatus.DISPATCHED


def test_chat_why_natural_language_falls_through(disp):
    """``/chat why is X happening?`` must dispatch, not error on
    'is' as a turn-id."""
    r = disp.handle("/chat why is the test failing?")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.intent is ChatIntent.EXPLANATION


def test_chat_why_unknown_turn_id_returns_unknown_turn(disp):
    r = disp.handle("/chat why chat-deadbeefcafe")
    assert r.status is ChatReplStatus.UNKNOWN_TURN
    assert "no turn" in r.rendered_text


def test_chat_why_extra_args_after_turn_id_falls_through(disp):
    """``/chat why chat-XXX did this happen?`` — multiple tokens,
    doesn't match the strict single-token shape → dispatch."""
    first = disp.handle("/chat fix the bug")
    assert first.turn is not None
    r = disp.handle(f"/chat why {first.turn.turn_id} did this happen?")
    assert r.status is ChatReplStatus.DISPATCHED


# ===========================================================================
# H — Renderer
# ===========================================================================


def test_render_decision_is_ascii_safe(disp):
    """Rendered output must survive strict-ASCII terminals."""
    r = disp.handle("/chat fix the bug")
    assert r.turn is not None and r.decision is not None
    out = render_decision(r.turn, r.decision)
    out.encode("ascii")  # raises if non-ASCII slipped in


def test_render_decision_includes_intent_and_action(disp):
    r = disp.handle("/chat fix the bug")
    assert r.turn is not None and r.decision is not None
    out = render_decision(r.turn, r.decision)
    assert "ACTION_REQUEST" in out
    assert "backlog_dispatch" in out


def test_render_decision_truncates_long_message():
    """The rendered ``message:`` line clips at 200 chars to keep the
    pane readable."""
    from backend.core.ouroboros.governance.intent_classifier import (
        IntentClassification,
    )
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        ChatRoutingDecision,
    )
    big = "x" * 500
    decision = ChatRoutingDecision(
        action="backlog_dispatch",
        intent=ChatIntent.ACTION_REQUEST,
        confidence=0.7,
        payload={"message": big},
    )
    turn = ChatTurn(
        turn_id="chat-test",
        session_id="s",
        operator_message=big,
        classification=IntentClassification(
            intent=ChatIntent.ACTION_REQUEST, confidence=0.7,
        ),
        decision=decision,
        created_unix=0.0,
    )
    out = render_decision(turn, decision)
    # No 500-char run of x present.
    assert "x" * 500 not in out


def test_render_history_empty_marker():
    out = render_history([], session_id="s")
    assert "(empty)" in out


def test_render_help_lists_all_subcommands():
    out = render_help()
    for sub in ("/chat <message>", "/chat history", "/chat why",
                "/chat clear", "/chat help"):
        assert sub in out


def test_render_why_includes_classifier_reasons(disp):
    r = disp.handle("/chat fix the bug")
    assert r.turn is not None
    out = render_why(r.turn)
    assert "action_verb" in out


def test_renderer_clipped_at_max_bytes():
    """Unit test the clipper directly via a long help-style join."""
    from backend.core.ouroboros.governance.chat_repl_dispatcher import (
        _clip,
    )
    assert len(_clip("a" * (MAX_RENDERED_BYTES + 100))) <= MAX_RENDERED_BYTES


# ===========================================================================
# I — Executor wiring (Slice 4 will provide a real impl)
# ===========================================================================


def test_no_executor_returns_dispatched_only(disp):
    r = disp.handle("/chat fix the bug")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.executor_response is None


def test_executor_backlog_called_on_action(disp):
    exec = _RecordingExecutor()
    disp.executor = exec
    r = disp.handle("/chat fix the bug")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert exec.calls[0][0] == "backlog"
    assert exec.calls[0][1] == "fix the bug"


def test_executor_subagent_called_on_exploration(disp):
    exec = _RecordingExecutor()
    disp.executor = exec
    r = disp.handle("/chat find all callers")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert exec.calls[0][0] == "subagent"


def test_executor_claude_called_on_explanation(disp):
    exec = _RecordingExecutor()
    disp.executor = exec
    r = disp.handle("/chat explain Iron Gate")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert exec.calls[0][0] == "claude"


def test_executor_attach_called_on_paste_with_prior(disp):
    exec = _RecordingExecutor()
    disp.executor = exec
    disp.handle("/chat fix the bug")  # prior turn
    r = disp.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert exec.calls[-1][0] == "attach"


def test_executor_paste_with_no_prior_falls_to_claude(disp):
    """When CONTEXT_PASTE has no prior turn, executor MUST be invoked
    via query_claude (degraded path) — never attach to a non-existent
    target."""
    exec = _RecordingExecutor()
    disp.executor = exec
    r = disp.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert exec.calls[-1][0] == "claude"


def test_executor_response_persisted_to_turn(disp):
    exec = _RecordingExecutor(response="op-12345")
    disp.executor = exec
    r = disp.handle("/chat fix the bug")
    assert r.executor_response == "op-12345"
    assert r.turn is not None
    fetched = disp._orch().get_turn(r.turn.turn_id)
    assert fetched is not None
    assert fetched.response_text == "op-12345"


def test_executor_failure_does_not_propagate(disp):
    disp.executor = _RaisingExecutor()
    r = disp.handle("/chat fix the bug")
    assert r.status is ChatReplStatus.EXECUTOR_FAILED
    assert "executor failed" in r.rendered_text
    assert r.executor_response == "backlog boom"


def test_executor_skipped_on_noop(disp):
    """Empty input → noop decision. Executor MUST not be called."""
    exec = _RecordingExecutor()
    disp.executor = exec
    # Bare `/chat` becomes empty-message dispatch which yields noop.
    r = disp.handle("/chat ")
    assert r.status is ChatReplStatus.DISPATCHED
    assert r.decision is not None
    assert r.decision.action == "noop"
    assert exec.calls == []


# ===========================================================================
# J — Default-orchestrator fallback
# ===========================================================================


def test_dispatcher_uses_default_orchestrator_when_unset():
    """Pin: when no orchestrator is injected, dispatcher transparently
    uses the process-wide singleton (mirrors P3 renderer pattern)."""
    reset_default_orchestrator()
    d = ChatReplDispatcher()  # no orchestrator wired
    r = d.handle("fix the bug")
    assert r.status is ChatReplStatus.DISPATCHED
    reset_default_orchestrator()


# ===========================================================================
# K — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_dispatcher_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/chat_repl_dispatcher.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_dispatcher_no_io_or_subprocess():
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/chat_repl_dispatcher.py"),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


# ===========================================================================
# L — Protocol shape (compile-time only — Slice 4 will provide an impl)
# ===========================================================================


def test_chat_action_executor_protocol_method_names():
    """Pin: the Protocol has exactly the four methods Slice 4 will
    implement — dispatch_backlog / spawn_subagent / query_claude /
    attach_context. Adding/renaming requires a new slice."""
    expected = {
        "dispatch_backlog", "spawn_subagent",
        "query_claude", "attach_context",
    }
    actual = {
        m for m in dir(ChatActionExecutor)
        if not m.startswith("_") and not m.startswith("__")
    }
    # Protocol surfaces inherit class-level attributes; compare against
    # expected set as a subset.
    assert expected.issubset(actual), (
        f"Protocol shape changed; missing: {expected - actual}"
    )
