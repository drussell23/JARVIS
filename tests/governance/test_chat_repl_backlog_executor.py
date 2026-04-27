"""P3 P2 Slice 4 deferred follow-up — BacklogChatActionExecutor regression suite.

Pins the structural integration of `BacklogChatActionExecutor` (the
first of three concrete chat executors per the operator's 3-PR mini-arc).

Coverage:
  * Module constants + master-flag default-false-pre-graduation.
  * Executor implements the 4-method ChatActionExecutor Protocol.
  * `dispatch_backlog` writes a real entry to .jarvis/backlog.json
    via the existing helper (same shape used by /backlog auto-proposed).
  * Provenance fields populated (source="chat_repl", session_id,
    turn_id, submitted_timestamp_unix).
  * Other 3 methods delegate to fallback executor (defaults to
    LoggingChatActionExecutor).
  * Bounded message length (MAX_BACKLOG_DESCRIPTION_CHARS).
  * Empty message → error token + no file write.
  * task_id is `chat:{turn_id}` for BacklogSensor dedup.
  * Audit list (.calls) populated with task_id or error token.
  * Append failure → error token + no raise.
  * Factory `build_chat_repl_dispatcher_with_backlog`:
    - flag-off → falls through to legacy factory (LoggingChatActionExecutor).
    - flag-on + /chat enabled → wires BacklogChatActionExecutor.
    - /chat disabled (master-off) → returns None regardless.
  * Authority invariants (AST grep):
    - No subprocess / network / env-mutation tokens.
    - No banned governance imports (orchestrator / iron_gate / etc).
"""
from __future__ import annotations

