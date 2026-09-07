"""Subagent Diagnostic Interceptor — subagent failures learn, in the open.

Why this exists
---------------
The execution-graph bridge backups of 2026-09-07 showed 22 failed work units
across the day's soaks. Every one carried the literal ``error="validation
failed"`` and nothing else: the executor discarded the runner's verdict, the
graph reported ``work_unit_failed``, no WARNING reached the headless soak log
(the lifecycle lines are INFO), and no lesson was written — so the next op
repeated the same failure against the same file, forty minutes at a time.

The fixed-type subagents (EXPLORE / REVIEW / PLAN / GENERAL) have the same
gap one layer up: ``SubagentOrchestrator`` converts every failure into a
structured ``SubagentResult`` and hands it to a ``CommSink``, but nothing
turned a ``FAILED`` result into memory.

What this module does
---------------------
* :class:`SubagentDiagnosticSink` — a ``CommSink`` DECORATOR (the same shape
  as :class:`subagent_narrator.SubagentNarrationSink`): every event is
  delegated to the wrapped sink FIRST, then a non-successful result is
  routed into the LessonMemory JSONL substrate (:mod:`lesson_memory` →
  :mod:`failure_mode_memory`) — the store the generation prompt already
  reads back. One store, one taxonomy, every worker.
* :func:`record_unit_failure` — the same routing for L3 work units
  (``WorkUnitResult``), called by the scheduler where a unit's terminal
  status is applied, so swarm-synthesised and legacy units are covered by
  one seam.
* :func:`record_fanout_crash` — the routing for a graph that crashed
  outright (see :func:`parallel_dispatch.enforce_evaluate_fanout_guarded`).

Every failure also emits ONE WARNING line (``[SubagentDiagnostics]``) so a
headless soak, which only carries WARNING and above, shows the failure the
moment it happens.

Boundaries
----------
* Fail-soft everywhere: a diagnostics fault can never perturb dispatch,
  the scheduler, or the FSM. Recording is scheduled on the running loop
  (fire-and-forget, tracked so it is never garbage-collected mid-write) and
  bounded by :func:`lesson_memory.timeout_s`.
* ``JARVIS_SUBAGENT_DIAGNOSTICS_ENABLED`` (default on) is the only knob.
* Statuses that are not the model's failure (``completed``, ``cancelled``,
  ``not_implemented``) are never recorded — a wiring placeholder is not a
  lesson.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.SubagentDiagnostics")

_ENV_ENABLED = "JARVIS_SUBAGENT_DIAGNOSTICS_ENABLED"
_ENV_MAX_FILES = "JARVIS_SUBAGENT_DIAGNOSTICS_MAX_FILES"

#: Lesson phases. ``SUBAGENT_<TYPE>`` for orchestrator dispatches,
#: ``SUBAGENT_UNIT`` for L3 work units, ``FANOUT`` for a crashed graph.
PHASE_PREFIX = "SUBAGENT"
PHASE_UNIT = "SUBAGENT_UNIT"
PHASE_FANOUT = "FANOUT"

#: Terminal statuses that are NOT a failure of the worker's own doing.
_NON_FAILURE_STATUSES = frozenset({"completed", "cancelled", "not_implemented"})

#: ``SubagentResult.error_class`` (the executors' own names) → the open-set
#: lesson taxonomy shared with :mod:`lesson_memory`. Names describe the SHAPE
#: of the failure so the mitigation text stays generic.
_ERROR_CLASS_MAP: Dict[str, str] = {
    "SubagentTimeout": "subagent_timeout",
    "SubagentSemanticFirewallRejection": "cage_breach",
    "BlockedPathError": "cage_breach",
    "ScopedToolBackendViolation": "cage_breach",
    "ToolCageViolation": "cage_breach",
    "IronGateDiversityRejection": "diversity_rejected",
    "MalformedGeneralInput": "schema_hallucination",
    "MalformedReviewInput": "schema_hallucination",
    "MalformedPlanInput": "schema_hallucination",
    "InvalidPlanDag": "schema_hallucination",
}

#: ``SubagentStatus`` values that classify on their own.
_STATUS_CLASS_MAP: Dict[str, str] = {
    "budget_exhausted": "budget_exhausted",
    "diversity_rejected": "diversity_rejected",
}

#: ``WorkUnitResult.failure_class`` → lesson taxonomy. ``None`` = not a
#: lesson. ``test``/``validation`` fall through to the evidence classifier so
#: an assertion, an ambient red or a timeout keep their own class.
_UNIT_CLASS_MAP: Dict[str, Optional[str]] = {
    "infra": "subagent_infra",
    "worktree_isolation": "worktree_isolation",
    "budget": "budget_exhausted",
    "security": "cage_breach",
    "syntax": "syntax_error",
    "cancelled": None,
}


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() not in ("0", "false", "no", "off")


def _max_files() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_MAX_FILES, "16")))
    except ValueError:
        return 16


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_subagent_failure(result: Any) -> Optional[str]:
    """Lesson error class for a ``SubagentResult``; ``None`` when the result
    is not a failure worth learning from. NEVER raises."""
    try:
        status = _enum_value(getattr(result, "status", ""))
        if status in _NON_FAILURE_STATUSES:
            return None
        error_class = str(getattr(result, "error_class", "") or "").strip()
        if error_class in _ERROR_CLASS_MAP:
            return _ERROR_CLASS_MAP[error_class]
        if status in _STATUS_CLASS_MAP:
            return _STATUS_CLASS_MAP[status]
        detail = f"{error_class}: {getattr(result, 'error_detail', '') or ''}"
        if "timeout" in error_class.lower() or "timed out" in detail.lower():
            return "subagent_timeout"
        from backend.core.ouroboros.governance.lesson_memory import classify_error
        return classify_error(detail)
    except Exception:  # noqa: BLE001
        return "exception"


def classify_unit_failure(result: Any) -> Optional[str]:
    """Lesson error class for a ``WorkUnitResult``; ``None`` when the unit
    did not fail on its own account. NEVER raises."""
    try:
        status = _enum_value(getattr(result, "status", ""))
        if status != "failed":
            return None
        fc = str(getattr(result, "failure_class", "") or "").strip().lower()
        if fc in _UNIT_CLASS_MAP:
            return _UNIT_CLASS_MAP[fc]
        from backend.core.ouroboros.governance.lesson_memory import classify_error
        return classify_error(str(getattr(result, "error", "") or ""))
    except Exception:  # noqa: BLE001
        return "exception"


# ---------------------------------------------------------------------------
# Lesson composition
# ---------------------------------------------------------------------------

def subagent_lesson_kwargs(
    parent_op_id: str, result: Any, *, target_files: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    """The :func:`lesson_memory.record_lesson` arguments for a failed
    subagent result; ``None`` when there is nothing to record."""
    error_class = classify_subagent_failure(result)
    if error_class is None:
        return None
    subtype = _enum_value(getattr(result, "subagent_type", "")).upper() or "UNKNOWN"
    files = tuple(str(f) for f in (target_files or ()) if f)
    if not files:
        files = tuple(str(f) for f in (getattr(result, "files_read", ()) or ()) if f)[: _max_files()]
    raw_class = str(getattr(result, "error_class", "") or "").strip()
    detail = _text(getattr(result, "error_detail", ""), 600)
    return {
        "op_id": str(parent_op_id or ""),
        "target_files": files,
        "phase": f"{PHASE_PREFIX}_{subtype}",
        "failure_class": _enum_value(getattr(result, "status", "")) or "failed",
        "error_text": f"{raw_class}: {detail}" if raw_class else detail,
        "summary": _text(getattr(result, "goal", ""), 240),
        "error_class": error_class,
    }


def unit_lesson_kwargs(op_id: str, unit: Any, result: Any) -> Optional[Dict[str, Any]]:
    error_class = classify_unit_failure(result)
    if error_class is None:
        return None
    files = tuple(str(f) for f in (getattr(unit, "target_files", ()) or ()) if f)
    return {
        "op_id": str(op_id or ""),
        "target_files": files,
        "phase": PHASE_UNIT,
        "failure_class": str(getattr(result, "failure_class", "") or "failed"),
        "error_text": _text(getattr(result, "error", ""), 600),
        "summary": _text(getattr(unit, "goal", ""), 240),
        "error_class": error_class,
    }


# ---------------------------------------------------------------------------
# Recording (async, bounded, fail-soft)
# ---------------------------------------------------------------------------

Recorder = Callable[..., Awaitable[str]]


async def _record(kwargs: Dict[str, Any], recorder: Optional[Recorder]) -> str:
    """Route one lesson into the store. NEVER raises."""
    try:
        if recorder is None:
            from backend.core.ouroboros.governance.lesson_memory import record_lesson
            recorder = record_lesson
        outcome = await recorder(**kwargs)
        return str(outcome)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — diagnostics never perturb the caller
        logger.debug("[SubagentDiagnostics] lesson record degraded: %s", exc, exc_info=True)
        return "error"


async def record_subagent_failure(
    parent_op_id: str, result: Any, *, target_files: Sequence[str] = (),
    recorder: Optional[Recorder] = None,
) -> str:
    """Log (WARNING) and persist a failed ``SubagentResult`` as a lesson.
    Returns the store outcome, ``"skipped"`` for non-failures. NEVER raises."""
    try:
        kwargs = subagent_lesson_kwargs(parent_op_id, result, target_files=target_files)
        if kwargs is None:
            return "skipped"
        logger.warning(
            "[SubagentDiagnostics] subagent failed parent=%s sub=%s type=%s status=%s "
            "class=%s provider=%s detail=%s",
            str(parent_op_id)[:16], str(getattr(result, "subagent_id", ""))[:24],
            _enum_value(getattr(result, "subagent_type", "")), kwargs["failure_class"],
            kwargs["error_class"], str(getattr(result, "provider_used", "") or "-"),
            kwargs["error_text"][:240],
        )
        return await _record(kwargs, recorder)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SubagentDiagnostics] subagent failure routing degraded: %s", exc)
        return "error"


async def record_unit_failure(
    op_id: str, unit: Any, result: Any, *, recorder: Optional[Recorder] = None,
) -> str:
    """Log (WARNING) and persist a failed L3 ``WorkUnitResult`` as a lesson.
    Returns the store outcome, ``"skipped"`` for non-failures. NEVER raises."""
    try:
        if not enabled():
            return "disabled"
        kwargs = unit_lesson_kwargs(op_id, unit, result)
        if kwargs is None:
            return "skipped"
        logger.warning(
            "[SubagentDiagnostics] unit failed op=%s unit=%s files=%s fc=%s class=%s error=%s",
            str(op_id)[:16], str(getattr(unit, "unit_id", ""))[:24],
            ",".join(kwargs["target_files"])[:200], kwargs["failure_class"],
            kwargs["error_class"], kwargs["error_text"][:240],
        )
        return await _record(kwargs, recorder)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SubagentDiagnostics] unit failure routing degraded: %s", exc)
        return "error"


async def record_fanout_crash(
    op_id: str, exc: BaseException, *, target_files: Sequence[str] = (),
    recorder: Optional[Recorder] = None,
) -> str:
    """Persist a crashed fan-out graph as a lesson. NEVER raises."""
    try:
        if not enabled():
            return "disabled"
        kwargs = {
            "op_id": str(op_id or ""),
            "target_files": tuple(str(f) for f in (target_files or ()) if f),
            "phase": PHASE_FANOUT,
            "failure_class": "crashed",
            "error_text": _text(f"{type(exc).__name__}: {exc}", 600),
            "summary": "execution graph crashed before a terminal state",
            "error_class": "fanout_crash",
        }
        return await _record(kwargs, recorder)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("[SubagentDiagnostics] fan-out crash routing degraded: %s", e)
        return "error"


# ---------------------------------------------------------------------------
# CommSink decorator
# ---------------------------------------------------------------------------

class SubagentDiagnosticSink:
    """Wraps a ``CommSink``; delegates first, then routes failures to memory.

    Delegation comes first so the wrapped spine (ledger, observability API,
    SSE stream) can never be starved by a diagnostics fault. Recording is
    scheduled on the running loop and tracked in :attr:`pending` so a task
    is never dropped mid-write; :meth:`drain` awaits them (tests, shutdown).
    """

    def __init__(self, inner: Any = None, *, recorder: Optional[Recorder] = None) -> None:
        self._inner = inner
        self._recorder = recorder
        self.pending: Set["asyncio.Task[Any]"] = set()

    # -- CommSink protocol ---------------------------------------------------

    def emit_spawn(self, parent_op_id: str, subagent_id: str, subagent_type: Any, goal: str) -> None:
        self._delegate("emit_spawn", parent_op_id, subagent_id, subagent_type, goal)

    def emit_result(self, parent_op_id: str, subagent_id: str, result: Any) -> None:
        self._delegate("emit_result", parent_op_id, subagent_id, result)
        if not enabled():
            return
        try:
            if classify_subagent_failure(result) is None:
                return
            self._schedule(record_subagent_failure(parent_op_id, result, recorder=self._recorder))
        except Exception:  # noqa: BLE001 — never reaches the orchestrator
            logger.debug("[SubagentDiagnostics] emit_result interception degraded", exc_info=True)

    # -- internals -----------------------------------------------------------

    def _delegate(self, name: str, *args: Any) -> None:
        fn = getattr(self._inner, name, None) if self._inner is not None else None
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — the inner sink's fault is its own
            logger.debug("[SubagentDiagnostics] inner sink %s raised", name, exc_info=True)

    def _schedule(self, coro: Awaitable[str]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                coro.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            logger.debug("[SubagentDiagnostics] no running loop — lesson not scheduled")
            return
        task = loop.create_task(coro)  # type: ignore[arg-type]
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def drain(self) -> None:
        """Await every scheduled recording (tests, orderly shutdown)."""
        if self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)


def wrap_subagent_diagnostics(inner: Any, *, recorder: Optional[Recorder] = None) -> Any:
    """Decorate *inner* when diagnostics are enabled; *inner* unchanged
    otherwise or on any fault — a missing interceptor must never cost the
    observability spine it wraps."""
    try:
        if not enabled():
            return inner
        return SubagentDiagnosticSink(inner, recorder=recorder)
    except Exception:  # noqa: BLE001
        return inner


__all__ = [
    "PHASE_FANOUT", "PHASE_PREFIX", "PHASE_UNIT",
    "SubagentDiagnosticSink", "classify_subagent_failure", "classify_unit_failure",
    "enabled", "record_fanout_crash", "record_subagent_failure", "record_unit_failure",
    "subagent_lesson_kwargs", "unit_lesson_kwargs", "wrap_subagent_diagnostics",
]
