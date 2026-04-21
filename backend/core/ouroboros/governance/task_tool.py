"""Venom task tools — deny-by-default observability scratchpad.

Slice 2 of Gap #5. Wraps the Slice 1 :class:`TaskBoard` primitive
with three Venom-surface handlers:

  * ``task_create``   — make a new task (pending)
  * ``task_update``   — edit title/body OR transition state via
                        ``action`` ∈ {"start", "cancel"}
  * ``task_complete`` — mark a task terminal (pending→completed or
                        in_progress→completed)

## Authority posture (locked by authorization)

- **Deny-by-default**. ``JARVIS_TOOL_TASK_BOARD_ENABLED`` defaults
  ``false``; mirrors the Ticket #4 Slice 2 Monitor tool discipline.
- **No write / mutation capability set**. Manifest capabilities are
  the empty frozenset — these tools touch ephemeral in-process
  state, NOT the repo, NOT a subprocess, NOT the network. Rule 0d's
  ``is_read_only`` gate allows them through (not in
  ``_MUTATION_TOOLS``, no ``"write"`` capability).
- **Scratchpad + observability only**. Manifesto §1 + §6: NOTHING
  downstream branches on task state. Never Iron Gate, never
  merge / repair correctness, never tool policy side effects
  beyond allow/deny on THIS tool's own invocation.
- **Single source of truth**. All state mutations flow through
  ``TaskBoard`` APIs; this module never touches board internals
  directly. No parallel state.

## Lifecycle

Per-op TaskBoard registry keyed by ``policy_ctx.op_id``:

  * Lazy-creates a board on the first tool touch for an op
  * ``close_task_board(op_id, reason)`` is the single canonical
    shutdown hook — orchestrator calls it once per op at the
    existing ctx shutdown path. No other seal hook. This matches
    the Option A contract from Slice 1: explicit close, no
    ``__del__`` reliance, deterministic.
  * Registry is process-wide (not disk-backed). Dies with the
    process — respects §4 (no silent persistence of scratch state
    into durable memory).

## Output shape

Each handler returns a ``ToolResult`` with a JSON-serialized
payload. Shared schema:

    {
      "task_id":  str,
      "op_id":    str,
      "state":    "pending" | "in_progress" | "completed" | "cancelled",
      "title":    str,
      "body":     str,
      "sequence": int,
      "active_task_id": str | null,   # current in_progress task or null
      "board_size":     int,           # total task count post-call
    }

On error the handler returns ``ToolExecStatus.EXEC_ERROR`` with
``result.error`` carrying a deterministic reason string. Policy
errors are caught at the policy layer BEFORE the handler runs; the
defense-in-depth re-validation here catches bypass paths.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional, TYPE_CHECKING

from backend.core.ouroboros.governance.task_board import (
    Task,
    TaskBoard,
    TaskBoardCapacityError,
    TaskBoardClosedError,
    TaskBoardError,
    TaskBoardStateError,
)

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext,
        ToolCall,
        ToolResult,
    )


logger = logging.getLogger(__name__)


# --- Env knobs -------------------------------------------------------------


def task_tools_enabled() -> bool:
    """Master switch for the three task-tool handlers.

    Default: **``false``** — deny-by-default. Matches the Ticket #4
    Slice 2 Monitor tool discipline. Slice 4 will flip this default
    to ``true`` after graduation; until then, operators opt in
    explicitly.
    """
    return os.environ.get(
        "JARVIS_TOOL_TASK_BOARD_ENABLED", "false",
    ).strip().lower() == "true"


# --- Per-op board registry -------------------------------------------------


_BOARDS: Dict[str, TaskBoard] = {}
_BOARDS_LOCK = threading.Lock()


def get_or_create_task_board(op_id: str) -> TaskBoard:
    """Return the TaskBoard for ``op_id``, creating lazily on first
    touch. Thread-safe. Called internally by handlers — callers
    outside this module should NOT create boards directly."""
    if not op_id:
        raise ValueError("op_id must be non-empty to create/lookup a board")
    with _BOARDS_LOCK:
        board = _BOARDS.get(op_id)
        if board is None:
            board = TaskBoard(op_id=op_id)
            _BOARDS[op_id] = board
            logger.info(
                "[TaskTool] registry_created op=%s", op_id,
            )
        return board


def close_task_board(op_id: str, reason: str = "") -> bool:
    """Close + evict the TaskBoard for ``op_id`` from the registry.

    The single canonical shutdown hook. Orchestrator calls this once
    per op from the existing ctx shutdown path. Idempotent — calling
    on a missing op_id is a safe no-op.

    Returns True if a board existed + was closed; False if no board
    was registered (op never touched a task tool).
    """
    with _BOARDS_LOCK:
        board = _BOARDS.pop(op_id, None)
    if board is None:
        return False
    try:
        board.close(reason=reason)
    except Exception:  # noqa: BLE001
        # Close is already idempotent + never raises on repeat; this
        # catch is belt-and-suspenders so a close-time issue cannot
        # kill the orchestrator shutdown path.
        logger.debug(
            "[TaskTool] close_task_board raised for op=%s", op_id,
            exc_info=True,
        )
    return True


def reset_task_board_registry() -> None:
    """Test helper — clear the registry. Never called by production
    code. Each test that exercises the handler should call this in
    setup / teardown to prevent cross-test contamination."""
    with _BOARDS_LOCK:
        for board in list(_BOARDS.values()):
            try:
                board.close(reason="test reset")
            except Exception:  # noqa: BLE001
                pass
        _BOARDS.clear()


def registry_size() -> int:
    """Observability helper. Returns the current number of
    registered (open) boards."""
    with _BOARDS_LOCK:
        return len(_BOARDS)


# --- Structural arg validator (shared: policy layer + handler) -------------


_VALID_ACTIONS = frozenset({"start", "cancel"})


def classify_task_args(tool_name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return an error-reason string on rejection, or ``None`` when
    valid. Called by the policy layer AND by the handler (defense-
    in-depth). Matches the ``classify_cmd`` pattern from the
    Monitor tool.
    """
    if args is None or not isinstance(args, dict):
        return "arguments must be a dict"

    if tool_name == "task_create":
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            return "title must be a non-empty string"
        body = args.get("body", "")
        if not isinstance(body, str):
            return "body must be a string when provided"
        return None

    if tool_name == "task_update":
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "task_id must be a non-empty string"
        action = args.get("action")
        if action is not None:
            if not isinstance(action, str) or action not in _VALID_ACTIONS:
                return (
                    "action must be one of "
                    + str(sorted(_VALID_ACTIONS))
                    + " when provided"
                )
            # action \!= None → title/body must not be set (action is
            # state-only; content updates are a separate call shape).
            if "title" in args or "body" in args:
                return (
                    "title/body cannot coexist with action; content "
                    "updates are a separate no-action call shape"
                )
        else:
            # No action → must have at least one of title / body.
            has_title = "title" in args
            has_body = "body" in args
            if not (has_title or has_body):
                return "task_update requires title, body, or action"
            if has_title:
                title = args.get("title")
                if not isinstance(title, str) or not title.strip():
                    return "title must be a non-empty string when provided"
            if has_body:
                body = args.get("body")
                if not isinstance(body, str):
                    return "body must be a string when provided"
        reason = args.get("reason")
        if reason is not None and not isinstance(reason, str):
            return "reason must be a string when provided"
        return None

    if tool_name == "task_complete":
        task_id = args.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "task_id must be a non-empty string"
        return None

    return "unknown task tool: " + repr(tool_name)


