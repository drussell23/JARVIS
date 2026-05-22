"""AST/compile provenance telemetry — Slice 11 Phase 11A.

Empirical context — bt-2026-05-22-011927 (Slice 7g acceptance soak,
post Slice 10 chromadb isolation):

    Sample analysis showed the main asyncio thread blocked in:
      task_step → gen_send_ex2 → _PyEval_EvalFrameDefault →
      builtin_compile → Py_CompileStringObject → PyAST_mod2obj →
      ast2obj_stmt → ast2obj_list → PyType_GenericAlloc →
      _PyObject_GC_Link → gc_collect_main

    Some coroutine on the main control plane is calling Python's
    ``compile()`` on source repeatedly, and each call triggers a
    full GC traversal (``dict_traverse`` × N, ``subtype_traverse``
    × N, ``func_traverse`` × N, ``visit_decref``,
    ``visit_reachable``) over a large object graph. The GC blocks
    the main thread for seconds per call; the asyncio event loop
    can't tick.

    Slice 10 isolated ChromaDB. The remaining starvation source is
    NOT chromadb — it's ``compile()`` / ``ast.parse()`` on
    something heavy enough to trigger gc_collect_main mid-call.

## Phase 11A scope (operator-bound, verbatim)

  *"Provenance first (no behavioral change). Add a small
  instrumentation wrapper for AST/compile-heavy work: caller
  label, source byte length, elapsed_ms, gc count before/after,
  whether called on the event-loop thread, optional stack digest."*

This module is the instrumentation primitive. It MUST NOT change
the behavior of any caller — calls to ``compile()`` /
``ast.parse()`` produce the same return value as before. The
wrapper only records telemetry about the call.

## Discipline

  * **Pure telemetry** — wraps a call, records metadata, returns
    the call's result unchanged. NEVER changes argv / kwargs /
    behavior.
  * **Bounded in-memory ring** — 1024 most recent records; older
    records evicted automatically.
  * **NEVER raises** — if instrumentation itself fails, the
    underlying compile/parse call still happens (or we return a
    synthetic record); the caller's correctness is preserved.
  * **Best-effort logging** — each call emits a single
    ``[CompileProvenance]`` line at INFO when above a configurable
    duration threshold (env-knobbed, default 50ms); below the
    threshold the record stays in-ring only.
  * **Loop-thread detection** — uses ``asyncio._get_running_loop``
    via the running-loop accessor. A True ``on_loop_thread`` flag
    means this call STARVES the event loop while it runs — that's
    the structural sin Slice 11B will fix.

## API

  * ``record_compile(caller, source, **kwargs)`` — wraps a
    ``compile()`` call.
  * ``record_ast_parse(caller, source, **kwargs)`` — wraps an
    ``ast.parse()`` call.
  * ``measure(caller, kind)`` — context manager for the cases
    where source isn't a string (e.g. ``ast.parse(file_obj)``).
  * ``recent_records()`` — ring snapshot for forensic inspection.
  * ``top_callers_by_total_ms()`` — aggregate by caller label.

Slice 11B refactor will introduce a canonical helper
(``ast_compile_helper`` / ``code_analysis_worker``) that runs
heavy AST/compile work off the main control plane. This module's
records pinpoint WHICH callers need to migrate to that helper.
"""

from __future__ import annotations

import ast as _ast_mod
import asyncio
import enum
import gc as _gc_mod
import logging
import os
import threading
import time
import traceback
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterator, List, Optional


logger = logging.getLogger("Ouroboros.CompileProvenance")


# ============================================================================
# Closed taxonomy — call kind
# ============================================================================


class CallKind(str, enum.Enum):
    """Closed 2-value taxonomy: what kind of heavy work is being
    instrumented."""

    COMPILE  = "compile"      # builtin compile() call
    AST_PARSE = "ast_parse"   # ast.parse() call


# ============================================================================
# Record — one provenance entry
# ============================================================================


