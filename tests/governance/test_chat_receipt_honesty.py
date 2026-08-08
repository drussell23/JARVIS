"""The cockpit told the operator three things and all three were false.

The operator typed a real request into `ov`:

    ❯ add a module docstring to .../session_identity.py explaining the
      pytest isolation rule
    ● adding that to the backlog
      ⎿ queued (logged-backlog-chat-cbda1fd0f016) — the Backlog sensor
        will pick it up on its next sweep

Nothing was added. Nothing was queued. There was nothing for the sensor to
pick up. Verified on the live tree at the time: `.jarvis/backlog.json` was
last modified in April and contained zero chat entries.

WHY
---
`backlog_dispatch` has THREE outcomes and the renderer had TWO branches:

    op-…      intake accepted it, a worker has it        → say nothing
    chat:…    filed in backlog.json for the sensor       → "queued"
    logged-…  the safe-default executor DECLINED         → fell through

`LoggingChatActionExecutor` is wired whenever
`JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED` is off. It logs the message and
returns a synthetic token it deliberately prefixes `logged-` — it even
declares `LABEL_PREFIX = "logged-"` as a class attribute. That prefix is the
executor saying, in the only channel it has, "I did not do this."

Nothing read it. The third reality fell into the second branch, so the most
comfortable of the three answers was the one printed.

Same defect as the rest of this arc — a surface reporting a state it did not
measure — with the sharpest consequence available, because this one eats the
operator's work and thanks them for it.

THE FIX IS BOTH HALVES
----------------------
Reading the prefix makes "off" visibly off. Flipping the flag makes it not
off. Either alone is wrong: honesty without capability just documents a dead
end, and capability without honesty leaves the next inert executor free to
lie the same way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.chat_repl_dispatcher import (  # noqa: E402
    LoggingChatActionExecutor,
)
from backend.core.ouroboros.governance.chat_response_style import (  # noqa: E402
    _dispatched_now,
    _logging_prefix,
    _queue_note,
    _was_discarded,
)


# ---------------------------------------------------------------------------
# the three realities
# ---------------------------------------------------------------------------

def test_a_discarded_request_is_reported_as_discarded() -> None:
    """THE regression. This exact receipt printed "queued" for months."""
    note = _queue_note("backlog_dispatch", "logged-backlog-chat-cbda1fd0f016")
    assert note is not None
    assert "NOT queued" in note
    assert "queued (" not in note, "still claiming the entry was filed"


def test_the_operator_is_told_which_flag_to_set() -> None:
    """Someone who has just watched their request vanish should not have to
    grep for why."""
    note = _queue_note("backlog_dispatch", "logged-x")
    assert "JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED" in note


def test_a_filed_request_still_says_queued() -> None:
    note = _queue_note("backlog_dispatch", "chat:turn-123")
    assert note is not None and "queued (chat:turn-123)" in note
    assert "NOT queued" not in note


def test_a_running_request_says_nothing() -> None:
    """An op-id means a worker already has it; a queue note would be wrong."""
    assert _queue_note("backlog_dispatch", "op-019fd3a6-23d8-7bb2") is None
    assert _dispatched_now("op-019fd3a6-23d8-7bb2") is True


@pytest.mark.parametrize("action", ["subagent_explore", "claude_query",
                                    "context_attach", "social_ack", "noop"])
def test_non_deferred_actions_are_unaffected(action) -> None:
    assert _queue_note(action, "logged-x") is None


# ---------------------------------------------------------------------------
# one authority for what "logged" looks like
# ---------------------------------------------------------------------------

def test_the_prefix_comes_from_the_executor_not_a_copy() -> None:
    """Two spellings of the same marker is how this breaks again: the
    executor renames its prefix, the renderer keeps matching the old one, and
    the lie returns silently."""
    assert _logging_prefix() == LoggingChatActionExecutor.LABEL_PREFIX


def test_the_executor_still_stamps_that_prefix() -> None:
    """The other direction. If the safe default stops marking its receipts,
    a discarded request becomes indistinguishable from a filed one again."""
    assert LoggingChatActionExecutor.LABEL_PREFIX
    assert str(LoggingChatActionExecutor.LABEL_PREFIX).strip()


@pytest.mark.parametrize("receipt,discarded", [
    ("logged-backlog-chat-abc", True),
    ("chat:turn-1", False),
    ("op-019f-7abc", False),
    ("", False),
    ("   ", False),
    (None, False),
])
def test_discard_detection_is_exact(receipt, discarded) -> None:
    assert _was_discarded(receipt) is discarded


def test_an_empty_receipt_is_not_called_discarded() -> None:
    """An executor that returned nothing is a different failure from one that
    declined. Claiming the flag is off would send the operator to fix
    something that is already on."""
    note = _queue_note("backlog_dispatch", "")
    assert note is not None and "NOT queued" not in note


def test_the_renderer_survives_a_missing_dispatcher(monkeypatch) -> None:
    """`_logging_prefix` lazily imports the dispatcher. A render path must not
    raise because an import failed."""
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **k):
        if "chat_repl_dispatcher" in name:
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert _logging_prefix() == "logged-"
    assert "NOT queued" in _queue_note("backlog_dispatch", "logged-x")


# ---------------------------------------------------------------------------
# the capability half — the executor was correct, tested, and switched off
# ---------------------------------------------------------------------------

def test_the_backlog_executor_is_on_by_default(monkeypatch) -> None:
    """Merged and inert is the defect this codebase keeps shipping. An
    executor that writes one bounded, deduped file has no business being the
    reason the operator's input disappears."""
    from backend.core.ouroboros.governance import chat_repl_backlog_executor as ex
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", raising=False)
    assert ex.is_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_the_flag_still_switches_it_off(monkeypatch, raw, expected) -> None:
    from backend.core.ouroboros.governance import chat_repl_backlog_executor as ex
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", raw)
    assert ex.is_enabled() is expected


