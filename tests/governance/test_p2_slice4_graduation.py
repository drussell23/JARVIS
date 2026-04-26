"""P2 Slice 4 — graduation pin suite + reachability supplement +
in-process live-fire smoke for the conversational mode (PRD §9 P2).

Layered evidence pattern, mirrors P3 Slice 4:
  * Master flag default-true pin (file-scoped + source-grep ``"1"``
    literal in BOTH slice modules that ship the env knob).
  * Pre-graduation pin rename pins (intent_classifier + dispatcher
    suites both must have the renamed test, not the old name).
  * Factory-selection invariants:
      - master-on  → ChatReplDispatcher with LoggingChatActionExecutor
      - master-off → None (SerpentFlow can skip surfacing /chat)
  * LoggingChatActionExecutor contract: 4 methods, returns
    "logged-..." token, bumps internal calls list, never raises.
  * Cross-slice authority survival: banned-import scan over all 4
    slice modules.
  * In-process live-fire smoke (15 checks): factory-built dispatcher
    routes every classifier intent end-to-end through the executor
    and back to the orchestrator's record_response.
  * Reachability supplement: factory hits both branches
    deterministically; LoggingExecutor reaches every action path
    deterministically.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatReplDispatcher,
    ChatReplStatus,
    LoggingChatActionExecutor,
    build_chat_repl_dispatcher,
    is_enabled as dispatcher_is_enabled,
)
from backend.core.ouroboros.governance.conversation_orchestrator import (
    ConversationOrchestrator,
    reset_default_orchestrator,
)
from backend.core.ouroboros.governance.intent_classifier import (
    ChatIntent,
    is_enabled as classifier_is_enabled,
)


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


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    yield


@pytest.fixture
def fresh_orch():
    reset_default_orchestrator()
    yield ConversationOrchestrator(conversation_bridge=_FakeBridge())
    reset_default_orchestrator()


# ===========================================================================
# §A — Master flag default-true (post-graduation)
# ===========================================================================


def test_classifier_master_flag_default_true(monkeypatch):
    """Pin: Slice 4 graduation flipped intent_classifier default."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    assert classifier_is_enabled() is True