@dataclass(frozen=True)
class CompileProvenanceRecord:
    """Frozen telemetry record for one heavy parse/compile call."""

    caller: str
    kind: CallKind
    source_bytes: int
    elapsed_ms: float
    gc_count_before: int
    gc_count_after: int
    on_loop_thread: bool
    thread_name: str
    ts_monotonic: float
    stack_digest: str = ""


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_COMPILE_PROVENANCE_ENABLED"
_LOG_THRESHOLD_MS_ENV: str = "JARVIS_COMPILE_PROVENANCE_LOG_THRESHOLD_MS"
_RING_CAP_ENV: str = "JARVIS_COMPILE_PROVENANCE_RING_CAP"
_STACK_DIGEST_ENV: str = "JARVIS_COMPILE_PROVENANCE_STACK_DIGEST_ENABLED"

_DEFAULT_LOG_THRESHOLD_MS: float = 50.0
_DEFAULT_RING_CAP: int = 1024


def provenance_enabled() -> bool:
    """Master gate. Default TRUE for Phase 11A — pure telemetry,
    no behavior change. Explicit ``"false"`` opts out (e.g. for
    bench microbench scenarios). NEVER raises."""
    try:
        return os.environ.get(_MASTER_FLAG_ENV, "").strip().lower() not in (
            "0", "false", "no", "off",
        )
    except Exception:  # noqa: BLE001 — defensive
        return True


