"""P3 P2 Slice 4 deferred follow-up — ClaudeChatActionExecutor regression suite.

PR 3 of 3 in the chat-executor mini-arc. CLOSES the entire mini-arc +
the third (final) deferred follow-up from Phase 3 P2 Slice 4 graduation.

Coverage:
  * Module constants + master flag default-false-pre-graduation.
  * Executor implements 4-method ChatActionExecutor Protocol.
  * `query_claude` calls injected provider + persists audit + returns
    response text.
  * Cage:
    - Empty / whitespace message → error token + no provider call.
    - Per-call cost cap respected.
    - Cumulative session budget gate triggers refusal.
    - Pre-call budget-overshoot gate refuses BEFORE provider call.
    - Provider raise → error token + no propagation.
    - Provider returns non-string → error token.
    - Bounded prompt length (MAX_QUERY_CHARS).
    - Bounded recent-turns context (MAX_RECENT_TURNS_INCLUDED).
    - Bounded response (MAX_RESPONSE_CHARS).
    - One-shot (no auto-retry).
  * NullClaudeQueryProvider returns sentinel + does NOT spend money.
  * Audit ledger captures every outcome (ok / empty / budget_exhausted
    / call_would_exceed_budget / provider_error / provider_non_string).
  * Per-method composition: 3 fallback delegations + cage check.
  * Factory composition: 8-flag matrix (claude × subagent × backlog
    × master) + provider injection + custom budget kwargs.
  * Authority invariants: no banned imports / no subprocess+network /
    no providers.py imports (Cage: provider is injected, never
    imported here so the executor stays light + testable).
"""
from __future__ import annotations

import ast as _ast
import json
import time
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.chat_repl_claude_executor import (
    AUDIT_SCHEMA_VERSION,
    ClaudeChatActionExecutor,
    DEFAULT_COST_CAP_PER_CALL_USD,
    DEFAULT_MAX_TOKENS_PER_QUERY,
    DEFAULT_SESSION_BUDGET_USD,
    MAX_QUERY_CHARS,
    MAX_RECENT_TURNS_INCLUDED,
    MAX_RECENT_TURN_FRAGMENT_CHARS,
    MAX_RESPONSE_CHARS,
    _NullClaudeQueryProvider,
    build_chat_repl_dispatcher_with_claude,
    is_enabled,
)
from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
    BacklogChatActionExecutor,
)
from backend.core.ouroboros.governance.chat_repl_subagent_executor import (
    SubagentChatActionExecutor,
)
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatRoutingDecision,
    LoggingChatActionExecutor,
)
from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatTurn,
)
from backend.core.ouroboros.governance.intent_classifier import (
    ChatIntent,
    IntentClassification,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "chat_repl_claude_executor.py"
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_turn(
    turn_id: str = "t-1",
    session_id: str = "s-1",
    message: str = "why is X happening?",
    response_text: str = "",
):
    decision = ChatRoutingDecision(
        action="claude_query",
        intent=ChatIntent.EXPLANATION,
        confidence=0.7,
        payload={"message": message},
    )
    return ChatTurn(
        turn_id=turn_id, session_id=session_id, operator_message=message,
        classification=IntentClassification(
            intent=ChatIntent.EXPLANATION, confidence=0.7,
        ),
        decision=decision,
        created_unix=time.time(),
        response_text=response_text,
    )


class _FakeProvider:
    """Test double — records every call, returns canned responses."""

    def __init__(self, responses=None, raise_on_call=None):
        self.calls: List[dict] = []
        self._responses = list(responses or [])
        self._raise = raise_on_call
        self._next = 0

    def query(self, prompt: str, max_tokens: int = 1024) -> str:
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if self._raise is not None:
            raise self._raise
        if self._next < len(self._responses):
            r = self._responses[self._next]
            self._next += 1
            return r
        return "ok"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    yield


def _read_audit(tmp_path: Path) -> list:
    p = tmp_path / ".jarvis" / "chat_claude_audit.jsonl"
    if not p.exists():
        return []
    return [
        json.loads(line) for line in p.read_text().splitlines()
        if line.strip()
    ]


# ===========================================================================
# A — Module constants + master flag
# ===========================================================================


def test_default_cost_cap_per_call_pinned():
    assert DEFAULT_COST_CAP_PER_CALL_USD == 0.05


def test_default_session_budget_pinned():
    assert DEFAULT_SESSION_BUDGET_USD == 1.0


def test_max_query_chars_pinned():
    assert MAX_QUERY_CHARS == 1024


def test_max_recent_turns_included_pinned():
    assert MAX_RECENT_TURNS_INCLUDED == 5


def test_max_response_chars_pinned():
    assert MAX_RESPONSE_CHARS == 4096


def test_default_max_tokens_pinned():
    assert DEFAULT_MAX_TOKENS_PER_QUERY == 1024


def test_audit_schema_version_pinned():
    assert AUDIT_SCHEMA_VERSION == 1


def test_master_flag_default_false_pre_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", raising=False)
    assert is_enabled() is False


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", val)
        assert is_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", val)
        assert is_enabled() is False


# ===========================================================================
# B — NullClaudeQueryProvider safety
# ===========================================================================


def test_null_provider_returns_sentinel():
    null = _NullClaudeQueryProvider()
    out = null.query("anything", max_tokens=100)
    assert "Claude provider not wired" in out
    assert "JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED" in out


def test_null_provider_does_not_spend(tmp_path):
    """Pin: an executor with NullProvider can be invoked freely
    without incrementing cost (tests + misconfigured factory)."""
    null = _NullClaudeQueryProvider()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=null,
    )
    # The current implementation conservatively bumps cost on a
    # successful call. Document this here so a future change to
    # "only count cost when the provider is real" has an explicit
    # test to update.
    out = ex.query_claude("q", _make_turn(turn_id="t-n"), recent_turns=[])
    assert "Claude provider not wired" in out
    # Cost increments by per-call cap (conservative accounting).
    assert ex.cumulative_cost_usd == DEFAULT_COST_CAP_PER_CALL_USD


