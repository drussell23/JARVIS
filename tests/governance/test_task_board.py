"""Regression spine — Gap #5 Slice 1 TaskBoard primitive.

Pins the authority + lifecycle + audit contracts locked by authorization:

  1. State machine: pending / in_progress / completed / cancelled;
     terminal states are sticky; unknown task_id raises.
  2. Single-focus invariant: at most one task in_progress at a time.
  3. Bounded capacity: max tasks / title len / body len all caps,
     deterministic reject via TaskBoardCapacityError.
  4. Stable monotonic IDs: task-{op_id}-{seq:04d}, zero-padded so
     string ordering matches numeric.
  5. Close semantics: explicit close() method, idempotent, post-close
     mutations raise TaskBoardClosedError (explicit > silent
     corruption per authorization).
  6. §8 audit trail: per-transition INFO log line with stable format;
     synchronous with state change.
  7. No __del__ reliance: this module does NOT hook GC for
     correctness — caller-driven lifecycle.
  8. Immutability: Task is a frozen dataclass; snapshot is Tuple.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.task_board import (
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_IN_PROGRESS,
    STATE_PENDING,
    Task,
    TaskBoard,
    TaskBoardCapacityError,
    TaskBoardClosedError,
    TaskBoardError,
    TaskBoardStateError,
)


# ---------------------------------------------------------------------------
# Fixture — isolate env per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_taskboard_env(monkeypatch):
    for key in (
        "JARVIS_TASK_BOARD_MAX_TASKS",
        "JARVIS_TASK_BOARD_MAX_TITLE_LEN",
        "JARVIS_TASK_BOARD_MAX_BODY_LEN",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Construction + input validation
# ---------------------------------------------------------------------------


def test_construction_requires_non_empty_op_id():
    """Slice 1 test 1: op_id is required; empty or non-string raises."""
    with pytest.raises(ValueError):
        TaskBoard(op_id="")
    with pytest.raises(ValueError):
        TaskBoard(op_id=None)  # type: ignore[arg-type]


def test_construction_captures_caps_at_birth(monkeypatch):
    """Slice 1 test 2: capacity caps are read at __init__ and captured
    for the life of the board — mid-op env drift does NOT shift
    behavior. Predictability over reactivity."""
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_TASKS", "3")
    board = TaskBoard(op_id="op-caps")
    assert board.max_tasks == 3
    # Env flip after construction doesn't affect the already-born board.
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_TASKS", "100")
    assert board.max_tasks == 3


# ---------------------------------------------------------------------------
# 2. Create — shape, validation, capacity
# ---------------------------------------------------------------------------


def test_create_returns_pending_task_with_stable_id():
    """Slice 1 test 3: ``create`` returns a Task in pending state with
    a stable ID shape ``task-{op_id}-{seq:04d}``. IDs monotonic +
    zero-padded so string sort matches numeric."""
    board = TaskBoard(op_id="op-alpha")
    t1 = board.create(title="first")
    t2 = board.create(title="second")
    assert t1.state == STATE_PENDING
    assert t1.task_id == "task-op-alpha-0001"
    assert t2.task_id == "task-op-alpha-0002"
    # String ordering matches numeric.
    assert sorted([t2.task_id, t1.task_id]) == [t1.task_id, t2.task_id]
    assert t1.op_id == "op-alpha"
    assert t1.title == "first"
    assert t1.body == ""


def test_create_rejects_empty_title():
    board = TaskBoard(op_id="op-empty")
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="")
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="   ")  # whitespace-only


def test_create_rejects_title_over_cap(monkeypatch):
    """Slice 1 test 5: title length cap enforced."""
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_TITLE_LEN", "10")
    board = TaskBoard(op_id="op-title-cap")
    board.create(title="short")  # under cap — OK
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="x" * 11)


def test_create_rejects_body_over_cap(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_BODY_LEN", "10")
    board = TaskBoard(op_id="op-body-cap")
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="t", body="x" * 11)


def test_create_capacity_cap_rejects_deterministically(monkeypatch):
    """Slice 1 test 7 (CRITICAL): overflow beyond max_tasks raises
    TaskBoardCapacityError — does NOT silently coalesce / reorder /
    drop, per authorization."""
    monkeypatch.setenv("JARVIS_TASK_BOARD_MAX_TASKS", "3")
    board = TaskBoard(op_id="op-cap")
    for i in range(3):
        board.create(title="t-" + str(i))
    with pytest.raises(TaskBoardCapacityError):
        board.create(title="overflow")
    # Exactly 3 still there, none corrupted.
    snap = board.snapshot()
    assert len(snap) == 3
    assert [t.sequence for t in snap] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 3. State machine
# ---------------------------------------------------------------------------


def test_start_transitions_pending_to_in_progress():
    board = TaskBoard(op_id="op-sm")
    t = board.create(title="work")
    started = board.start(t.task_id)
    assert started.state == STATE_IN_PROGRESS
    assert board.active_task() is not None
    assert board.active_task().task_id == t.task_id


def test_complete_transitions_from_pending_quick_win():
    """Slice 1 test 9: pending → completed (skip in_progress). Quick-win
    path; documented in the state-machine ASCII art."""
    board = TaskBoard(op_id="op-quick")
    t = board.create(title="trivial")
    done = board.complete(t.task_id)
    assert done.state == STATE_COMPLETED
    assert done.terminal_ts is not None


def test_complete_transitions_from_in_progress():
    board = TaskBoard(op_id="op-flow")
    t = board.create(title="work")
    board.start(t.task_id)
    done = board.complete(t.task_id)
    assert done.state == STATE_COMPLETED
    # Active cleared.
    assert board.active_task() is None


def test_cancel_transitions_from_pending():
    board = TaskBoard(op_id="op-cancel")
    t = board.create(title="abandoned")
    cancelled = board.cancel(t.task_id, reason="no longer relevant")
    assert cancelled.state == STATE_CANCELLED
    assert cancelled.cancel_reason == "no longer relevant"


def test_cancel_transitions_from_in_progress_clears_active():
    board = TaskBoard(op_id="op-cancel-active")
    t = board.create(title="work")
    board.start(t.task_id)
    assert board.active_task() is not None
    board.cancel(t.task_id)
    assert board.active_task() is None


def test_terminal_states_are_sticky_completed():
    """Slice 1 test 13: completed → cancelled raises.
    Terminal-state invariant."""
    board = TaskBoard(op_id="op-sticky-c")
    t = board.create(title="done")
    board.complete(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.cancel(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.complete(t.task_id)  # also blocked
    with pytest.raises(TaskBoardStateError):
        board.start(t.task_id)


def test_terminal_states_are_sticky_cancelled():
    board = TaskBoard(op_id="op-sticky-x")
    t = board.create(title="abandoned")
    board.cancel(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.complete(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.cancel(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.start(t.task_id)


def test_unknown_task_id_raises():
    board = TaskBoard(op_id="op-unknown")
    with pytest.raises(TaskBoardStateError):
        board.start("task-nonexistent")
    with pytest.raises(TaskBoardStateError):
        board.complete("task-nonexistent")
    with pytest.raises(TaskBoardStateError):
        board.cancel("task-nonexistent")


# ---------------------------------------------------------------------------
# 4. Single-focus invariant
# ---------------------------------------------------------------------------


def test_single_focus_second_start_raises():
    """Slice 1 test 16 (CRITICAL): at most one in_progress at a time.
    Starting a second task while one is active raises
    TaskBoardStateError. Matches "what am I working on right now"
    intent — one focus, not a stack."""
    board = TaskBoard(op_id="op-focus")
    a = board.create(title="A")
    b = board.create(title="B")
    board.start(a.task_id)
    with pytest.raises(TaskBoardStateError) as excinfo:
        board.start(b.task_id)
    assert "single-focus" in str(excinfo.value).lower()


def test_single_focus_clears_after_complete():
    """Slice 1 test 17: after completing the active task, another
    pending task CAN be started — the focus slot is freed."""
    board = TaskBoard(op_id="op-focus-clear")
    a = board.create(title="A")
    b = board.create(title="B")
    board.start(a.task_id)
    board.complete(a.task_id)
    # Slot is free; b.start succeeds.
    board.start(b.task_id)
    active = board.active_task()
    assert active is not None
    assert active.task_id == b.task_id


def test_active_task_returns_none_when_nothing_in_progress():
    board = TaskBoard(op_id="op-no-active")
    board.create(title="pending-only")
    assert board.active_task() is None


# ---------------------------------------------------------------------------
# 5. Update
# ---------------------------------------------------------------------------


def test_update_title_and_body():
    board = TaskBoard(op_id="op-upd")
    t = board.create(title="original", body="body")
    updated = board.update(t.task_id, title="revised", body="body2")
    assert updated.title == "revised"
    assert updated.body == "body2"


def test_update_rejects_terminal_state():
    """Slice 1 test 20: content is frozen at terminal transition —
    completed/cancelled tasks cannot be updated."""
    board = TaskBoard(op_id="op-upd-term")
    t = board.create(title="t")
    board.complete(t.task_id)
    with pytest.raises(TaskBoardStateError):
        board.update(t.task_id, title="nope")


def test_update_requires_at_least_one_field():
    board = TaskBoard(op_id="op-upd-empty")
    t = board.create(title="t")
    with pytest.raises(TaskBoardStateError):
        board.update(t.task_id)  # no title, no body


def test_update_noop_when_values_unchanged():
    """Slice 1 test 22: updating with identical values is a silent
    no-op — does NOT log, does NOT bump updated_ts. Reduces audit
    noise on tautological updates."""
    board = TaskBoard(op_id="op-upd-noop")
    t = board.create(title="same", body="same")
    updated = board.update(t.task_id, title="same", body="same")
    # Identical state + timestamp — no log, no mutation.
    assert updated == t


# ---------------------------------------------------------------------------
# 6. Close semantics — explicit lifecycle (NOT __del__ / GC)
# ---------------------------------------------------------------------------


def test_close_marks_board_closed():
    board = TaskBoard(op_id="op-close")
    assert board.closed is False
    board.close(reason="op terminated")
    assert board.closed is True


def test_close_is_idempotent():
    """Slice 1 test 24: second close() call is a no-op — does NOT
    raise, does NOT re-log. Defensive against callers who aren't
    certain whether close was already invoked."""
    board = TaskBoard(op_id="op-close-twice")
    board.close()
    # Second close — must not raise.
    board.close()
    assert board.closed is True


def test_mutation_after_close_raises_explicit_error():
    """Slice 1 test 25 (CRITICAL, authorization-locked):
    post-close mutations raise TaskBoardClosedError. Explicit-over-
    silent — per authorization, we chose RuntimeError over no-op
    to avoid silent corruption."""
    board = TaskBoard(op_id="op-close-mutate")
    t = board.create(title="pre")
    board.close()
    with pytest.raises(TaskBoardClosedError):
        board.create(title="post")
    with pytest.raises(TaskBoardClosedError):
        board.start(t.task_id)
    with pytest.raises(TaskBoardClosedError):
        board.complete(t.task_id)
    with pytest.raises(TaskBoardClosedError):
        board.cancel(t.task_id)
    with pytest.raises(TaskBoardClosedError):
        board.update(t.task_id, title="x")


def test_reads_after_close_still_work():
    """Slice 1 test 26: snapshot / active_task / get / closed all
    remain callable post-close. The in-memory board is still a
    valid read surface — mutations are what's forbidden."""
    board = TaskBoard(op_id="op-close-read")
    t = board.create(title="readable")
    board.close()
    assert board.snapshot()[0].task_id == t.task_id
    assert board.active_task() is None
    assert board.get(t.task_id) is not None
    assert board.closed is True