# --- Serialization ---------------------------------------------------------


def _serialize_result(
    task: Task, board: TaskBoard,
) -> Dict[str, Any]:
    """Build the shared JSON output shape for every task-tool handler."""
    active = board.active_task()
    snap = board.snapshot()
    return {
        "task_id": task.task_id,
        "op_id": task.op_id,
        "state": task.state,
        "title": task.title,
        "body": task.body,
        "sequence": task.sequence,
        "active_task_id": active.task_id if active is not None else None,
        "board_size": len(snap),
    }


# --- Handler dispatch -------------------------------------------------------


async def run_task_tool(
    call: "ToolCall",
    policy_ctx: "PolicyContext",
    timeout: float,  # unused — task ops are O(1), but signature matches peers
    cap: int,
) -> "ToolResult":
    """Dispatch task_create / task_update / task_complete.

    The handler runs the caller's intent against the op's TaskBoard
    (lazy-created on first touch). Every failure path returns a
    ToolResult with EXEC_ERROR — never raises.
    """
    from backend.core.ouroboros.governance.tool_executor import (
        ToolExecStatus,
        ToolResult,
    )

    name = call.name
    args = call.arguments or {}

    # Defense-in-depth re-validation (policy should have caught
    # already, but the direct-call test surface and any future bypass
    # path must not crash the handler).
    err = classify_task_args(name, args)
    if err is not None:
        return ToolResult(
            tool_call=call, output="", error=err,
            status=ToolExecStatus.EXEC_ERROR,
        )

    if not policy_ctx.op_id:
        return ToolResult(
            tool_call=call, output="",
            error="policy_ctx.op_id is empty — task tools require an op_id",
            status=ToolExecStatus.EXEC_ERROR,
        )

    try:
        board = get_or_create_task_board(policy_ctx.op_id)
        if name == "task_create":
            task = board.create(
                title=args["title"], body=args.get("body", ""),
            )
        elif name == "task_complete":
            task = board.complete(args["task_id"])
        elif name == "task_update":
            task_id = args["task_id"]
            action = args.get("action")
            if action == "start":
                task = board.start(task_id)
            elif action == "cancel":
                task = board.cancel(task_id, reason=args.get("reason", ""))
            else:
                # No action → content update path.
                task = board.update(
                    task_id,
                    title=args.get("title"),
                    body=args.get("body"),
                )
        else:
            return ToolResult(
                tool_call=call, output="",
                error="unknown task tool: " + name,
                status=ToolExecStatus.EXEC_ERROR,
            )
    except TaskBoardClosedError as exc:
        return ToolResult(
            tool_call=call, output="",
            error="board_closed: " + str(exc),
            status=ToolExecStatus.EXEC_ERROR,
        )
    except TaskBoardCapacityError as exc:
        return ToolResult(
            tool_call=call, output="",
            error="capacity: " + str(exc),
            status=ToolExecStatus.EXEC_ERROR,
        )
    except TaskBoardStateError as exc:
        return ToolResult(
            tool_call=call, output="",
            error="state: " + str(exc),
            status=ToolExecStatus.EXEC_ERROR,
        )
    except TaskBoardError as exc:
        return ToolResult(
            tool_call=call, output="",
            error="task_board: " + str(exc),
            status=ToolExecStatus.EXEC_ERROR,
        )
    except Exception as exc:  # noqa: BLE001 — tool boundary, must never raise
        logger.debug(
            "[TaskTool] unexpected exception tool=%s op=%s",
            name, policy_ctx.op_id, exc_info=True,
        )
        return ToolResult(
            tool_call=call, output="",
            error=name + ": " + type(exc).__name__ + ": " + str(exc)[:200],
            status=ToolExecStatus.EXEC_ERROR,
        )

    payload = _serialize_result(task, board)
    output = json.dumps(payload, ensure_ascii=False)
    if len(output) > cap:
        # Truncate the body only (keep header fields intact so the
        # model can still reason about the call).
        truncated = dict(payload)
        truncated["body"] = (
            (truncated.get("body") or "")[:max(0, cap // 4)] + "...<truncated>"
        )
        output = json.dumps(truncated, ensure_ascii=False)
    return ToolResult(
        tool_call=call, output=output, error=None,
        status=ToolExecStatus.SUCCESS,
    )
