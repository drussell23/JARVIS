"""TaskBoard — ephemeral per-op "what am I working on right now" primitive.

Closes Gap #5 (structured to-do lists) at the primitive layer. Slice 1
of 4: self-contained + unit-tested + no orchestrator wiring. Slice 2
adds Venom ``task_create`` / ``task_update`` / ``task_complete`` tools
on top; Slice 3 optional advisory prompt injection; Slice 4 graduation.

## Authority posture (locked by authorization)

- **Observability-only** — Slice 1 emits structured state + a §8
  per-transition audit log. NOTHING branches on task state. Never
  Iron Gate, never policy, never validator / approval / merge gates.
- **Per-op lifetime** — the board is owned by whatever owns the op
  (typically ``OperationContext``). Option A locked-in: lazy
  attachment, no explicit FSM hook, ephemeral with ctx. Audit history
  lives in the log pipeline, NOT in model-rewritable structures.
- **No ``__del__`` reliance** — per authorization, lifecycle is
  whatever explicitly drops / closes the ctx. This module does NOT
  implement ``__del__``; callers invoke ``close()`` when
  appropriate. Post-close mutations raise ``TaskBoardClosedError``
  (explicit over silent).

## State machine (strict)

    pending ──start()───────> in_progress ──complete()─> completed
       │                         │
       │                         └───cancel()──────────> cancelled
       │
       ├───complete()─────────────────────────────────── completed  (quick-win path)
       │
       └───cancel()──────────────────────────────────── cancelled

Terminal states are sticky: ``completed`` and ``cancelled`` do not
transition further. ``completed → cancelled`` and ``cancelled →
completed`` both raise ``TaskBoardStateError``.

## Single-focus invariant (Slice 1 design)

At most ONE task may be in ``in_progress`` state at a time. Attempting
to start a second active task raises ``TaskBoardStateError``. This
matches the "what am I working on right NOW" intent — one focus, not
a stack. CC's TaskCreate/TaskUpdate surface allows multiple
simultaneous ``in_progress`` tasks; Slice 1 intentionally diverges
from that looser semantic until Slice 2 Venom wiring surfaces a
concrete need to relax it (at which point the relaxation gets its
own tests + comment).

## §8 audit trail

Every state transition emits a synchronous INFO log line BEFORE the
method returns to the caller. The log line is the authoritative
history — in-memory board state can be read for immediate queries
but is NOT the audit surface. Operators grep debug.log for
``[TaskBoard]`` markers:

    [TaskBoard] task_created op=X task_id=Y sequence=N title=<preview>
    [TaskBoard] task_started op=X task_id=Y
    [TaskBoard] task_completed op=X task_id=Y
    [TaskBoard] task_cancelled op=X task_id=Y reason=<R>
    [TaskBoard] task_updated op=X task_id=Y fields=<F>
    [TaskBoard] board_closed op=X reason=<R> final_task_count=N
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# --- State constants -------------------------------------------------------


STATE_PENDING = "pending"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"
STATE_CANCELLED = "cancelled"

_TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_CANCELLED})
_ALL_STATES = frozenset({
    STATE_PENDING, STATE_IN_PROGRESS, STATE_COMPLETED, STATE_CANCELLED,
})


# --- Env-driven caps -------------------------------------------------------


def _max_tasks() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_TASK_BOARD_MAX_TASKS", "50")))
    except (TypeError, ValueError):
        return 50


def _max_title_len() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_TASK_BOARD_MAX_TITLE_LEN", "200",
        )))
    except (TypeError, ValueError):
        return 200


def _max_body_len() -> int:
    try:
        return max(0, int(os.environ.get(
            "JARVIS_TASK_BOARD_MAX_BODY_LEN", "2000",
        )))
    except (TypeError, ValueError):
        return 2000


# --- Data types ------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """One task on the board. Immutable — updates produce a new Task."""

    task_id: str
    op_id: str
    title: str
    body: str
    state: str  # one of _ALL_STATES
    sequence: int
    created_ts: float
    updated_ts: float
    terminal_ts: Optional[float] = None
    cancel_reason: str = ""


# --- Exceptions ------------------------------------------------------------


class TaskBoardError(Exception):
    """Base for all TaskBoard errors — lets callers catch broadly."""


class TaskBoardCapacityError(TaskBoardError):
    """Bounded-capacity violation (max tasks / title len / body len)."""


class TaskBoardStateError(TaskBoardError):
    """Illegal state transition, unknown task_id, or single-focus breach."""


class TaskBoardClosedError(TaskBoardError):
    """Attempted mutation on a closed board."""


# --- TaskBoard -------------------------------------------------------------


class TaskBoard:
    """Per-op ephemeral to-do list with immutable audit trail via logging.

    Lifetime contract (Option A, locked by authorization):
      * Lazily attached to whatever owns the op (typically
        ``OperationContext``). This module does NOT know about ctx —
        callers wire the attachment themselves.
      * Ephemeral — in-memory state dies when the owning ctx is
        released. The history lives in the per-transition INFO log
        pipeline, NOT in this object.
      * No ``__del__`` / GC reliance. Callers invoke ``close()``
        when appropriate. Post-close mutations raise
        ``TaskBoardClosedError`` (explicit > silent corruption).
    """

    def __init__(self, op_id: str) -> None:
        if not op_id or not isinstance(op_id, str):
            raise ValueError("op_id must be a non-empty string")
        self._op_id = op_id
        self._tasks: Dict[str, Task] = {}
        self._insertion_order: List[str] = []
        self._sequence_counter: int = 0
        self._active_task_id: Optional[str] = None
        self._lock = threading.Lock()
        self._closed: bool = False
        # Capture caps at construction — avoids env-drift mid-op.
        self._max_tasks = _max_tasks()
        self._max_title_len = _max_title_len()
        self._max_body_len = _max_body_len()

    # --- properties --------------------------------------------------------

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def closed(self) -> bool:
        """True once ``close()`` has been called. Read-only methods remain
        usable after close; all mutations raise ``TaskBoardClosedError``."""
        return self._closed

    @property
    def max_tasks(self) -> int:
        return self._max_tasks

    # --- read API ----------------------------------------------------------

    def snapshot(self) -> Tuple[Task, ...]:
        """Immutable snapshot in insertion order. Safe to call post-close."""
        with self._lock:
            return tuple(self._tasks[tid] for tid in self._insertion_order)

    def active_task(self) -> Optional[Task]:
        """The single ``in_progress`` task, or None when nothing is active.
        Reflects the single-focus invariant enforced at ``start()``."""
        with self._lock:
            if self._active_task_id is None:
                return None
            return self._tasks.get(self._active_task_id)

    def get(self, task_id: str) -> Optional[Task]:
        """Lookup by ID. Returns None for unknown IDs."""
        with self._lock:
            return self._tasks.get(task_id)

    # --- write API ---------------------------------------------------------

    def create(self, title: str, body: str = "") -> Task:
        """Create a ``pending`` task. Raises on capacity violations or
        invalid input. Emits [TaskBoard] task_created log synchronously."""
        self._check_not_closed()
        if not isinstance(title, str) or not title.strip():
            raise TaskBoardCapacityError("title must be a non-empty string")
        if len(title) > self._max_title_len:
            raise TaskBoardCapacityError(
                "title length "
                + str(len(title)) + " exceeds cap " + str(self._max_title_len)
            )
        if not isinstance(body, str):
            raise TaskBoardCapacityError("body must be a string")
        if len(body) > self._max_body_len:
            raise TaskBoardCapacityError(
                "body length "
                + str(len(body)) + " exceeds cap " + str(self._max_body_len)
            )

        with self._lock:
            if len(self._tasks) >= self._max_tasks:
                raise TaskBoardCapacityError(
                    "board at capacity ("
                    + str(self._max_tasks) + " tasks); rejecting create"
                )
            self._sequence_counter += 1
            seq = self._sequence_counter
            task_id = "task-" + self._op_id + "-" + str(seq).zfill(4)
            now = time.monotonic()
            task = Task(
                task_id=task_id,
                op_id=self._op_id,
                title=title,
                body=body,
                state=STATE_PENDING,
                sequence=seq,
                created_ts=now,
                updated_ts=now,
                terminal_ts=None,
                cancel_reason="",
            )
            self._tasks[task_id] = task
            self._insertion_order.append(task_id)

        # Log AFTER lock release — avoid holding the lock across logging
        # I/O. The task is already committed to state; the log line is
        # part of the audit trail.
        logger.info(
            "[TaskBoard] task_created op=%s task_id=%s sequence=%d title=%.80s",
            self._op_id, task_id, seq, title,
        )
        return task

    def start(self, task_id: str) -> Task:
        """Transition ``pending → in_progress``. Enforces single-focus:
        if another task is already ``in_progress``, raises
        ``TaskBoardStateError``. Use ``complete`` or ``cancel`` on the
        active task first."""
        self._check_not_closed()
        with self._lock:
            task = self._require_task(task_id)
            if task.state != STATE_PENDING:
                raise TaskBoardStateError(
                    "cannot start from state "
                    + task.state + " (must be pending)"
                )
            if (
                self._active_task_id is not None
                and self._active_task_id != task_id
            ):
                raise TaskBoardStateError(
                    "single-focus violation: task "
                    + self._active_task_id
                    + " is already in_progress; complete or cancel it first"
                )
            updated = replace(
                task, state=STATE_IN_PROGRESS, updated_ts=time.monotonic(),
            )
            self._tasks[task_id] = updated
            self._active_task_id = task_id

        logger.info(
            "[TaskBoard] task_started op=%s task_id=%s",
            self._op_id, task_id,
        )
        return updated

    def complete(self, task_id: str) -> Task:
        """Transition ``pending → completed`` (quick-win path) or
        ``in_progress → completed``. Terminal-state tasks raise."""
        self._check_not_closed()
        with self._lock:
            task = self._require_task(task_id)
            if task.state in _TERMINAL_STATES:
                raise TaskBoardStateError(
                    "cannot complete from terminal state " + task.state
                )
            if task.state not in (STATE_PENDING, STATE_IN_PROGRESS):
                raise TaskBoardStateError(
                    "unexpected state " + task.state
                )
            now = time.monotonic()
            updated = replace(
                task, state=STATE_COMPLETED,
                updated_ts=now, terminal_ts=now,
            )
            self._tasks[task_id] = updated
            if self._active_task_id == task_id:
                self._active_task_id = None

        logger.info(
            "[TaskBoard] task_completed op=%s task_id=%s",
            self._op_id, task_id,
        )
        return updated

    def cancel(self, task_id: str, reason: str = "") -> Task:
        """Transition any non-terminal state to ``cancelled``. Optional
        ``reason`` is captured in the Task and logged."""
        self._check_not_closed()
        if not isinstance(reason, str):
            reason = ""
        with self._lock:
            task = self._require_task(task_id)
            if task.state in _TERMINAL_STATES:
                raise TaskBoardStateError(
                    "cannot cancel from terminal state " + task.state
                )
            now = time.monotonic()
            updated = replace(
                task, state=STATE_CANCELLED,
                updated_ts=now, terminal_ts=now,
                cancel_reason=reason[:self._max_body_len],
            )
            self._tasks[task_id] = updated
            if self._active_task_id == task_id:
                self._active_task_id = None

        logger.info(
            "[TaskBoard] task_cancelled op=%s task_id=%s reason=%.200s",
            self._op_id, task_id, reason or "",
        )
        return updated

    def update(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Task:
        """Update title and/or body. Terminal-state tasks cannot be
        updated — content is frozen at the terminal transition."""
        self._check_not_closed()
        if title is None and body is None:
            raise TaskBoardStateError(
                "update requires at least one of title/body"
            )
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise TaskBoardCapacityError(
                    "title must be a non-empty string"
                )
            if len(title) > self._max_title_len:
                raise TaskBoardCapacityError(
                    "title length " + str(len(title))
                    + " exceeds cap " + str(self._max_title_len)
                )
        if body is not None:
            if not isinstance(body, str):
                raise TaskBoardCapacityError("body must be a string")
            if len(body) > self._max_body_len:
                raise TaskBoardCapacityError(
                    "body length " + str(len(body))
                    + " exceeds cap " + str(self._max_body_len)
                )

        fields_changed: List[str] = []
        with self._lock:
            task = self._require_task(task_id)
            if task.state in _TERMINAL_STATES:
                raise TaskBoardStateError(
                    "cannot update terminal-state task (" + task.state + ")"
                )
            new_fields: Dict[str, object] = {"updated_ts": time.monotonic()}
            if title is not None and title != task.title:
                new_fields["title"] = title
                fields_changed.append("title")
            if body is not None and body != task.body:
                new_fields["body"] = body
                fields_changed.append("body")
            if not fields_changed:
                # No-op update — don't bump updated_ts, don't log.
                return task
            updated = replace(task, **new_fields)
            self._tasks[task_id] = updated

        logger.info(
            "[TaskBoard] task_updated op=%s task_id=%s fields=%s",
            self._op_id, task_id, ",".join(fields_changed),
        )
        return updated

    # --- close -------------------------------------------------------------

    def close(self, reason: str = "") -> None:
        """Explicit close. Idempotent — second call is a no-op without
        raising or re-logging. After close, all mutation methods raise
        ``TaskBoardClosedError``; read methods (``snapshot``,
        ``active_task``, ``get``, ``closed``) remain usable.

        Per Option A locked contract: callers invoke this when the
        owning ``OperationContext`` is released. This module does NOT
        implement ``__del__`` — lifecycle is explicit.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            final_count = len(self._tasks)

        logger.info(
            "[TaskBoard] board_closed op=%s reason=%.200s final_task_count=%d",
            self._op_id, reason or "", final_count,
        )

    # --- internals ---------------------------------------------------------

    def _check_not_closed(self) -> None:
        if self._closed:
            raise TaskBoardClosedError(
                "TaskBoard for op=" + self._op_id
                + " is closed; mutations refused. "
                "Per Option A, lifecycle is explicit — the owning "
                "OperationContext released this board."
            )

    def _require_task(self, task_id: str) -> Task:
        """Caller must hold ``self._lock``."""
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskBoardStateError(
                "unknown task_id " + repr(task_id)
            )
        return task