# ---------------------------------------------------------------------------
# 7. §8 audit trail — per-transition INFO log lines
# ---------------------------------------------------------------------------


def test_log_line_on_task_created(caplog):
    board = TaskBoard(op_id="op-log-create")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.task_board",
    )
    board.create(title="hello world")
    hits = [r for r in caplog.records if "task_created" in r.getMessage()]
    assert hits, "expected task_created log line"
    msg = hits[0].getMessage()
    assert "op=op-log-create" in msg
    assert "task_id=task-op-log-create-0001" in msg
    assert "sequence=1" in msg
    assert "hello world" in msg


def test_log_line_on_task_started_and_completed(caplog):
    board = TaskBoard(op_id="op-log-flow")
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.task_board",
    )
    t = board.create(title="t")
    board.start(t.task_id)
    board.complete(t.task_id)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("task_started" in m and t.task_id in m for m in msgs)
    assert any("task_completed" in m and t.task_id in m for m in msgs)


def test_log_line_on_board_closed(caplog):
    caplog.set_level(
        logging.INFO,
        logger="backend.core.ouroboros.governance.task_board",
    )
    board = TaskBoard(op_id="op-log-close")
    board.create(title="one")
    board.create(title="two")
    board.close(reason="op complete")
    hits = [r for r in caplog.records if "board_closed" in r.getMessage()]
    assert hits
    msg = hits[0].getMessage()
    assert "op=op-log-close" in msg
    assert "final_task_count=2" in msg
    assert "op complete" in msg