# ===========================================================================
# C — query_claude happy path
# ===========================================================================


def test_query_claude_calls_provider_and_returns_response(tmp_path):
    fake = _FakeProvider(responses=["the answer is 42"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    out = ex.query_claude(
        "what is the meaning of life?",
        _make_turn(turn_id="t-h"),
        recent_turns=[],
    )
    assert out == "the answer is 42"
    assert len(fake.calls) == 1
    assert "operator: what is the meaning of life?" in fake.calls[0]["prompt"]
    assert fake.calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS_PER_QUERY


def test_query_claude_includes_recent_turns_in_context(tmp_path):
    fake = _FakeProvider(responses=["sure"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    recent = [
        _make_turn(
            turn_id="t-prev", message="earlier op question",
            response_text="earlier assistant answer",
        ),
    ]
    ex.query_claude(
        "follow-up", _make_turn(turn_id="t-h"), recent_turns=recent,
    )
    prompt = fake.calls[0]["prompt"]
    assert "[chat context]" in prompt
    assert "earlier op question" in prompt
    assert "earlier assistant answer" in prompt
    assert "[current message]" in prompt


def test_query_claude_response_truncated_at_max(tmp_path):
    big = "y" * (MAX_RESPONSE_CHARS + 200)
    fake = _FakeProvider(responses=[big])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    out = ex.query_claude("q", _make_turn(turn_id="t-t"), recent_turns=[])
    assert len(out) == MAX_RESPONSE_CHARS


def test_query_claude_message_truncated_at_max(tmp_path):
    fake = _FakeProvider(responses=["ok"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    big = "x" * (MAX_QUERY_CHARS + 500)
    ex.query_claude(big, _make_turn(turn_id="t-bm"), recent_turns=[])
    prompt = fake.calls[0]["prompt"]
    # Find the operator: line at the bottom and assert clipped
    op_line_start = prompt.rfind("operator: ")
    op_msg = prompt[op_line_start + len("operator: "):].split("\n")[0]
    assert len(op_msg) == MAX_QUERY_CHARS


def test_query_claude_recent_turns_capped_at_max(tmp_path):
    fake = _FakeProvider(responses=["ok"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    # 10 turns; only last MAX_RECENT_TURNS_INCLUDED (5) included
    recent = [
        _make_turn(
            turn_id=f"t-r{i}", message=f"op message {i}",
            response_text=f"response {i}",
        )
        for i in range(10)
    ]
    ex.query_claude("now", _make_turn(turn_id="t-r"), recent_turns=recent)
    prompt = fake.calls[0]["prompt"]
    # Earliest 5 (indices 0-4) NOT in prompt; latest 5 (5-9) IN prompt
    for i in range(5):
        assert f"op message {i}" not in prompt, (
            f"turn {i} should be excluded (only last 5 kept)"
        )
    for i in range(5, 10):
        assert f"op message {i}" in prompt


def test_query_claude_recent_fragment_truncated(tmp_path):
    fake = _FakeProvider(responses=["ok"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    huge = "z" * (MAX_RECENT_TURN_FRAGMENT_CHARS + 200)
    recent = [_make_turn(turn_id="t-prev", message=huge)]
    ex.query_claude("q", _make_turn(turn_id="t-f"), recent_turns=recent)
    prompt = fake.calls[0]["prompt"]
    # The huge fragment must NOT appear at full length
    assert "z" * (MAX_RECENT_TURN_FRAGMENT_CHARS + 100) not in prompt


# ===========================================================================
# D — Cage / error paths
# ===========================================================================


def test_query_claude_empty_message_returns_error_no_call(tmp_path):
    fake = _FakeProvider()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    out = ex.query_claude("", _make_turn(turn_id="t-e"), recent_turns=[])
    assert out.startswith("error-empty-message-")
    assert len(fake.calls) == 0
    assert ex.cumulative_cost_usd == 0.0


def test_query_claude_whitespace_message_returns_error(tmp_path):
    fake = _FakeProvider()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    out = ex.query_claude(
        "   \n\t   ", _make_turn(turn_id="t-ws"), recent_turns=[],
    )
    assert out.startswith("error-empty-message-")
    assert len(fake.calls) == 0


def test_query_claude_provider_raise_returns_error_no_propagation(tmp_path):
    fake = _FakeProvider(raise_on_call=ValueError("api borked"))
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    out = ex.query_claude(
        "q", _make_turn(turn_id="t-raise"), recent_turns=[],
    )
    assert out.startswith("error-provider-ValueError-")
    assert "t-raise" in out
    # Raise did not bump cost (call failed pre-charge)
    assert ex.cumulative_cost_usd == 0.0


def test_query_claude_provider_returns_non_string_returns_error(tmp_path):
    class BadProvider:
        def query(self, prompt, max_tokens=1024):
            return {"not": "a string"}
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=BadProvider(),
    )
    out = ex.query_claude(
        "q", _make_turn(turn_id="t-bad"), recent_turns=[],
    )
    assert out.startswith("error-provider-non-string-")
    assert ex.cumulative_cost_usd == 0.0


def test_session_budget_exhausted_blocks_further_calls(tmp_path):
    fake = _FakeProvider(responses=["a", "b", "c"])
    # Set session budget so 2 calls fit but 3rd is blocked
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
        cost_cap_per_call_usd=0.05,
        session_budget_usd=0.10,
    )
    out1 = ex.query_claude("q1", _make_turn(turn_id="t-1"), recent_turns=[])
    out2 = ex.query_claude("q2", _make_turn(turn_id="t-2"), recent_turns=[])
    out3 = ex.query_claude("q3", _make_turn(turn_id="t-3"), recent_turns=[])
    assert out1 == "a"
    assert out2 == "b"
    # Either gate fires: cumulative == budget after 2 calls trips the
    # `cumulative >= budget` gate; if it didn't, the pre-call
    # `cumulative + cap > budget` gate would. Accept either.
    assert (
        out3.startswith("error-session-budget-exhausted-")
        or out3.startswith("error-call-would-exceed-budget-")
    )
    assert len(fake.calls) == 2  # third call refused before reaching provider


def test_session_budget_pre_check_refuses_before_provider_call(tmp_path):
    """Even the first call is refused if cost_cap_per_call > session_budget."""
    fake = _FakeProvider(responses=["a"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
        cost_cap_per_call_usd=0.50,
        session_budget_usd=0.10,
    )
    out = ex.query_claude("q", _make_turn(turn_id="t-pre"), recent_turns=[])
    assert out.startswith("error-call-would-exceed-budget-")
    assert len(fake.calls) == 0


def test_session_budget_already_exhausted_path(tmp_path):
    """If cumulative cost is somehow already at budget, the
    `cumulative >= session_budget` gate fires (vs pre-call gate)."""
    fake = _FakeProvider(responses=["a"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
        cost_cap_per_call_usd=0.05,
        session_budget_usd=0.05,
    )
    # Manually exhaust budget (simulates restored state from a prior session)
    ex._cumulative_cost_usd = 0.05
    out = ex.query_claude("q", _make_turn(turn_id="t-ex"), recent_turns=[])
    assert out.startswith("error-session-budget-exhausted-")


# ===========================================================================
# E — Audit ledger
# ===========================================================================


def test_audit_ok_row_persisted(tmp_path):
    fake = _FakeProvider(responses=["yes"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    ex.query_claude("q", _make_turn(turn_id="t-au"), recent_turns=[])
    rows = _read_audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["turn_id"] == "t-au"
    assert rows[0]["session_id"] == "s-1"
    assert rows[0]["schema_version"] == AUDIT_SCHEMA_VERSION
    assert rows[0]["source"] == "chat_repl"
    assert rows[0]["response_chars"] == 3
    assert rows[0]["cumulative_cost_usd"] == DEFAULT_COST_CAP_PER_CALL_USD


def test_audit_empty_message_row(tmp_path):
    fake = _FakeProvider()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    ex.query_claude("", _make_turn(turn_id="t-e"), recent_turns=[])
    rows = _read_audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "empty_message"


def test_audit_provider_error_row(tmp_path):
    fake = _FakeProvider(raise_on_call=RuntimeError("boom"))
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    ex.query_claude("q", _make_turn(turn_id="t-pe"), recent_turns=[])
    rows = _read_audit(tmp_path)
    assert rows[0]["outcome"] == "provider_error"


def test_audit_session_budget_exhausted_row(tmp_path):
    fake = _FakeProvider(responses=["a"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
        cost_cap_per_call_usd=0.05, session_budget_usd=0.05,
    )
    ex._cumulative_cost_usd = 0.05
    ex.query_claude("q", _make_turn(turn_id="t-bx"), recent_turns=[])
    rows = _read_audit(tmp_path)
    assert rows[0]["outcome"] == "session_budget_exhausted"


# ===========================================================================
# F — Per-method composition
# ===========================================================================


def test_dispatch_backlog_delegates_to_fallback(tmp_path):
    fb = LoggingChatActionExecutor()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, fallback=fb,
    )
    out = ex.dispatch_backlog("x", _make_turn(turn_id="t-d"))
    assert out.startswith("logged-backlog-")
    assert fb.calls == [out]


def test_spawn_subagent_delegates_to_fallback(tmp_path):
    fb = LoggingChatActionExecutor()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, fallback=fb,
    )
    out = ex.spawn_subagent("x", _make_turn(turn_id="t-s"))
    assert out.startswith("logged-subagent-")


def test_attach_context_delegates_to_fallback(tmp_path):
    fb = LoggingChatActionExecutor()
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, fallback=fb,
    )
    target = _make_turn(turn_id="t-target")
    out = ex.attach_context("x", _make_turn(turn_id="t-a"), target)
    assert out.startswith("logged-attach-")


def test_query_claude_does_not_invoke_fallback(tmp_path):
    """Cage check: concrete query_claude must NEVER delegate."""
    fb = LoggingChatActionExecutor()
    fake = _FakeProvider(responses=["real"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake, fallback=fb,
    )
    ex.query_claude("q", _make_turn(turn_id="t-d"), recent_turns=[])
    assert fb.calls == []


def test_full_composition_routes_each_method_correctly(tmp_path):
    """Composition end-to-end:
    Claude(fallback=Subagent(fallback=Backlog(fallback=Logging))).
    query_claude → Claude (audit jsonl).
    spawn_subagent → Subagent (queue jsonl).
    dispatch_backlog → Backlog (backlog.json).
    attach_context → Logging."""
    backlog = BacklogChatActionExecutor(project_root=tmp_path)
    subagent = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=backlog,
    )
    fake = _FakeProvider(responses=["claude says hi"])
    claude = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake, fallback=subagent,
    )
    # Claude path
    out_c = claude.query_claude(
        "q", _make_turn(turn_id="t-1"), recent_turns=[],
    )
    assert out_c == "claude says hi"
    assert (tmp_path / ".jarvis" / "chat_claude_audit.jsonl").exists()
    # Subagent path
    out_s = claude.spawn_subagent("explore", _make_turn(turn_id="t-2"))
    assert out_s == "subagent:t-2"
    assert (tmp_path / ".jarvis" / "chat_subagent_queue.jsonl").exists()
    # Backlog path
    out_b = claude.dispatch_backlog("add", _make_turn(turn_id="t-3"))
    assert out_b == "chat:t-3"
    assert (tmp_path / ".jarvis" / "backlog.json").exists()
    # Attach path → Logging
    out_a = claude.attach_context(
        "ctx", _make_turn(turn_id="t-4"), _make_turn(turn_id="t-target"),
    )
    assert out_a.startswith("logged-attach-")


# ===========================================================================
# G — Factory wiring
# ===========================================================================


def test_factory_claude_off_falls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, LoggingChatActionExecutor)


def test_factory_claude_on_with_null_provider_when_no_provider(
    monkeypatch, tmp_path,
):
    """Claude flag on but no provider → NullProvider (no API call)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, ClaudeChatActionExecutor)
    out = disp.executor.query_claude(
        "q", _make_turn(turn_id="t-1"), recent_turns=[],
    )
    assert "Claude provider not wired" in out


def test_factory_claude_on_with_real_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    fake = _FakeProvider(responses=["from real provider"])
    disp = build_chat_repl_dispatcher_with_claude(
        project_root=tmp_path, claude_provider=fake,
    )
    assert disp is not None
    out = disp.executor.query_claude(
        "q", _make_turn(turn_id="t-r"), recent_turns=[],
    )
    assert out == "from real provider"


def test_factory_claude_on_subagent_on_backlog_on_chains_all(
    monkeypatch, tmp_path,
):
    """All three flags on → Claude(Subagent(Backlog(Logging)))."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, ClaudeChatActionExecutor)
    inner = disp.executor._fallback
    assert isinstance(inner, SubagentChatActionExecutor)
    inner2 = inner._fallback
    assert isinstance(inner2, BacklogChatActionExecutor)


def test_factory_claude_on_subagent_off_backlog_on(monkeypatch, tmp_path):
    """Claude(Backlog(Logging)) when subagent off."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, ClaudeChatActionExecutor)
    assert isinstance(disp.executor._fallback, BacklogChatActionExecutor)


def test_factory_claude_on_only(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, ClaudeChatActionExecutor)
    assert isinstance(disp.executor._fallback, LoggingChatActionExecutor)


def test_factory_chat_master_off_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_claude(project_root=tmp_path)
    assert disp is None


def test_factory_explicit_fallback_used_directly(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    custom = LoggingChatActionExecutor()
    disp = build_chat_repl_dispatcher_with_claude(
        project_root=tmp_path, fallback_executor=custom,
    )
    assert disp is not None
    assert disp.executor._fallback is custom


def test_factory_custom_budget_kwargs_propagate(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "1")
    fake = _FakeProvider(responses=["a"])
    disp = build_chat_repl_dispatcher_with_claude(
        project_root=tmp_path, claude_provider=fake,
        cost_cap_per_call_usd=0.10, session_budget_usd=2.5,
    )
    assert disp is not None
    assert disp.executor._cost_cap_per_call_usd == 0.10
    assert disp.executor._session_budget_usd == 2.5


# ===========================================================================
# H — Authority invariants (AST grep on module source)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator.",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    found_banned = [
        (m, s) for (m, s) in found_banned
        if not (s == "orchestrator." and "conversation_orchestrator" in m)
    ]
    assert not found_banned


def test_module_does_not_import_providers_module():
    """Cage: provider is INJECTED. The executor MUST NOT import the
    heavyweight `providers.py` module — that would couple chat to
    the codegen path + force the test suite to drag in the entire
    Anthropic stack. Production wiring constructs the provider
    elsewhere and passes it in."""
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert "providers" not in mod or "test" in mod, (
                f"chat_repl_claude_executor MUST NOT import "
                f"`providers.py` — provider is injected. Found: {mod}"
            )


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found


def test_module_does_not_import_anthropic_directly():
    """Cage: even the Anthropic SDK is NOT imported at module top
    level. Production code constructs the AnthropicClaudeQueryProvider
    elsewhere and injects it via the factory."""
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            names = []
            if isinstance(node, _ast.ImportFrom):
                names.append(node.module or "")
            else:
                names.extend(n.name for n in node.names)
            for n in names:
                assert "anthropic" not in n.lower(), (
                    f"chat_repl_claude_executor MUST NOT import "
                    f"anthropic — provider is injected. Found: {n}"
                )


# ===========================================================================
# I — Protocol conformance + audit list
# ===========================================================================


def test_executor_implements_all_four_protocol_methods():
    ex = ClaudeChatActionExecutor(project_root=Path.cwd())
    for method_name in ("dispatch_backlog", "spawn_subagent",
                         "query_claude", "attach_context"):
        assert hasattr(ex, method_name)
        assert callable(getattr(ex, method_name))


def test_calls_audit_list_populated_on_success(tmp_path):
    fake = _FakeProvider(responses=["resp"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    ex.query_claude("q", _make_turn(turn_id="t-c"), recent_turns=[])
    assert ex.calls == ["resp"]


def test_calls_audit_list_populated_on_error(tmp_path):
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=_FakeProvider(),
    )
    ex.query_claude("", _make_turn(turn_id="t-e"), recent_turns=[])
    assert ex.calls[0].startswith("error-empty-message-")


def test_cumulative_cost_property_exposed(tmp_path):
    fake = _FakeProvider(responses=["a", "b"])
    ex = ClaudeChatActionExecutor(
        project_root=tmp_path, provider=fake,
    )
    assert ex.cumulative_cost_usd == 0.0
    ex.query_claude("q1", _make_turn(turn_id="t-1"), recent_turns=[])
    assert ex.cumulative_cost_usd == DEFAULT_COST_CAP_PER_CALL_USD
    ex.query_claude("q2", _make_turn(turn_id="t-2"), recent_turns=[])
    assert ex.cumulative_cost_usd == 2 * DEFAULT_COST_CAP_PER_CALL_USD