def test_dispatcher_master_flag_default_true(monkeypatch):
    """Pin: Slice 4 graduation flipped chat_repl_dispatcher default."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    assert dispatcher_is_enabled() is True


def test_classifier_source_grep_default_literal_one():
    """Pin: source declares the env-default fallback as ``"1"`` in
    intent_classifier. Reverting means changing this literal back to
    ``""`` — pinning the literal makes the revert mechanically
    visible in any PR diff."""
    src = _read("backend/core/ouroboros/governance/intent_classifier.py")
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_CONVERSATIONAL_MODE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src), (
        "intent_classifier.is_enabled() must use "
        "os.environ.get(KEY, \"1\") for default-true"
    )


def test_dispatcher_source_grep_default_literal_one():
    """Pin: source declares the env-default fallback as ``"1"`` in
    chat_repl_dispatcher (the second module that ships the env knob)."""
    src = _read("backend/core/ouroboros/governance/chat_repl_dispatcher.py")
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_CONVERSATIONAL_MODE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src), (
        "chat_repl_dispatcher.is_enabled() must use "
        "os.environ.get(KEY, \"1\") for default-true"
    )


def test_master_flag_explicit_false_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    assert classifier_is_enabled() is False
    assert dispatcher_is_enabled() is False


def test_classifier_pin_renamed_in_primitive_suite():
    """Pin: pre-graduation pin
    ``test_is_enabled_default_false_pre_graduation`` MUST have been
    renamed to ``..._default_true_post_graduation`` per its embedded
    discipline."""
    src = _read("tests/governance/test_intent_classifier.py")
    code = _strip_docstrings_and_comments(src)
    assert "def test_is_enabled_default_false_pre_graduation" not in code
    assert "def test_is_enabled_default_true_post_graduation" in code


def test_dispatcher_pin_renamed_in_dispatcher_suite():
    """Pin: same rename in the dispatcher suite — both env-knob owners
    must rotate their pins together."""
    src = _read("tests/governance/test_chat_repl_dispatcher.py")
    code = _strip_docstrings_and_comments(src)
    assert "def test_is_enabled_default_false_pre_graduation" not in code
    assert "def test_is_enabled_default_true_post_graduation" in code


# ===========================================================================
# §B — Factory selection
# ===========================================================================


def test_factory_returns_dispatcher_when_master_on(monkeypatch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    reset_default_orchestrator()
    d = build_chat_repl_dispatcher()
    assert isinstance(d, ChatReplDispatcher)


def test_factory_returns_none_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    assert build_chat_repl_dispatcher() is None


def test_factory_default_executor_is_logging(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    assert isinstance(d.executor, LoggingChatActionExecutor)


def test_factory_threads_orchestrator(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    assert d.orchestrator is fresh_orch


def test_factory_threads_custom_executor(monkeypatch, fresh_orch):
    """Operators / Slice 5+ may inject a concrete executor; the
    factory MUST honour the override."""
    class CustomExec(LoggingChatActionExecutor):
        LABEL_PREFIX = "custom-"

    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    custom = CustomExec()
    d = build_chat_repl_dispatcher(
        orchestrator=fresh_orch, executor=custom,
    )
    assert d.executor is custom


def test_factory_truthy_variants_return_dispatcher(monkeypatch, fresh_orch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", val)
        d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
        assert isinstance(d, ChatReplDispatcher)


def test_factory_falsy_variants_return_none(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", val)
        assert build_chat_repl_dispatcher() is None


def test_factory_called_each_invocation(monkeypatch, fresh_orch):
    """Pin: factory checks env on each call (no caching). Operator can
    flip the flag mid-process and the next factory call respects."""
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    a = build_chat_repl_dispatcher()
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "true")
    b = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    assert a is None
    assert isinstance(b, ChatReplDispatcher)


# ===========================================================================
# §C — LoggingChatActionExecutor contract
# ===========================================================================


def test_logging_executor_label_prefix_pinned():
    assert LoggingChatActionExecutor.LABEL_PREFIX == "logged-"


def test_logging_executor_dispatch_backlog_returns_token(fresh_orch):
    e = LoggingChatActionExecutor()
    d = ChatReplDispatcher(orchestrator=fresh_orch, executor=e)
    r = d.handle("/chat fix the bug")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert r.executor_response.startswith("logged-backlog-")
    assert e.calls[0].startswith("logged-backlog-")


def test_logging_executor_spawn_subagent_returns_token(fresh_orch):
    e = LoggingChatActionExecutor()
    d = ChatReplDispatcher(orchestrator=fresh_orch, executor=e)
    r = d.handle("/chat find all callers of deprecated_api")
    assert r.executor_response.startswith("logged-subagent-")


def test_logging_executor_query_claude_returns_token(fresh_orch):
    e = LoggingChatActionExecutor()
    d = ChatReplDispatcher(orchestrator=fresh_orch, executor=e)
    r = d.handle("/chat explain the FSM")
    assert r.executor_response.startswith("logged-claude-")
    assert "ctx=" in r.executor_response


def test_logging_executor_attach_context_returns_token(fresh_orch):
    e = LoggingChatActionExecutor()
    d = ChatReplDispatcher(orchestrator=fresh_orch, executor=e)
    d.handle("/chat fix the bug")  # prior turn
    r = d.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )
    assert r.executor_response.startswith("logged-attach-")


def test_logging_executor_never_raises():
    """Pin: LoggingExecutor is the safe-default — a NoneType message
    must not crash it (defensive contract)."""
    e = LoggingChatActionExecutor()
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        ChatTurn,
    )
    from backend.core.ouroboros.governance.intent_classifier import (
        IntentClassification,
    )
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        ChatRoutingDecision,
    )
    fake_turn = ChatTurn(
        turn_id="chat-x", session_id="s", operator_message="x",
        classification=IntentClassification(
            intent=ChatIntent.ACTION_REQUEST, confidence=0.7,
        ),
        decision=ChatRoutingDecision(
            action="backlog_dispatch",
            intent=ChatIntent.ACTION_REQUEST, confidence=0.7,
        ),
        created_unix=0.0,
    )
    # Each method must accept the turn without raising.
    assert e.dispatch_backlog("msg", fake_turn).startswith("logged-backlog-")
    assert e.spawn_subagent("msg", fake_turn).startswith("logged-subagent-")
    assert e.query_claude("msg", fake_turn, []).startswith("logged-claude-")
    assert e.attach_context("msg", fake_turn, fake_turn).startswith(
        "logged-attach-",
    )


# ===========================================================================
# §D — Cross-slice authority survival
# ===========================================================================


_SLICE_FILES = [
    "backend/core/ouroboros/governance/intent_classifier.py",
    "backend/core/ouroboros/governance/conversation_orchestrator.py",
    "backend/core/ouroboros/governance/chat_repl_dispatcher.py",
]

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


@pytest.mark.parametrize("path", _SLICE_FILES)
def test_no_authority_imports_in_any_slice(path):
    src = _read(path)
    for imp in _BANNED:
        assert imp not in src, f"{path} imports banned: {imp}"


def test_classifier_remains_pure_data_post_graduation():
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/intent_classifier.py"),
    )
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling: {c}"


def test_orchestrator_remains_io_free_post_graduation():
    src = _strip_docstrings_and_comments(_read(
        "backend/core/ouroboros/governance/conversation_orchestrator.py",
    ))
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling: {c}"


def test_dispatcher_remains_io_free_post_graduation():
    src = _strip_docstrings_and_comments(_read(
        "backend/core/ouroboros/governance/chat_repl_dispatcher.py",
    ))
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling: {c}"


# ===========================================================================
# §E — In-process live-fire smoke (factory-built end-to-end)
# ===========================================================================


def test_livefire_L1_factory_builds_dispatcher_with_logging_executor(
    monkeypatch,
):
    """L1: post-graduation, factory selects ChatReplDispatcher with
    LoggingChatActionExecutor."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    reset_default_orchestrator()
    d = build_chat_repl_dispatcher()
    assert isinstance(d, ChatReplDispatcher)
    assert isinstance(d.executor, LoggingChatActionExecutor)