# ---------------------------------------------------------------------------
# 8. Immutability
# ---------------------------------------------------------------------------


def test_task_is_frozen_dataclass():
    """Slice 1 test 30: Task is frozen — external callers cannot mutate
    its fields. Updates flow through board.update() and produce a new
    Task instance."""
    board = TaskBoard(op_id="op-frozen")
    t = board.create(title="t")
    with pytest.raises(Exception):  # FrozenInstanceError (dataclass)
        t.title = "tampered"  # type: ignore[misc]


def test_snapshot_is_immutable_tuple():
    board = TaskBoard(op_id="op-snap")
    board.create(title="a")
    snap = board.snapshot()
    assert isinstance(snap, tuple)


# ---------------------------------------------------------------------------
# 9. Authorization-locked invariant — no __del__ hook
# ---------------------------------------------------------------------------


def test_taskboard_does_not_implement_dunder_del():
    """Slice 1 test 32 (CRITICAL, authorization-locked): per Option A
    non-negotiables, TaskBoard MUST NOT implement __del__. Lifecycle
    is explicit via close(); GC is NOT the audit trail. Greps the
    module source for __del__ — fails loudly if anyone adds one."""
    src = Path(
        "backend/core/ouroboros/governance/task_board.py"
    ).read_text()
    # Check for __del__ at method-level only (not in docstrings/comments).
    for line in src.splitlines():
        stripped = line.strip()
        # Allow mentions in comments / docstrings; forbid actual method def.
        if stripped.startswith("def __del__"):
            raise AssertionError(
                "Option A authorization violation: TaskBoard has a "
                "__del__ method. Lifecycle MUST be explicit via "
                "close(); no GC-time correctness."
            )


def test_taskboard_module_documents_option_a_contract():
    """Slice 1 test 33 (bit-rot guard): the module docstring carries
    the Option A lifecycle-contract language. Future refactors that
    strip the documentation fail loudly."""
    import backend.core.ouroboros.governance.task_board as module
    doc = (module.__doc__ or "").lower()
    assert "option a" in doc or "lazy" in doc
    assert "close" in doc
    assert "del" in doc  # references to __del__ in the rationale