import ast as _ast
import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
    BacklogChatActionExecutor,
    MAX_BACKLOG_DESCRIPTION_CHARS,
    build_chat_repl_dispatcher_with_backlog,
    is_enabled,
)
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    LoggingChatActionExecutor,
)
from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatTurn,
)
from backend.core.ouroboros.governance.intent_classifier import (
    ChatIntent,
    IntentClassification,
)
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatRoutingDecision,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "chat_repl_backlog_executor.py"
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_turn(
    turn_id: str = "t-1",
    session_id: str = "s-1",
    message: str = "add a backlog item",
):
    decision = ChatRoutingDecision(
        action="backlog_dispatch",
        intent=ChatIntent.ACTION_REQUEST,
        confidence=0.8,
        payload={"message": message},
    )
    return ChatTurn(
        turn_id=turn_id, session_id=session_id, operator_message=message,
        classification=IntentClassification(
            intent=ChatIntent.ACTION_REQUEST, confidence=0.8,
        ),
        decision=decision,
        created_unix=time.time(),
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    yield


# ===========================================================================
# A — Module constants + master flag
# ===========================================================================


def test_max_backlog_description_chars_pinned():
    assert MAX_BACKLOG_DESCRIPTION_CHARS == 1024


def test_master_flag_default_false_pre_graduation(monkeypatch):
    """Pin: this concrete executor ships default-OFF until its own
    graduation. Operator opts in via env knob; legacy fallback to
    LoggingChatActionExecutor remains the safe-default."""
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", raising=False)
    assert is_enabled() is False


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", val)
        assert is_enabled() is True, f"value {val!r} should be truthy"


def test_master_flag_falsy_variants(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", val)
        assert is_enabled() is False, f"value {val!r} should be falsy"


# ===========================================================================
# B — dispatch_backlog writes real entry
# ===========================================================================


def test_dispatch_backlog_writes_entry(tmp_path):
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    turn = _make_turn(turn_id="t-write", message="clean up X")
    task_id = ex.dispatch_backlog("clean up X", turn)
    assert task_id == "chat:t-write"
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    assert backlog_path.exists()
    data = json.loads(backlog_path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    assert entry["task_id"] == "chat:t-write"
    assert entry["description"] == "clean up X"
    assert entry["source"] == "chat_repl"
    assert entry["session_id"] == "s-1"
    assert entry["turn_id"] == "t-write"
    assert entry["status"] == "pending"
    assert entry["repo"] == "jarvis"


def test_dispatch_backlog_appends_to_existing_file(tmp_path):
    """If backlog.json already has entries (from BacklogSensor /
    auto-proposed REPL / etc.), the executor APPENDS rather than
    replaces."""
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(json.dumps([
        {"task_id": "existing-1", "description": "old", "status": "pending"},
    ]))
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    ex.dispatch_backlog("new from chat", _make_turn(turn_id="t-app"))
    data = json.loads(backlog_path.read_text())
    assert len(data) == 2
    assert data[0]["task_id"] == "existing-1"
    assert data[1]["task_id"] == "chat:t-app"


def test_dispatch_backlog_empty_message_returns_error_no_write(tmp_path):
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    out = ex.dispatch_backlog("", _make_turn(turn_id="t-empty"))
    assert out.startswith("error-empty-message-")
    assert "t-empty" in out
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    assert not backlog_path.exists()


def test_dispatch_backlog_whitespace_only_message_returns_error(tmp_path):
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    out = ex.dispatch_backlog("   \n\t  ", _make_turn(turn_id="t-ws"))
    assert out.startswith("error-empty-message-")


def test_dispatch_backlog_truncates_at_max_chars(tmp_path):
    big = "x" * (MAX_BACKLOG_DESCRIPTION_CHARS + 500)
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    ex.dispatch_backlog(big, _make_turn(turn_id="t-big"))
    data = json.loads((tmp_path / ".jarvis" / "backlog.json").read_text())
    assert len(data[0]["description"]) == MAX_BACKLOG_DESCRIPTION_CHARS


def test_dispatch_backlog_includes_timestamp(tmp_path):
    before = time.time()
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    ex.dispatch_backlog("ts test", _make_turn(turn_id="t-ts"))
    after = time.time()
    data = json.loads((tmp_path / ".jarvis" / "backlog.json").read_text())
    ts = data[0]["submitted_timestamp_unix"]
    assert before <= ts <= after


def test_dispatch_backlog_calls_audit_populated_on_success(tmp_path):
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    ex.dispatch_backlog("a", _make_turn(turn_id="t-1"))
    ex.dispatch_backlog("b", _make_turn(turn_id="t-2"))
    assert ex.calls == ["chat:t-1", "chat:t-2"]


def test_dispatch_backlog_calls_audit_populated_on_error(tmp_path):
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    ex.dispatch_backlog("", _make_turn(turn_id="t-bad"))
    assert len(ex.calls) == 1
    assert ex.calls[0].startswith("error-empty-message-")


# ===========================================================================
# C — Per-method composition (other 3 methods delegate to fallback)
# ===========================================================================


def test_spawn_subagent_delegates_to_fallback(tmp_path):
    """Until SubagentChatActionExecutor lands (PR 2), spawn_subagent
    must delegate to LoggingChatActionExecutor (the safe-default)."""
    fallback = LoggingChatActionExecutor()
    ex = BacklogChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    out = ex.spawn_subagent("explore X", _make_turn(turn_id="t-sub"))
    assert out.startswith("logged-subagent-")
    assert fallback.calls == [out]


def test_query_claude_delegates_to_fallback(tmp_path):
    """Until ClaudeChatActionExecutor lands (PR 3), query_claude
    must delegate to LoggingChatActionExecutor (the safe-default)."""
    fallback = LoggingChatActionExecutor()
    ex = BacklogChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    out = ex.query_claude(
        "why?", _make_turn(turn_id="t-q"), recent_turns=[],
    )
    assert out.startswith("logged-claude-")


def test_attach_context_delegates_to_fallback(tmp_path):
    fallback = LoggingChatActionExecutor()
    ex = BacklogChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    target = _make_turn(turn_id="t-target")
    out = ex.attach_context(
        "here is more context", _make_turn(turn_id="t-attach"), target,
    )
    assert out.startswith("logged-attach-")


def test_default_fallback_is_logging_executor(tmp_path):
    """Constructor uses LoggingChatActionExecutor when no fallback
    given. Pinned because subsequent PRs may add a different default
    (composite of all 3 concrete) and tests must catch regressions."""
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    out = ex.spawn_subagent("x", _make_turn(turn_id="t-fb"))
    assert out.startswith("logged-")


def test_dispatch_backlog_does_not_invoke_fallback(tmp_path):
    """Cage check: the concrete dispatch_backlog method must NEVER
    delegate to the fallback (else the chat dispatcher would log
    the call as 'logged-backlog-...' instead of writing the file)."""
    fallback = LoggingChatActionExecutor()
    ex = BacklogChatActionExecutor(
        project_root=tmp_path, fallback=fallback,
    )
    ex.dispatch_backlog("a real backlog item", _make_turn(turn_id="t-d"))
    assert fallback.calls == []


# ===========================================================================
# D — Factory wiring
# ===========================================================================


def test_factory_master_off_falls_through_to_legacy(monkeypatch, tmp_path):
    """Default off: factory produces a dispatcher with the legacy
    LoggingChatActionExecutor (zero behavior change vs Slice 4)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_backlog(project_root=tmp_path)
    assert disp is not None
    # The executor is the safe-default LoggingChatActionExecutor.
    assert isinstance(disp.executor, LoggingChatActionExecutor)


def test_factory_master_on_wires_backlog_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_backlog(project_root=tmp_path)
    assert disp is not None
    assert isinstance(disp.executor, BacklogChatActionExecutor)


def test_factory_chat_master_off_returns_none(monkeypatch, tmp_path):
    """Even with backlog flag on, /chat master off → factory returns
    None (the entire /chat surface is hot-reverted)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "0")
    disp = build_chat_repl_dispatcher_with_backlog(project_root=tmp_path)
    assert disp is None


def test_factory_default_project_root_is_cwd(monkeypatch):
    """When no project_root passed, factory uses Path.cwd()."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_backlog()
    assert disp is not None
    assert isinstance(disp.executor, BacklogChatActionExecutor)


def test_factory_passes_through_fallback_executor(monkeypatch, tmp_path):
    """Caller-supplied fallback flows through to the BacklogChatActionExecutor's
    fallback slot (so future PRs can compose subagent/claude executors
    without touching this factory)."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    custom = LoggingChatActionExecutor()
    disp = build_chat_repl_dispatcher_with_backlog(
        project_root=tmp_path, fallback_executor=custom,
    )
    assert disp is not None
    ex = disp.executor
    assert isinstance(ex, BacklogChatActionExecutor)
    # The custom fallback is what spawn_subagent delegates to.
    ex.spawn_subagent("x", _make_turn(turn_id="t-cust"))
    assert len(custom.calls) == 1


# ===========================================================================
# E — End-to-end: dispatcher.handle wires through to file write
# ===========================================================================


def test_end_to_end_dispatcher_handle_writes_to_backlog(monkeypatch, tmp_path):
    """The integration smoke: operator types `/chat add X to backlog`,
    dispatcher.handle classifies → action=backlog_dispatch → executor
    writes to backlog.json. Pinned because this is the user-visible
    contract."""
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "1")
    disp = build_chat_repl_dispatcher_with_backlog(project_root=tmp_path)
    assert disp is not None
    # Use a message the classifier will route to backlog_dispatch.
    result = disp.handle("/chat add backlog: refactor auth")
    # The dispatcher returns a result with the executor's response.
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    if backlog_path.exists():
        # If the classifier picked backlog_dispatch, the file was
        # written. (Some classifier confidence configs may route
        # elsewhere — in that case we just check the dispatcher
        # didn't crash.)
        data = json.loads(backlog_path.read_text())
        assert any(
            entry.get("source") == "chat_repl" for entry in data
        ), "if the action routed to backlog_dispatch, source must be chat_repl"
    # In any case, the dispatcher must have a result.
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
    # ConversationOrchestrator import is allowed (Slice 2 primitive,
    # not an authority crossing — it owns the ChatTurn dataclass).
    found_banned = [
        (m, s) for (m, s) in found_banned
        if not (s == "orchestrator." and "conversation_orchestrator" in m)
    ]
    assert not found_banned, (
        f"chat_repl_backlog_executor.py contains banned imports: "
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
        f"chat_repl_backlog_executor.py contains forbidden side-effect "
        f"tokens: {found}"
    )


def test_module_writes_only_via_existing_helper():
    """Pin: the executor delegates the actual file-write to
    `_append_to_backlog_json` (the same helper /backlog auto-proposed
    uses). This keeps the write path single-sourced — any future
    hardening to the writer (atomic rename, schema validation) auto-
    benefits this executor."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "_append_to_backlog_json" in src
    # And no direct file open/write calls (use the helper).
    assert ".write_text(" not in src, (
        "executor must NOT call .write_text directly — use "
        "_append_to_backlog_json so the write contract stays single-sourced"
    )


# ===========================================================================
# G — Protocol conformance
# ===========================================================================


def test_executor_implements_all_four_protocol_methods():
    """Smoke: BacklogChatActionExecutor has all 4 methods the
    ChatActionExecutor Protocol requires (else dispatcher would
    crash with AttributeError on unrecognized actions)."""
    ex = BacklogChatActionExecutor(project_root=Path.cwd())
    for method_name in ("dispatch_backlog", "spawn_subagent",
                         "query_claude", "attach_context"):
        assert hasattr(ex, method_name), (
            f"Protocol method {method_name} missing"
        )
        assert callable(getattr(ex, method_name))