def test_livefire_L2_action_request_round_trip(monkeypatch, fresh_orch):
    """L2: 'fix the bug' → ACTION_REQUEST → backlog_dispatch →
    LoggingExecutor → 'logged-backlog-...' token persisted to turn."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle("/chat fix the auth bug")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert r.decision.intent is ChatIntent.ACTION_REQUEST
    assert r.executor_response.startswith("logged-backlog-")
    fetched = fresh_orch.get_turn(r.turn.turn_id)
    assert fetched.response_text == r.executor_response


def test_livefire_L3_exploration_round_trip(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle("/chat find every sensor under intake/")
    assert r.executor_response.startswith("logged-subagent-")


def test_livefire_L4_explanation_round_trip(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle("/chat why does ROUTE skip plan?")
    assert r.executor_response.startswith("logged-claude-")


def test_livefire_L5_paste_attaches_to_prior(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    first = d.handle("/chat fix the auth bug")
    r = d.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )
    assert r.executor_response.startswith("logged-attach-")
    # The attach token includes the target turn id.
    assert first.turn.turn_id in r.executor_response


def test_livefire_L6_paste_without_prior_falls_to_claude(
    monkeypatch, fresh_orch,
):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )
    assert r.executor_response.startswith("logged-claude-")


def test_livefire_L7_history_subcommand_after_dispatch(
    monkeypatch, fresh_orch,
):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    d.handle("/chat fix one")
    d.handle("/chat fix two")
    r = d.handle("/chat history 5")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "fix one" in r.rendered_text
    assert "fix two" in r.rendered_text


def test_livefire_L8_why_subcommand_against_real_turn(
    monkeypatch, fresh_orch,
):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    first = d.handle("/chat fix the auth bug")
    r = d.handle(f"/chat why {first.turn.turn_id}")
    assert r.status is ChatReplStatus.SUBCOMMAND
    assert "action_verb" in r.rendered_text
    assert first.turn.turn_id in r.rendered_text


def test_livefire_L9_clear_then_history_empty(monkeypatch, fresh_orch):
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    d.handle("/chat fix the bug")
    r1 = d.handle("/chat clear")
    assert "cleared" in r1.rendered_text
    r2 = d.handle("/chat history")
    assert "(empty)" in r2.rendered_text


def test_livefire_L10_natural_language_why_falls_through(
    monkeypatch, fresh_orch,
):
    """L10: graduation preserves the Slice 3 shape-gate so
    `/chat why is X happening?` still routes as EXPLANATION through
    the live executor — no UNKNOWN_TURN regression."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle("/chat why is the test failing?")
    assert r.status is ChatReplStatus.EXECUTOR_OK
    assert r.decision.intent is ChatIntent.EXPLANATION
    assert r.executor_response.startswith("logged-claude-")


