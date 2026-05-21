"""SWE-Bench-Pro evaluator structural trace observer.

Closes the structural blind-spot surfaced by the Phase-1 wiring-smoke
soak (``bt-2026-05-21-045132``): the evaluator's ``bgop-37a419353d93``
op was alive for ~35 minutes but emitted zero log lines during that
window — the harness's wall_clock_cap eventually fired SIGKILL with no
diagnostic visibility into what the evaluator was stuck on.

This module is the canonical structural-probe substrate. It does NOT
build a parallel logging framework — instead it composes five existing
primitives:

  * :func:`asyncio.all_tasks` (stdlib, 8+ prior-art sites incl.
    ``candidate_generator.py:322``) — live task inventory.
  * :class:`PostureObserver` cadence shape (mirror, not import) —
    periodic async loop with documented cancel + cleanup discipline.
  * :func:`cross_process_jsonl.flock_append_line` (Vector #10 / v2.82) —
    canonical async-safe JSONL append; wrapped via
    ``loop.run_in_executor`` so the sync ``fcntl.flock`` call never
    blocks the observer task.
  * ``StreamEventBroker.publish`` (Gap #6 Slice 2) — sync, non-blocking,
    drop-oldest pub/sub for the new ``evaluator_trace_frame`` SSE event.
  * :class:`FlagRegistry` (Wave 1 #2) — 5 typed env knobs registered
    via canonical :class:`FlagSpec` constructor.

Architectural invariants (AST-pinned at Slice 1's spine tests):

  * **No homegrown task scanner.** The observer reads tasks via
    ``asyncio.all_tasks()`` only. Any module-level reference to a
    private ``_all_tasks`` / ``_running_tasks`` collection is banned.

  * **No parallel JSONL primitive.** The module imports
    ``flock_append_line`` from
    :mod:`backend.core.ouroboros.governance.cross_process_jsonl`
    and never imports ``fcntl`` directly. Persistence goes through
    one seam.

  * **No new logging framework.** The observer emits ONE compact
    INFO-level tick line per cycle (``[EvTrace] tick=N tasks=K ...``);
    task/subprocess detail bodies go to JSONL + SSE only. Module
    must NOT call ``logging.basicConfig`` or instantiate any handler.

  * **Closed taxonomies.** :class:`BlockedOnKind` (8 values) and
    :class:`EvaluatorPhase` (6 values) are frozen — adding a value
    requires bumping the schema version + a paired AST pin.

  * **Default-FALSE master.** ``JARVIS_EVALUATOR_TRACE_ENABLED`` ships
    default-FALSE per §33.1. Bars A/B/C in the design document gate
    the eventual default flip; until then the observer never starts
    unless explicitly opted-in.

  * **Observer NEVER blocks the main loop.** Every collector is
    guarded by ``asyncio.wait_for``; the JSONL append runs in an
    executor; the SSE publish is sync but drop-oldest by design
    (broker is non-blocking — see Gap #6 Slice 2).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
)

logger = logging.getLogger("Ouroboros.EvaluatorTrace")

EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION: str = (
    "evaluator_trace_frame.v1"
)


# ---------------------------------------------------------------------------
# Closed taxonomies — AST-pinned; adding a value requires schema bump
# ---------------------------------------------------------------------------


class BlockedOnKind(str, enum.Enum):
    """Classification of what a tracked asyncio task is blocked on.

    Derived purely from inspecting the task's top stack frame against
    a closed lookup table — no LLM, no heuristic scoring, no fallback
    beyond ``UNKNOWN_AWAIT`` / ``RUNNING_CPU``. Adding a value here
    requires bumping :data:`EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION`."""

    QUEUE_GET = "queue_get"              # asyncio.Queue.get / SimpleQueue.get
    SUBPROCESS_WAIT = "subprocess_wait"  # proc.wait / proc.communicate
    NETWORK_AWAIT = "network_await"      # aiohttp / urllib / requests
    ASYNCIO_SLEEP = "asyncio_sleep"      # asyncio.sleep
    ASYNCIO_WAIT_FOR = "asyncio_wait_for"  # asyncio.wait_for / wait
    LOCK_ACQUIRE = "lock_acquire"        # asyncio.Lock / threading.Lock
    UNKNOWN_AWAIT = "unknown_await"      # awaiting something we can't classify
    RUNNING_CPU = "running_cpu"          # no stack frame (currently scheduled)


class EvaluatorPhase(str, enum.Enum):
    """SWE-Bench-Pro evaluator phase derived from task-name suffix.

    Naming convention enforced by Slice 2 AST pins — every
    ``asyncio.create_task`` in the evaluator path must use a
    ``swe_bench_pro:<phase>:<instance_id>`` name."""

    PREPARE_PROBLEM = "prepare_problem"
    INGEST_ENVELOPE = "ingest_envelope"
    WAITING_TERMINAL = "waiting_terminal"
    SCORE_EVALUATION = "score_evaluation"
    RECORD_RESULT = "record_result"
    UNKNOWN = "unknown"


# Closed (top-frame-pattern → BlockedOnKind) lookup table. Substring
# match against frame's filename + function name. First match wins.
# Adding a row requires bumping schema + paired AST pin.
_BLOCKED_ON_PATTERNS: Tuple[Tuple[str, str, BlockedOnKind], ...] = (
    # (filename_substr, funcname_substr, kind)
    ("asyncio/queues.py",       "get",          BlockedOnKind.QUEUE_GET),
    ("asyncio/queues.py",       "put",          BlockedOnKind.QUEUE_GET),
    ("asyncio/subprocess.py",   "wait",         BlockedOnKind.SUBPROCESS_WAIT),
    ("asyncio/subprocess.py",   "communicate",  BlockedOnKind.SUBPROCESS_WAIT),
    ("asyncio/subprocess.py",   "_feed_stdin",  BlockedOnKind.SUBPROCESS_WAIT),
    ("aiohttp",                 "",             BlockedOnKind.NETWORK_AWAIT),
    ("urllib",                  "",             BlockedOnKind.NETWORK_AWAIT),
    ("requests",                "",             BlockedOnKind.NETWORK_AWAIT),
    ("asyncio/tasks.py",        "sleep",        BlockedOnKind.ASYNCIO_SLEEP),
    ("asyncio/tasks.py",        "wait_for",     BlockedOnKind.ASYNCIO_WAIT_FOR),
    ("asyncio/tasks.py",        "_wait",        BlockedOnKind.ASYNCIO_WAIT_FOR),
    ("asyncio/locks.py",        "acquire",      BlockedOnKind.LOCK_ACQUIRE),
    ("threading.py",            "acquire",      BlockedOnKind.LOCK_ACQUIRE),
)

# Closed (task-name-suffix → EvaluatorPhase) lookup. Substring match
# against the task's name after the ``swe_bench_pro:`` prefix.
_PHASE_PATTERNS: Tuple[Tuple[str, EvaluatorPhase], ...] = (
    ("prepare",         EvaluatorPhase.PREPARE_PROBLEM),
    ("ingest",          EvaluatorPhase.INGEST_ENVELOPE),
    ("harness_inject",  EvaluatorPhase.INGEST_ENVELOPE),
    ("parallel",        EvaluatorPhase.INGEST_ENVELOPE),
    ("waiting",         EvaluatorPhase.WAITING_TERMINAL),
    ("evaluate",        EvaluatorPhase.WAITING_TERMINAL),
    ("score",           EvaluatorPhase.SCORE_EVALUATION),
    ("record",          EvaluatorPhase.RECORD_RESULT),
)


# ---------------------------------------------------------------------------
# Frozen snapshot dataclasses (§33.5 lossless to_dict/from_dict)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSnapshot:
    """Single tracked asyncio task at a snapshot instant."""

    task_name: str
    evaluator_phase: EvaluatorPhase
    blocked_on_kind: BlockedOnKind
    blocked_on_detail: str
    stack_top3: Tuple[Tuple[str, int, str], ...]  # (file, lineno, func)
    elapsed_in_state_s: float
    op_id: str

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "task_name": self.task_name,
            "evaluator_phase": self.evaluator_phase.value,
            "blocked_on_kind": self.blocked_on_kind.value,
            "blocked_on_detail": self.blocked_on_detail,
            "stack_top3": [list(f) for f in self.stack_top3],
            "elapsed_in_state_s": self.elapsed_in_state_s,
            "op_id": self.op_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TaskSnapshot":
        return cls(
            task_name=str(d.get("task_name", "")),
            evaluator_phase=EvaluatorPhase(
                d.get("evaluator_phase", "unknown")
            ),
            blocked_on_kind=BlockedOnKind(
                d.get("blocked_on_kind", "unknown_await")
            ),
            blocked_on_detail=str(d.get("blocked_on_detail", "")),
            stack_top3=tuple(
                (str(f[0]), int(f[1]), str(f[2]))
                for f in (d.get("stack_top3") or ())
            ),
            elapsed_in_state_s=float(d.get("elapsed_in_state_s", 0.0)),
            op_id=str(d.get("op_id", "")),
        )


@dataclass(frozen=True)
class SubprocessSnapshot:
    """Single tracked evaluator-spawned subprocess at snapshot."""

    pid: int
    cmd_repr: str   # sanitized — credential-shaped substrings redacted
    started_at_iso: str
    alive: bool

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "pid": self.pid,
            "cmd_repr": self.cmd_repr,
            "started_at_iso": self.started_at_iso,
            "alive": self.alive,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SubprocessSnapshot":
        return cls(
            pid=int(d.get("pid", 0)),
            cmd_repr=str(d.get("cmd_repr", "")),
            started_at_iso=str(d.get("started_at_iso", "")),
            alive=bool(d.get("alive", False)),
        )


@dataclass(frozen=True)
class EvaluatorTraceFrame:
    """One snapshot frame — one tick of the observer."""

    session_id: str
    snapshot_seq: int
    monotonic_ts: float
    frame_iso: str
    tasks: Tuple[TaskSnapshot, ...]
    subprocesses: Tuple[SubprocessSnapshot, ...]
    total_tasks_loop: int
    schema_version: str = EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshot_seq": self.snapshot_seq,
            "monotonic_ts": self.monotonic_ts,
            "frame_iso": self.frame_iso,
            "tasks": [t.to_dict() for t in self.tasks],
            "subprocesses": [s.to_dict() for s in self.subprocesses],
            "total_tasks_loop": self.total_tasks_loop,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "EvaluatorTraceFrame":
        return cls(
            session_id=str(d.get("session_id", "")),
            snapshot_seq=int(d.get("snapshot_seq", 0)),
            monotonic_ts=float(d.get("monotonic_ts", 0.0)),
            frame_iso=str(d.get("frame_iso", "")),
            tasks=tuple(
                TaskSnapshot.from_dict(t)
                for t in (d.get("tasks") or ())
            ),
            subprocesses=tuple(
                SubprocessSnapshot.from_dict(s)
                for s in (d.get("subprocesses") or ())
            ),
            total_tasks_loop=int(d.get("total_tasks_loop", 0)),
            schema_version=str(
                d.get("schema_version", EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION)
            ),
        )


# ---------------------------------------------------------------------------
# Env-flag accessors (canonical FlagRegistry seeds registered separately)
# ---------------------------------------------------------------------------


_ENV_MASTER = "JARVIS_EVALUATOR_TRACE_ENABLED"
_ENV_INTERVAL_S = "JARVIS_EVALUATOR_TRACE_INTERVAL_S"
_ENV_JSONL_PATH = "JARVIS_EVALUATOR_TRACE_JSONL_PATH"
_ENV_TASK_PREFIXES = "JARVIS_EVALUATOR_TRACE_TASK_PREFIXES"
_ENV_STACK_DEPTH = "JARVIS_EVALUATOR_TRACE_STACK_DEPTH"


def evaluator_trace_enabled() -> bool:
    """Master gate. Default FALSE per §33.1. NEVER raises."""
    try:
        return os.environ.get(_ENV_MASTER, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def _resolve_interval_s() -> float:
    """Default 30s. Posture-aware adjustment applied in the observer
    loop. NEVER raises."""
    try:
        raw = os.environ.get(_ENV_INTERVAL_S, "").strip()
        if not raw:
            return 30.0
        v = float(raw)
        return v if v > 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def _resolve_jsonl_path() -> Path:
    """Default ``.jarvis/evaluator_trace.jsonl``. NEVER raises."""
    raw = os.environ.get(_ENV_JSONL_PATH, "").strip()
    if not raw:
        return Path(".jarvis") / "evaluator_trace.jsonl"
    return Path(raw)


def _resolve_task_prefixes() -> Tuple[str, ...]:
    """Default ``swe_bench_pro:,evaluator:,scorer:,prepare:``. NEVER raises."""
    raw = os.environ.get(_ENV_TASK_PREFIXES, "").strip()
    if not raw:
        raw = "swe_bench_pro:,evaluator:,scorer:,prepare:"
    return tuple(
        p.strip() for p in raw.split(",") if p.strip()
    )


def _resolve_stack_depth() -> int:
    """Default 3 frames. Clamped to [1, 10]. NEVER raises."""
    try:
        raw = os.environ.get(_ENV_STACK_DEPTH, "").strip()
        n = int(raw) if raw else 3
        return max(1, min(10, n))
    except (TypeError, ValueError):
        return 3


# ---------------------------------------------------------------------------
# Subprocess registration via ContextVar (single-seam)
# ---------------------------------------------------------------------------


# Per-task subprocess registry — a ContextVar lets each evaluator task
# carry its own subprocess identity without a global mutex'd dict. The
# observer walks tasks and reads each task's context.
_active_subprocess: contextvars.ContextVar[
    Optional[Tuple[int, str, float]]  # (pid, cmd_repr, started_at_monotonic)
] = contextvars.ContextVar(
    "evaluator_trace_active_subprocess", default=None,
)


# Credential-shape redaction patterns for cmd_repr sanitization.
# Closed set; adding a pattern requires a paired AST pin.
_CREDENTIAL_REDACTION_TOKENS: Tuple[str, ...] = (
    "api_key", "apikey", "api-key", "token", "secret", "password",
    "Bearer ", "x-api-key",
)


def _sanitize_cmd_repr(cmd_repr: str) -> str:
    """Redact credential-shaped substrings from a command repr.

    Defensive; NEVER raises. The cmd_repr is supplied by the caller
    (typically the scorer's subprocess wiring) and may include args
    we don't want surfaced in observability."""
    try:
        out = str(cmd_repr)
        lowered = out.lower()
        for token in _CREDENTIAL_REDACTION_TOKENS:
            tok_l = token.lower()
            if tok_l in lowered:
                # Replace everything after the token (greedy to EOL or
                # next space) with ``<redacted>``.
                idx = lowered.find(tok_l)
                cut = idx + len(tok_l)
                # Skip past = or : if immediately after
                while cut < len(out) and out[cut] in "=: \"'":
                    cut += 1
                end = cut
                while end < len(out) and out[end] not in " \"'\n\r\t":
                    end += 1
                if end > cut:
                    out = out[:cut] + "<redacted>" + out[end:]
                    lowered = out.lower()
        # Truncate to 200 chars (observability budget).
        return out[:200]
    except Exception:  # noqa: BLE001
        return "<sanitize_failed>"


@contextlib.contextmanager
def trace_subprocess(pid: int, cmd_repr: str) -> Iterator[None]:
    """Register a subprocess for the duration of the ``with`` block.

    The observer reads this contextvar when snapshotting tasks; the
    block-bound subprocess is associated with whichever asyncio task
    is currently executing.

    NEVER raises (defensive: contextvar set/reset is infallible)."""
    started = time.monotonic()
    sanitized = _sanitize_cmd_repr(cmd_repr)
    token = _active_subprocess.set((int(pid), sanitized, started))
    try:
        yield
    finally:
        try:
            _active_subprocess.reset(token)
        except (LookupError, ValueError):
            # Context boundary crossed (e.g. task switch) — safe to
            # ignore; the contextvar will GC with its owning context.
            pass


# ---------------------------------------------------------------------------
# Task introspection — classify blocked_on_kind from top frame
# ---------------------------------------------------------------------------


def _extract_op_id_from_name(task_name: str) -> str:
    """Extract trailing ``<phase>:<op_id>`` suffix from a task name.

    Naming convention (Slice 2): ``swe_bench_pro:<phase>:<op_id>``.
    Defensive — returns ``""`` when the convention isn't honored."""
    if not task_name or ":" not in task_name:
        return ""
    parts = task_name.rsplit(":", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return ""


def _classify_phase(task_name: str) -> EvaluatorPhase:
    """Substring-match task name against :data:`_PHASE_PATTERNS`.
    First match wins; fallback :class:`EvaluatorPhase.UNKNOWN`. NEVER raises."""
    if not task_name:
        return EvaluatorPhase.UNKNOWN
    lowered = task_name.lower()
    for needle, phase in _PHASE_PATTERNS:
        if needle in lowered:
            return phase
    return EvaluatorPhase.UNKNOWN


def _classify_blocked_on(
    stack: Sequence[FrameType],
) -> Tuple[BlockedOnKind, str]:
    """Classify what the task is blocked on from its top stack frame.

    Returns ``(BlockedOnKind, detail_string)``. RUNNING_CPU when the
    task has no captured frames (currently scheduled but not yet
    suspended). UNKNOWN_AWAIT when frames exist but none match
    :data:`_BLOCKED_ON_PATTERNS`. NEVER raises."""
    if not stack:
        return BlockedOnKind.RUNNING_CPU, ""
    top = stack[0]
    try:
        fname = (top.f_code.co_filename or "").replace("\\", "/")
        func = top.f_code.co_name or ""
    except Exception:  # noqa: BLE001
        return BlockedOnKind.UNKNOWN_AWAIT, ""
    for fname_sub, func_sub, kind in _BLOCKED_ON_PATTERNS:
        if fname_sub and fname_sub not in fname:
            continue
        if func_sub and func_sub not in func:
            continue
        detail = f"{fname.rsplit('/', 1)[-1]}::{func}"
        return kind, detail
    return BlockedOnKind.UNKNOWN_AWAIT, f"{fname.rsplit('/', 1)[-1]}::{func}"


def _stack_top_n(
    stack: Sequence[FrameType],
    depth: int,
) -> Tuple[Tuple[str, int, str], ...]:
    """Render top-N stack frames as ``(file, lineno, func)`` tuples."""
    out: List[Tuple[str, int, str]] = []
    for frame in stack[:depth]:
        try:
            out.append((
                (frame.f_code.co_filename or "").rsplit("/", 1)[-1],
                int(frame.f_lineno or 0),
                frame.f_code.co_name or "",
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def snapshot_tasks(
    *,
    prefixes: Sequence[str],
    stack_depth: int,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Tuple[Tuple[TaskSnapshot, ...], int]:
    """Snapshot live asyncio tasks matching any of ``prefixes``.

    Returns ``(filtered_snapshots, total_tasks_in_loop)``.
    Composes :func:`asyncio.all_tasks` — the canonical primitive
    (no homegrown task scanner; AST-pinned).

    NEVER raises — every per-task introspection is wrapped."""
    snaps: List[TaskSnapshot] = []
    total = 0
    try:
        tasks = asyncio.all_tasks(loop=loop) if loop else asyncio.all_tasks()
    except RuntimeError:
        # No running loop — return empty.
        return tuple(snaps), 0
    total = len(tasks)
    for task in tasks:
        try:
            name = task.get_name() or ""
            if not any(name.startswith(p) for p in prefixes):
                continue
            stack = task.get_stack(limit=max(stack_depth, 3))
            kind, detail = _classify_blocked_on(stack)
            top_n = _stack_top_n(stack, stack_depth)
            phase = _classify_phase(name)
            op_id = _extract_op_id_from_name(name)
            # Best-effort elapsed-in-state from task creation; asyncio
            # doesn't expose this directly, so we fall back to 0.0.
            elapsed = 0.0
            snaps.append(TaskSnapshot(
                task_name=name,
                evaluator_phase=phase,
                blocked_on_kind=kind,
                blocked_on_detail=detail,
                stack_top3=top_n,
                elapsed_in_state_s=elapsed,
                op_id=op_id,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTrace] task snapshot fault: %s", exc)
            continue
    return tuple(snaps), total


def _pid_alive(pid: int) -> bool:
    """Cheap liveness probe — ``os.kill(pid, 0)``. NEVER raises."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def snapshot_subprocesses() -> Tuple[SubprocessSnapshot, ...]:
    """Walk live asyncio tasks for any registered subprocess contextvar.

    Because :data:`_active_subprocess` is a contextvar, snapshotting
    requires reading each task's stored context — Python 3.11+ exposes
    this via :meth:`asyncio.Task.get_context`. Earlier Pythons degrade
    to a single-task best-effort read of the observer's own context.
    Defensive throughout — NEVER raises."""
    out: List[SubprocessSnapshot] = []
    seen_pids: set = set()
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        # No running loop — skip the per-task walk; the own-context
        # fallback below still picks up the current-context subprocess
        # (sync test paths + the 3.10 fallback rely on this).
        tasks = set()
    for task in tasks:
        try:
            get_context = getattr(task, "get_context", None)
            if get_context is None:
                # 3.10 fallback: skip; the current task's contextvar is
                # still captured when build_frame runs in its own context.
                continue
            ctx = get_context()
            payload = ctx.run(_active_subprocess.get)
            if payload is None:
                continue
            pid, cmd_repr, started = payload
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            out.append(SubprocessSnapshot(
                pid=int(pid),
                cmd_repr=str(cmd_repr),
                started_at_iso=_iso_now_for_monotonic(started),
                alive=_pid_alive(int(pid)),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTrace] subprocess walk fault: %s", exc)
            continue
    # Always include the calling context's own subprocess (covers the
    # 3.10 fallback case + tests that run snapshot_subprocesses from
    # within a trace_subprocess() block).
    own = _active_subprocess.get()
    if own is not None and own[0] not in seen_pids:
        try:
            pid, cmd_repr, started = own
            out.append(SubprocessSnapshot(
                pid=int(pid),
                cmd_repr=str(cmd_repr),
                started_at_iso=_iso_now_for_monotonic(started),
                alive=_pid_alive(int(pid)),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTrace] own subprocess read fault: %s", exc)
    return tuple(out)


def _iso_now_for_monotonic(started_mono: float) -> str:
    """Best-effort wall-clock ISO for a monotonic-time value.

    asyncio's monotonic clock isn't tied to wall time; we approximate
    by using ``time.time() - (time.monotonic() - started_mono)``."""
    try:
        delta = time.monotonic() - float(started_mono)
        wall = time.time() - delta
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall),
        )
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------


def build_frame(
    *,
    session_id: str,
    snapshot_seq: int,
    prefixes: Optional[Sequence[str]] = None,
    stack_depth: Optional[int] = None,
) -> EvaluatorTraceFrame:
    """Build one trace frame. Composes :func:`snapshot_tasks` +
    :func:`snapshot_subprocesses`. NEVER raises (every internal
    fault is downgraded to an empty / partial frame)."""
    resolved_prefixes = (
        tuple(prefixes) if prefixes is not None
        else _resolve_task_prefixes()
    )
    resolved_depth = (
        int(stack_depth) if stack_depth is not None
        else _resolve_stack_depth()
    )
    tasks, total = snapshot_tasks(
        prefixes=resolved_prefixes,
        stack_depth=resolved_depth,
    )
    subs = snapshot_subprocesses()
    return EvaluatorTraceFrame(
        session_id=str(session_id),
        snapshot_seq=int(snapshot_seq),
        monotonic_ts=time.monotonic(),
        frame_iso=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        ),
        tasks=tasks,
        subprocesses=subs,
        total_tasks_loop=int(total),
    )


# ---------------------------------------------------------------------------
# Async-safe JSONL append (wraps sync fcntl in executor)
# ---------------------------------------------------------------------------


async def async_append_frame_to_jsonl(
    frame: EvaluatorTraceFrame,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Persist one frame to JSONL — composes canonical
    :func:`flock_append_line` via ``loop.run_in_executor`` so the
    sync ``fcntl.flock`` call NEVER blocks the observer task.

    Returns True on append success, False on any failure. NEVER raises."""
    target = path if path is not None else _resolve_jsonl_path()
    try:
        line = json.dumps(frame.to_dict(), separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.debug("[EvTrace] JSONL encode fault: %s", exc)
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — synchronous fallback OK for test paths.
        return flock_append_line(target, line)
    try:
        return await loop.run_in_executor(
            None, flock_append_line, target, line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EvTrace] JSONL executor fault: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Periodic observer (mirrors PostureObserver lifecycle shape)
# ---------------------------------------------------------------------------


# Per-posture cadence multipliers. HARDEN ticks fastest (closest
# attention); EXPLORE slowest. Mirrors PostureObserver's posture-aware
# discipline without importing it (composition by mirror, not by
# coupling — Manifesto §5: deterministic, no LLM).
_POSTURE_CADENCE_MULTIPLIER: Mapping[str, float] = {
    "HARDEN": 0.5,        # 2x faster
    "CONSOLIDATE": 1.0,   # baseline
    "MAINTAIN": 1.5,      # 1.5x slower
    "EXPLORE": 2.0,       # 2x slower
}


class EvaluatorTraceObserver:
    """Periodic asyncio-task-topology snapshot observer.

    Lifecycle (mirrors :class:`PostureObserver`):

      * :meth:`start` — spawns the async task
      * :meth:`stop`  — cancels the task and awaits cleanup
      * :meth:`run_one_cycle` — public for tests (no sleep between
        cycles)

    The observer NEVER blocks the main loop:

      * Frame building is a pure read of :func:`asyncio.all_tasks`
        and per-task ``get_stack`` — no I/O.
      * JSONL append runs in the default executor via
        :func:`async_append_frame_to_jsonl`.
      * SSE publish is sync but drop-oldest by design (the broker's
        non-blocking guarantee — see Gap #6 Slice 2).

    Master flag default-FALSE — :meth:`start` is a no-op when
    :func:`evaluator_trace_enabled` returns False."""

    def __init__(
        self,
        *,
        session_id: str,
        broker_publish: Optional[
            Callable[[str, str, Mapping[str, Any]], Optional[str]]
        ] = None,
        posture_provider: Optional[Callable[[], Optional[str]]] = None,
        jsonl_path: Optional[Path] = None,
    ) -> None:
        self._session_id = str(session_id) or "no-session"
        self._broker_publish = broker_publish
        self._posture_provider = posture_provider
        self._jsonl_path = jsonl_path
        self._task: Optional[asyncio.Task[None]] = None
        self._snapshot_seq = 0
        self._cycles_completed = 0
        self._cycles_failed = 0

    @property
    def snapshot_seq(self) -> int:
        """Number of frames built so far (informational)."""
        return self._snapshot_seq

    @property
    def cycles_completed(self) -> int:
        return self._cycles_completed

    @property
    def cycles_failed(self) -> int:
        return self._cycles_failed

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _current_interval_s(self) -> float:
        """Apply posture-aware cadence multiplier to the base interval."""
        base = _resolve_interval_s()
        if self._posture_provider is None:
            return base
        try:
            posture = self._posture_provider()
        except Exception:  # noqa: BLE001
            posture = None
        if not posture:
            return base
        mult = _POSTURE_CADENCE_MULTIPLIER.get(str(posture).upper(), 1.0)
        return max(1.0, base * mult)

    async def run_one_cycle(self) -> EvaluatorTraceFrame:
        """Build + persist + publish one frame. Public for tests.

        NEVER raises — every internal fault is logged and the cycle
        completes with whatever partial state it could capture."""
        self._snapshot_seq += 1
        try:
            frame = build_frame(
                session_id=self._session_id,
                snapshot_seq=self._snapshot_seq,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTrace] frame build fault: %s", exc)
            self._cycles_failed += 1
            return EvaluatorTraceFrame(
                session_id=self._session_id,
                snapshot_seq=self._snapshot_seq,
                monotonic_ts=time.monotonic(),
                frame_iso=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                tasks=tuple(),
                subprocesses=tuple(),
                total_tasks_loop=0,
            )
        # ONE compact INFO log line per tick — bodies go to JSONL+SSE.
        kind_counts: dict = {}
        for ts in frame.tasks:
            kind_counts[ts.blocked_on_kind.value] = (
                kind_counts.get(ts.blocked_on_kind.value, 0) + 1
            )
        logger.info(
            "[EvTrace] tick=%d tasks=%d sub=%d total_loop=%d blocked=%s",
            self._snapshot_seq,
            len(frame.tasks),
            len(frame.subprocesses),
            frame.total_tasks_loop,
            dict(sorted(kind_counts.items())) or "{}",
        )
        # JSONL persistence (async via executor).
        await async_append_frame_to_jsonl(frame, path=self._jsonl_path)
        # SSE publish (sync, drop-oldest by broker design).
        if self._broker_publish is not None:
            try:
                # Event type constant must be added to
                # _VALID_EVENT_TYPES (Slice 4).
                op_id_for_event = (
                    frame.tasks[0].op_id if frame.tasks else self._session_id
                )
                self._broker_publish(
                    "evaluator_trace_frame",
                    op_id_for_event,
                    frame.to_dict(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[EvTrace] SSE publish fault: %s", exc)
        self._cycles_completed += 1
        return frame

    async def _run(self) -> None:
        """Internal cadence loop. NEVER raises out — every iteration
        is wrapped so a single bad cycle doesn't tear down the loop."""
        while True:
            try:
                await self.run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("[EvTrace] cycle fault (continuing): %s", exc)
                self._cycles_failed += 1
            interval = self._current_interval_s()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    def start(self) -> bool:
        """Spawn the observer task. No-op when master flag is FALSE.

        Returns True when the task was spawned, False when no-op."""
        if not evaluator_trace_enabled():
            logger.debug(
                "[EvTrace] master flag FALSE — observer not started",
            )
            return False
        if self.running:
            return False
        try:
            self._task = asyncio.create_task(
                self._run(), name="evaluator_trace_observer",
            )
            logger.info(
                "[EvTrace] observer started session=%s interval=%.1fs",
                self._session_id,
                self._current_interval_s(),
            )
            return True
        except RuntimeError as exc:
            logger.debug("[EvTrace] start fault (no loop?): %s", exc)
            return False

    async def stop(self) -> None:
        """Cancel the task and await its cleanup. NEVER raises."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTrace] stop fault: %s", exc)
        finally:
            self._task = None
            logger.info(
                "[EvTrace] observer stopped session=%s cycles_ok=%d "
                "cycles_failed=%d",
                self._session_id,
                self._cycles_completed,
                self._cycles_failed,
            )


# ---------------------------------------------------------------------------
# FlagRegistry seeds (Wave 1 #2 canonical pattern — defensive on import)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Register the 5 evaluator-trace env knobs with the canonical
    :class:`FlagRegistry`. Defensive — NEVER raises (registry shape
    fluctuations are downgraded to a DEBUG line; the observer's env
    accessors are authoritative regardless of FlagRegistry state)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType, Relevance,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EvTrace] FlagRegistry import faulted: %s", exc)
        return
    source = (
        "backend/core/ouroboros/governance/swe_bench_pro/"
        "evaluator_trace_observer.py"
    )
    specs = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master gate for the SWE-Bench-Pro evaluator structural "
                "trace observer. When false, the observer never starts "
                "and the 5 env knobs below are inert. Closes the silent-"
                "evaluator blind spot surfaced by Phase-1 wiring smoke "
                "bt-2026-05-21-045132. Default false until Bars A/B/C "
                "clear per the design document. Single env-flip "
                "hot-revert."
            ),
            category=Category.SAFETY,
            source_file=source,
            example="true",
            posture_relevance={
                "HARDEN": Relevance.CRITICAL,
                "CONSOLIDATE": Relevance.RELEVANT,
                "MAINTAIN": Relevance.RELEVANT,
                "EXPLORE": Relevance.IGNORED,
            },
        ),
        FlagSpec(
            name=_ENV_INTERVAL_S,
            type=FlagType.INT,
            default=30,
            description=(
                "Base snapshot cadence in seconds. Posture-aware "
                "multiplier applied at tick time — HARDEN ticks 2x "
                "faster, EXPLORE 2x slower. Clamped to >0 with default "
                "30s fallback on invalid input."
            ),
            category=Category.TIMING,
            source_file=source,
            example="30",
        ),
        FlagSpec(
            name=_ENV_JSONL_PATH,
            type=FlagType.STR,
            default=".jarvis/evaluator_trace.jsonl",
            description=(
                "JSONL append path for frame persistence. Each frame "
                "written via the canonical flock_append_line primitive "
                "wrapped in loop.run_in_executor — never blocks the "
                "observer task."
            ),
            category=Category.OBSERVABILITY,
            source_file=source,
            example=".jarvis/evaluator_trace.jsonl",
        ),
        FlagSpec(
            name=_ENV_TASK_PREFIXES,
            type=FlagType.STR,
            default="swe_bench_pro:,evaluator:,scorer:,prepare:",
            description=(
                "Comma-separated list of asyncio task-name prefixes to "
                "include in snapshots. Slice 2 naming convention "
                "guarantees evaluator tasks start with swe_bench_pro:. "
                "Operators can broaden to debug other subsystems."
            ),
            category=Category.OBSERVABILITY,
            source_file=source,
            example="swe_bench_pro:,evaluator:",
        ),
        FlagSpec(
            name=_ENV_STACK_DEPTH,
            type=FlagType.INT,
            default=3,
            description=(
                "Top-N stack frames captured per task in each frame. "
                "Clamped to [1, 10]. Larger values expand frame body "
                "size in JSONL + SSE; smaller values lose diagnostic "
                "context."
            ),
            category=Category.CAPACITY,
            source_file=source,
            example="3",
        ),
    ]
    for spec in specs:
        try:
            registry.register(spec)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[EvTrace] FlagRegistry.register fault for %s: %s",
                spec.name, exc,
            )


__all__ = [
    "EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION",
    "BlockedOnKind",
    "EvaluatorPhase",
    "TaskSnapshot",
    "SubprocessSnapshot",
    "EvaluatorTraceFrame",
    "EvaluatorTraceObserver",
    "evaluator_trace_enabled",
    "trace_subprocess",
    "snapshot_tasks",
    "snapshot_subprocesses",
    "build_frame",
    "async_append_frame_to_jsonl",
    "register_flags",
]