def _resolve_log_threshold_ms() -> float:
    try:
        raw = os.environ.get(_LOG_THRESHOLD_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_LOG_THRESHOLD_MS
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_LOG_THRESHOLD_MS


def _resolve_ring_cap() -> int:
    try:
        raw = os.environ.get(_RING_CAP_ENV, "").strip()
        if not raw:
            return _DEFAULT_RING_CAP
        return max(16, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RING_CAP


def _stack_digest_enabled() -> bool:
    return os.environ.get(_STACK_DIGEST_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ============================================================================
# Module-singleton ring + lock
# ============================================================================


_ring: Deque[CompileProvenanceRecord] = deque(maxlen=_resolve_ring_cap())
_ring_lock: threading.Lock = threading.Lock()


def _record(rec: CompileProvenanceRecord) -> None:
    """Append to the ring + log if above threshold. NEVER raises."""
    try:
        with _ring_lock:
            _ring.append(rec)
    except Exception:  # noqa: BLE001
        return
    threshold = _resolve_log_threshold_ms()
    if rec.elapsed_ms >= threshold:
        logger.info(
            "[CompileProvenance] caller=%s kind=%s "
            "src_bytes=%d elapsed_ms=%.1f gc_delta=%d "
            "on_loop=%s thread=%s",
            rec.caller, rec.kind.value,
            rec.source_bytes, rec.elapsed_ms,
            rec.gc_count_after - rec.gc_count_before,
            rec.on_loop_thread, rec.thread_name,
        )
        if rec.stack_digest:
            logger.info(
                "[CompileProvenance]   stack: %s", rec.stack_digest,
            )


def _on_loop_thread() -> bool:
    """True iff the current thread is the asyncio event loop's
    thread."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _capture_stack_digest(max_frames: int = 8) -> str:
    """Compact stack digest — file:line + funcname for the top N
    frames, joined with `→`. Skipped unless env knob enables (cost)."""
    if not _stack_digest_enabled():
        return ""
    try:
        frames = traceback.extract_stack(limit=max_frames + 2)
        # Drop the top 2 frames (this helper + the wrapper that called it)
        frames = frames[:-2]
        digest_parts = []
        for f in frames[-max_frames:]:
            fname = f.filename.rsplit("/", 1)[-1]
            digest_parts.append(f"{fname}:{f.lineno}:{f.name}")
        return " → ".join(digest_parts)
    except Exception:  # noqa: BLE001
        return ""


# ============================================================================
# Public surface — measure() context manager
# ============================================================================


@contextmanager
def measure(caller: str, kind: CallKind, source_bytes: int = 0) -> Iterator[None]:
    """Context manager — wrap a heavy compile/parse call.

    Usage:

        with measure("self_evolution.scan_module", CallKind.AST_PARSE,
                     source_bytes=len(src)):
            tree = ast.parse(src)

    The wrapper records elapsed time, GC count delta, and whether
    the call was on the event-loop thread. NEVER raises into the
    caller — instrumentation failure preserves the underlying
    call's behavior.
    """
    if not provenance_enabled():
        yield
        return
    on_loop = _on_loop_thread()
    thread_name = threading.current_thread().name
    gc_before = sum(_gc_mod.get_count())
    digest = _capture_stack_digest()
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            gc_after = sum(_gc_mod.get_count())
            _record(CompileProvenanceRecord(
                caller=str(caller)[:128],
                kind=kind,
                source_bytes=int(source_bytes),
                elapsed_ms=elapsed_ms,
                gc_count_before=gc_before,
                gc_count_after=gc_after,
                on_loop_thread=on_loop,
                thread_name=thread_name,
                ts_monotonic=t0,
                stack_digest=digest,
            ))
        except Exception:  # noqa: BLE001 — never propagate
            pass


def record_compile(
    caller: str,
    source: Any,
    *,
    filename: str = "<string>",
    mode: str = "exec",
    **kwargs: Any,
) -> Any:
    """Wrapped ``compile(source, filename, mode, ...)`` with
    provenance telemetry. Returns the same value as built-in
    ``compile``."""
    src_bytes = len(source) if isinstance(source, (str, bytes)) else 0
    with measure(caller, CallKind.COMPILE, source_bytes=src_bytes):
        return compile(source, filename, mode, **kwargs)


def record_ast_parse(
    caller: str,
    source: Any,
    *,
    filename: str = "<unknown>",
    mode: str = "exec",
    **kwargs: Any,
) -> Any:
    """Wrapped ``ast.parse(source, ...)`` with provenance
    telemetry. Returns the same value as ``ast.parse``."""
    src_bytes = len(source) if isinstance(source, (str, bytes)) else 0
    with measure(caller, CallKind.AST_PARSE, source_bytes=src_bytes):
        return _ast_mod.parse(source, filename=filename, mode=mode, **kwargs)


# ============================================================================
# Introspection — for forensic inspection + slice 11B targeting
# ============================================================================


def recent_records(limit: Optional[int] = None) -> List[CompileProvenanceRecord]:
    """Snapshot of the most recent provenance records, newest last."""
    with _ring_lock:
        items = list(_ring)
    if limit is not None:
        items = items[-int(limit):]
    return items


def top_callers_by_total_ms(
    on_loop_thread_only: bool = False,
    top: int = 20,
) -> List[Dict[str, Any]]:
    """Aggregate provenance records by caller, sorted by total
    elapsed_ms descending. ``on_loop_thread_only`` filters to
    calls made on the asyncio event loop's thread (the actual
    Slice 11B targets — these starve the control plane)."""
    agg_total: Dict[str, float] = defaultdict(float)
    agg_count: Dict[str, int] = defaultdict(int)
    agg_max: Dict[str, float] = defaultdict(float)
    agg_kind: Dict[str, str] = {}
    agg_on_loop: Dict[str, int] = defaultdict(int)
    for r in recent_records():
        if on_loop_thread_only and not r.on_loop_thread:
            continue
        agg_total[r.caller] += r.elapsed_ms
        agg_count[r.caller] += 1
        if r.elapsed_ms > agg_max[r.caller]:
            agg_max[r.caller] = r.elapsed_ms
        agg_kind[r.caller] = r.kind.value
        if r.on_loop_thread:
            agg_on_loop[r.caller] += 1
    out = [
        {
            "caller": caller,
            "kind": agg_kind.get(caller, ""),
            "total_ms": round(agg_total[caller], 1),
            "count": agg_count[caller],
            "max_ms": round(agg_max[caller], 1),
            "on_loop_count": agg_on_loop[caller],
        }
        for caller in agg_total
    ]
    out.sort(key=lambda d: d["total_ms"], reverse=True)
    return out[:top]


def reset_ring() -> None:
    """For tests — drop all records."""
    with _ring_lock:
        _ring.clear()


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "CallKind",
    "CompileProvenanceRecord",
    "measure",
    "record_compile",
    "record_ast_parse",
    "provenance_enabled",
    "recent_records",
    "top_callers_by_total_ms",
    "reset_ring",
    "install_global_probe",
    "uninstall_global_probe",
]


# ============================================================================
# Global probe — monkey-patch builtins.compile + ast.parse for blanket
# provenance coverage. Phase 11A diagnostic only.
# ============================================================================
#
# The explicit ``measure()`` wrappers in suspect callers are the
# preferred instrumentation surface. But the empirical bt-2026-05-22
# wedge's compile() caller is UNKNOWN — we don't know which file
# in the live runtime is firing it. The global probe is a wide net:
# wraps every call to builtins.compile() + ast.parse() in the
# process, captures the caller's stack frame, records to the ring,
# and falls through to the original primitive. Pure pass-through.
#
# Master flag JARVIS_COMPILE_PROVENANCE_GLOBAL_PROBE_ENABLED gates
# this — default-FALSE because monkey-patching builtins is invasive
# even for telemetry. Operator-bound enable for the Slice 11A
# diagnostic soak.


_GLOBAL_PROBE_ENV: str = "JARVIS_COMPILE_PROVENANCE_GLOBAL_PROBE_ENABLED"

_original_compile: Any = None
_original_ast_parse: Any = None
_probe_installed: bool = False
_probe_lock = threading.Lock()


def _global_probe_enabled() -> bool:
    return os.environ.get(_GLOBAL_PROBE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _probe_caller_label() -> str:
    """Walk the stack to find the FIRST frame outside ast_compile_telemetry
    that called compile/ast.parse — that's the actual user-code caller."""
    try:
        frame = traceback.extract_stack(limit=8)
        # Skip the top 2 frames (this helper + the probe wrapper).
        for f in reversed(frame[:-2]):
            fname = f.filename.rsplit("/", 1)[-1]
            if fname.startswith("ast_compile_telemetry"):
                continue
            return f"{fname}:{f.lineno}:{f.name}"
    except Exception:  # noqa: BLE001
        pass
    return "<unknown>"


def _probed_compile(source: Any, filename: str = "<string>", mode: str = "exec",
                    *args: Any, **kwargs: Any) -> Any:
    """Drop-in replacement for builtins.compile() that records
    provenance + falls through to the original."""
    caller = _probe_caller_label()
    src_bytes = len(source) if isinstance(source, (str, bytes)) else 0
    with measure(caller, CallKind.COMPILE, source_bytes=src_bytes):
        return _original_compile(source, filename, mode, *args, **kwargs)


def _probed_ast_parse(source: Any, *args: Any, **kwargs: Any) -> Any:
    """Drop-in replacement for ast.parse() that records provenance +
    falls through to the original."""
    caller = _probe_caller_label()
    src_bytes = len(source) if isinstance(source, (str, bytes)) else 0
    with measure(caller, CallKind.AST_PARSE, source_bytes=src_bytes):
        return _original_ast_parse(source, *args, **kwargs)


def install_global_probe() -> bool:
    """Monkey-patch builtins.compile + ast.parse for blanket
    provenance. Returns True iff the probe was installed (False on
    flag-off / already-installed / install failure). NEVER raises."""
    global _original_compile, _original_ast_parse, _probe_installed
    if not _global_probe_enabled():
        return False
    with _probe_lock:
        if _probe_installed:
            return False
        try:
            import builtins
            _original_compile = builtins.compile
            _original_ast_parse = _ast_mod.parse
            builtins.compile = _probed_compile  # type: ignore[assignment]
            _ast_mod.parse = _probed_ast_parse  # type: ignore[assignment]
            _probe_installed = True
            logger.info(
                "[CompileProvenance] GLOBAL PROBE installed — "
                "every builtins.compile + ast.parse call records "
                "provenance until uninstall"
            )
            return True
        except Exception:  # noqa: BLE001 — never raise
            logger.debug(
                "[CompileProvenance] global probe install failed",
                exc_info=True,
            )
            return False


def uninstall_global_probe() -> None:
    """Restore the original builtins.compile + ast.parse. NEVER raises."""
    global _original_compile, _original_ast_parse, _probe_installed
    with _probe_lock:
        if not _probe_installed:
            return
        try:
            import builtins
            if _original_compile is not None:
                builtins.compile = _original_compile
            if _original_ast_parse is not None:
                _ast_mod.parse = _original_ast_parse
            _probe_installed = False
            logger.info(
                "[CompileProvenance] global probe uninstalled"
            )
        except Exception:  # noqa: BLE001
            pass


# Auto-install at module-load when the env knob is set. Boot-time
# install means EVERY subsequent compile/ast.parse in the process
# is captured — including ones the explicit measure() wrappers miss.
if _global_probe_enabled():
    install_global_probe()