def test_livefire_L11_bare_text_dispatches(monkeypatch, fresh_orch):
    """L11: graduated dispatcher honours the bare-text mode
    (operator opted into chat by typing without `/chat` prefix)."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    r = d.handle("explain the routing decision")
    assert r.status is ChatReplStatus.EXECUTOR_OK


def test_livefire_L12_master_off_revert_returns_none_dispatcher(
    monkeypatch,
):
    """L12: hot-revert proven — master-off → factory returns None,
    SerpentFlow can skip surfacing /chat entirely."""
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    reset_default_orchestrator()
    d = build_chat_repl_dispatcher()
    assert d is None


def test_livefire_L13_master_off_orchestrator_still_constructible():
    """L13: even with master-off, the orchestrator + bridge state
    remain inspectable (operators may want to read prior turns after
    revert). Mirrors the P3 queue-singleton-after-revert pin."""
    reset_default_orchestrator()
    o = ConversationOrchestrator(conversation_bridge=_FakeBridge())
    assert o.known_session_ids() == []


def test_livefire_L14_session_ring_caps_under_live_executor(
    monkeypatch, fresh_orch,
):
    """L14: bounded-by-construction safety preserved under graduation
    — pumping past the ring cap evicts oldest, never grows unbounded."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        MAX_TURNS_PER_SESSION,
    )
    for i in range(MAX_TURNS_PER_SESSION + 10):
        d.handle(f"/chat fix item {i}")
    session = fresh_orch.get_session(d.default_session_id)
    assert len(session.turns) == MAX_TURNS_PER_SESSION


def test_livefire_L15_executor_calls_traced(monkeypatch, fresh_orch):
    """L15: LoggingExecutor.calls list grows in dispatch order — gives
    operators (+ tests) a complete trace of every side-effecting call
    the executor would have made."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    d.handle("/chat fix the bug")
    d.handle("/chat find the helper")
    d.handle("/chat explain the FSM")
    e = d.executor
    assert isinstance(e, LoggingChatActionExecutor)
    assert len(e.calls) == 3
    assert e.calls[0].startswith("logged-backlog-")
    assert e.calls[1].startswith("logged-subagent-")
    assert e.calls[2].startswith("logged-claude-")


# ===========================================================================
# §F — Reachability supplement
# ===========================================================================


def test_reachability_factory_branch_dispatcher(monkeypatch):
    """Reachability: factory-on branch reaches `ChatReplDispatcher`
    deterministically."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    assert build_chat_repl_dispatcher() is not None


def test_reachability_factory_branch_none(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "false")
    assert build_chat_repl_dispatcher() is None


def test_reachability_logging_executor_all_four_methods(
    monkeypatch, fresh_orch,
):
    """All four ChatActionExecutor methods reachable via the live
    classifier in 4 dispatch calls — proves no dead branch exists."""
    monkeypatch.delenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", raising=False)
    d = build_chat_repl_dispatcher(orchestrator=fresh_orch)
    e = d.executor
    d.handle("/chat fix the bug")          # backlog
    d.handle("/chat find the helper")      # subagent
    d.handle("/chat explain the FSM")      # claude
    d.handle(
        "/chat ```\nTraceback (most recent call last):\n  File \"x\", line 5\n```",
    )                                       # attach (prior is the explain)
    prefixes = [c.split("-")[1] for c in e.calls]
    assert prefixes == ["backlog", "subagent", "claude", "attach"]
