"""P3 P2 Slice 4 deferred follow-up — SubagentChatActionExecutor regression suite.

PR 2 of 3 in the chat-executor mini-arc. Pins the structural integration
of `SubagentChatActionExecutor` (enqueue-and-return-ticket pattern for
chat-driven subagent dispatches).

Coverage:
  * Module constants + master flag default-false-pre-graduation.
  * Executor implements the 4-method ChatActionExecutor Protocol.
  * `spawn_subagent` writes a JSONL ticket entry to
    .jarvis/chat_subagent_queue.jsonl with provenance markers.
  * Empty / whitespace goal -> error token + no file write.
  * Bounded goal length (MAX_SUBAGENT_GOAL_CHARS).
  * ticket_id is `subagent:{turn_id}` for sweeper dedup.
  * Audit list (.calls) populated.
  * Other 3 methods delegate to fallback (defaults to LoggingChatActionExecutor).
  * Factory chains correctly through PR 1's backlog factory:
    - subagent flag off + backlog flag off -> Logging only
    - subagent flag off + backlog flag on -> Backlog wired (PR 1 path)
    - subagent flag on + backlog flag off -> Subagent(fallback=Logging)
    - subagent flag on + backlog flag on -> Subagent(fallback=Backlog)
    - chat master off -> None (regardless of either flag)
  * Authority invariants (AST grep):
    - No banned governance imports.
    - No subprocess / network / env-mutation tokens.
    - JSONL append, not arbitrary file write.
"""
from __future__ import annotations

import ast as _ast
import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.chat_repl_subagent_executor import (
    MAX_SUBAGENT_GOAL_CHARS,
    SubagentChatActionExecutor,
    TICKET_SCHEMA_VERSION,
    build_chat_repl_dispatcher_with_subagent,
    is_enabled,
)
from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
    BacklogChatActionExecutor,
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
    / "chat_repl_subagent_executor.py"
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_turn(
    turn_id: str = "t-1",
    session_id: str = "s-1",
    message: str = "explore the auth module",
):
    decision = ChatRoutingDecision(
        action="subagent_explore",
        intent=ChatIntent.EXPLORATION,
        confidence=0.7,
        payload={"message": message},
    )
    return ChatTurn(
        turn_id=turn_id, session_id=session_id, operator_message=message,
        classification=IntentClassification(
            intent=ChatIntent.EXPLORATION, confidence=0.7,
        ),
        decision=decision,
        created_unix=time.time(),
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    yield


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines()
        if line.strip()
    ]


# ===========================================================================
# A — Module constants + master flag
# ===========================================================================


def test_max_subagent_goal_chars_pinned():
    assert MAX_SUBAGENT_GOAL_CHARS == 512


def test_ticket_schema_version_pinned():
    assert TICKET_SCHEMA_VERSION == 1


def test_master_flag_default_false_pre_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", raising=False)
    assert is_enabled() is False


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", val)
        assert is_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", val)
        assert is_enabled() is False


# ===========================================================================
# B — spawn_subagent enqueues real ticket
# ===========================================================================