def test_a_dispatched_request_actually_lands_on_disk(tmp_path) -> None:
    """End to end through the real executor. The receipt shape and the file
    must agree — a `chat:` receipt over an unwritten file would be the same
    lie one layer down."""
    import inspect
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        BacklogChatActionExecutor,
    )
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        ChatTurn,
    )
    (tmp_path / ".jarvis").mkdir()
    kw = {n: ("t-1" if "id" in n else "x")
          for n, p in inspect.signature(ChatTurn).parameters.items()
          if p.default is inspect.Parameter.empty}
    receipt = BacklogChatActionExecutor(project_root=tmp_path).dispatch_backlog(
        "add a docstring explaining the pytest isolation rule", ChatTurn(**kw))

    assert not _was_discarded(receipt), receipt
    assert receipt.startswith("chat:")

    raw = json.loads((tmp_path / ".jarvis" / "backlog.json").read_text())
    items = raw if isinstance(raw, list) else raw.get("tasks") or raw.get("items")
    assert any("pytest isolation rule" in json.dumps(t) for t in items)
    # And the line the operator sees is now true of the file that exists.
    assert "queued" in _queue_note("backlog_dispatch", receipt)


def test_the_same_turn_twice_is_idempotent(tmp_path) -> None:
    """`chat:{turn_id}` dedup at the sensor. An operator retrying a request
    must not fan out into two ops."""
    import inspect
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        BacklogChatActionExecutor,
    )
    from backend.core.ouroboros.governance.conversation_orchestrator import (
        ChatTurn,
    )
    (tmp_path / ".jarvis").mkdir()
    kw = {n: ("t-dup" if "id" in n else "x")
          for n, p in inspect.signature(ChatTurn).parameters.items()
          if p.default is inspect.Parameter.empty}
    ex = BacklogChatActionExecutor(project_root=tmp_path)
    a = ex.dispatch_backlog("same request", ChatTurn(**kw))
    b = ex.dispatch_backlog("same request", ChatTurn(**kw))
    assert a == b, "the receipt must be stable for a retried turn"