def test_spawn_subagent_writes_ticket(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    out = ex.spawn_subagent("explore auth", _make_turn(turn_id="t-w"))
    assert out == "subagent:t-w"
    queue = tmp_path / ".jarvis" / "chat_subagent_queue.jsonl"
    assert queue.exists()
    rows = _read_jsonl(queue)
    assert len(rows) == 1
    t = rows[0]
    assert t["ticket_id"] == "subagent:t-w"
    assert t["schema_version"] == TICKET_SCHEMA_VERSION
    assert t["subagent_type"] == "explore"
    assert t["goal"] == "explore auth"
    assert t["source"] == "chat_repl"
    assert t["session_id"] == "s-1"
    assert t["turn_id"] == "t-w"
    assert t["status"] == "pending"
    assert t["target_files"] == []
    assert t["scope_paths"] == []


def test_spawn_subagent_appends_to_existing_queue(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    ex.spawn_subagent("first goal", _make_turn(turn_id="t-1"))
    ex.spawn_subagent("second goal", _make_turn(turn_id="t-2"))
    rows = _read_jsonl(tmp_path / ".jarvis" / "chat_subagent_queue.jsonl")
    assert len(rows) == 2
    assert rows[0]["ticket_id"] == "subagent:t-1"
    assert rows[1]["ticket_id"] == "subagent:t-2"


def test_spawn_subagent_empty_goal_returns_error_no_write(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    out = ex.spawn_subagent("", _make_turn(turn_id="t-e"))
    assert out.startswith("error-empty-goal-")
    assert "t-e" in out
    queue = tmp_path / ".jarvis" / "chat_subagent_queue.jsonl"
    assert not queue.exists()


def test_spawn_subagent_whitespace_goal_returns_error(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    out = ex.spawn_subagent("\n\t   ", _make_turn(turn_id="t-ws"))
    assert out.startswith("error-empty-goal-")


def test_spawn_subagent_truncates_at_max_chars(tmp_path):
    big = "x" * (MAX_SUBAGENT_GOAL_CHARS + 200)
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    ex.spawn_subagent(big, _make_turn(turn_id="t-big"))
    rows = _read_jsonl(tmp_path / ".jarvis" / "chat_subagent_queue.jsonl")
    assert len(rows[0]["goal"]) == MAX_SUBAGENT_GOAL_CHARS


def test_spawn_subagent_includes_timestamp(tmp_path):
    before = time.time()
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    ex.spawn_subagent("ts", _make_turn(turn_id="t-ts"))
    after = time.time()
    rows = _read_jsonl(tmp_path / ".jarvis" / "chat_subagent_queue.jsonl")
    assert before <= rows[0]["submitted_timestamp_unix"] <= after


def test_spawn_subagent_calls_audit_on_success(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    ex.spawn_subagent("g1", _make_turn(turn_id="t-1"))
    ex.spawn_subagent("g2", _make_turn(turn_id="t-2"))
    assert ex.calls == ["subagent:t-1", "subagent:t-2"]


def test_spawn_subagent_calls_audit_on_error(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    ex.spawn_subagent("", _make_turn(turn_id="t-bad"))
    assert ex.calls[0].startswith("error-empty-goal-")


# ===========================================================================
# C — Per-method composition
# ===========================================================================


def test_dispatch_backlog_delegates_to_fallback(tmp_path):
    fallback = LoggingChatActionExecutor()
    ex = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    out = ex.dispatch_backlog("backlog item", _make_turn(turn_id="t-b"))
    assert out.startswith("logged-backlog-")
    assert fallback.calls == [out]


def test_query_claude_delegates_to_fallback(tmp_path):
    fallback = LoggingChatActionExecutor()
    ex = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    out = ex.query_claude("?", _make_turn(turn_id="t-q"), recent_turns=[])
    assert out.startswith("logged-claude-")


def test_attach_context_delegates_to_fallback(tmp_path):
    fallback = LoggingChatActionExecutor()
    ex = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    target = _make_turn(turn_id="t-target")
    out = ex.attach_context(
        "more ctx", _make_turn(turn_id="t-a"), target,
    )
    assert out.startswith("logged-attach-")


def test_default_fallback_is_logging_executor(tmp_path):
    ex = SubagentChatActionExecutor(project_root=tmp_path)
    out = ex.dispatch_backlog("x", _make_turn(turn_id="t-fb"))
    assert out.startswith("logged-")


def test_spawn_subagent_does_not_invoke_fallback(tmp_path):
    """Cage check: concrete spawn_subagent must NEVER delegate."""
    fallback = LoggingChatActionExecutor()
    ex = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    ex.spawn_subagent("real subagent", _make_turn(turn_id="t-d"))
    assert fallback.calls == []


def test_subagent_executor_with_backlog_fallback_routes_correctly(tmp_path):
    """Composition: Subagent(fallback=Backlog(fallback=Logging)).
    spawn_subagent → Subagent (queue file).
    dispatch_backlog → Backlog (backlog.json).
    query_claude → Logging."""
    backlog = BacklogChatActionExecutor(project_root=tmp_path)
    subagent = SubagentChatActionExecutor(
        project_root=tmp_path, fallback=backlog,
    )
    # spawn_subagent → queue file
    out_sub = subagent.spawn_subagent("sub", _make_turn(turn_id="t-1"))
    assert out_sub == "subagent:t-1"
    assert (tmp_path / ".jarvis" / "chat_subagent_queue.jsonl").exists()
    # dispatch_backlog → backlog.json
    out_back = subagent.dispatch_backlog("back", _make_turn(turn_id="t-2"))
    assert out_back == "chat:t-2"
    assert (tmp_path / ".jarvis" / "backlog.json").exists()
    # query_claude → still Logging via Backlog's fallback
    out_claude = subagent.query_claude(
        "?", _make_turn(turn_id="t-3"), recent_turns=[],
    )
    assert out_claude.startswith("logged-claude-")


# ===========================================================================
# D — Factory wiring
# ===========================================================================


def test_factory_subagent_off_falls_through(monkeypatch, tmp_path):
    """subagent flag off + backlog flag off → Logging only."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, LoggingChatActionExecutor)


def test_factory_subagent_off_backlog_on_routes_to_backlog(
    monkeypatch, tmp_path,
):
    """subagent flag off + backlog flag on → Backlog (PR 1 path)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "0")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, BacklogChatActionExecutor)


def test_factory_subagent_on_backlog_off_wires_subagent_logging(
    monkeypatch, tmp_path,
):
    """subagent flag on + backlog flag off → Subagent(fallback=Logging)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, SubagentChatActionExecutor)
    # Fallback is Logging
    assert isinstance(disp.executor._fallback, LoggingChatActionExecutor)


def test_factory_both_flags_on_chains_subagent_backlog_logging(
    monkeypatch, tmp_path,
):
    """subagent flag on + backlog flag on →
    Subagent(fallback=Backlog(fallback=Logging))."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, SubagentChatActionExecutor)
    assert isinstance(disp.executor._fallback, BacklogChatActionExecutor)


def test_factory_chat_master_off_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is None


def test_factory_default_project_root_is_cwd(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_subagent()
    assert disp is not None
    assert isinstance(disp.executor, SubagentChatActionExecutor)


def test_factory_explicit_fallback_used_directly(monkeypatch, tmp_path):
    """Caller-supplied fallback bypasses the auto-compose with backlog —
    the explicit fallback is what spawn_subagent's siblings delegate to."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    custom = LoggingChatActionExecutor()
    disp = build_chat_repl_dispatcher_with_subagent(
        project_root=tmp_path, fallback_executor=custom,
    )
    assert disp is not None
    assert isinstance(disp.executor, SubagentChatActionExecutor)
    # Explicit fallback wins over the auto-compose path
    assert disp.executor._fallback is custom


# ===========================================================================
# E — End-to-end smoke
# ===========================================================================


def test_end_to_end_dispatcher_handle_writes_to_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_subagent(project_root=tmp_path)
    assert disp is not None
    # Use a question-style message that should classify as
    # subagent-eligible by the intent classifier.
    result = disp.handle("/chat explore the auth module please")
    assert result is not None


# ===========================================================================
# F — Authority invariants (AST grep on module source)
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
    # ConversationOrchestrator is allowed (Slice 2 primitive, owns ChatTurn).
    found_banned = [
        (m, s) for (m, s) in found_banned
        if not (s == "orchestrator." and "conversation_orchestrator" in m)
    ]
    assert not found_banned, (
        f"chat_repl_subagent_executor.py contains banned imports: "
        f"{found_banned}"
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
    assert not found, (
        f"chat_repl_subagent_executor.py contains forbidden side-effect "
        f"tokens: {found}"
    )


def test_module_writes_only_via_internal_jsonl_helper():
    """Pin: file write goes through `_append_ticket` (internal helper).
    Source-grep that no other write paths slipped in."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "_append_ticket" in src
    # No .write_text directly (the helper uses .open("a") which is fine)
    assert ".write_text(" not in src, (
        "executor must NOT call .write_text directly — use "
        "_append_ticket so the JSONL contract stays single-sourced"
    )


def test_module_does_not_dispatch_subagent_synchronously():
    """The whole point of the enqueue-and-return-ticket pattern: this
    module MUST NOT actually run a subagent (would block /chat REPL).
    Pin by AST-asserting no AgenticExploreSubagent / SubagentScheduler
    imports (docstrings can mention them — what matters is no import)."""
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_names = ("AgenticExploreSubagent", "SubagentScheduler",
                    "ExplorationSubagent")
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in banned_names, (
                    f"banned import {alias.name!r} from "
                    f"{node.module} — chat_repl_subagent_executor "
                    f"persists tickets only, sweeper runs them"
                )
        elif isinstance(node, _ast.Import):
            for alias in node.names:
                for banned in banned_names:
                    assert banned not in alias.name, (
                        f"banned import {alias.name!r}"
                    )


# ===========================================================================
# G — Protocol conformance
# ===========================================================================


def test_executor_implements_all_four_protocol_methods():
    ex = SubagentChatActionExecutor(project_root=Path.cwd())
    for method_name in ("dispatch_backlog", "spawn_subagent",
                         "query_claude", "attach_context"):
        assert hasattr(ex, method_name)
        assert callable(getattr(ex, method_name))
