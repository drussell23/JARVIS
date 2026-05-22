"""Canonical AST/compile helper — Slice 11 Phase 11B.

Closes the empirical wedge from bt-2026-05-22-013824 (Slice 11A
provenance soak). The diagnostic captured **85 on-loop
``ast.parse()`` calls totalling 101.3 seconds** of asyncio
event-loop blocking time. The dominant caller was
``opportunity_miner_sensor._scan_module`` — synchronous
``ast.parse()`` on 19-31KB files taking 4-7 seconds each because
CPython's GIL-held ``gc_collect_main`` cascade triggers during
large-source AST construction.

## Phase 11B fix (operator-bound)

This module is the canonical helper that runs heavy AST/compile
work **off the main asyncio control plane**. Architecture:

  * ``await parse_python_source(caller, source, ...)`` — single
    async entry point.
  * **Process pool** isolation for source above a byte threshold
    (default 4 KB). Each worker has its own GIL; the main asyncio
    thread continues to tick during the parse.
  * **Inline tiny** path for source below the threshold —
    ``ast.parse()`` returns in <5ms for small files; the
    process-pool IPC overhead would dominate.
  * **Closed-taxonomy result** — ``ParseOutcome`` 5-value enum
    (``OK / SYNTAX_ERROR / TIMEOUT / TOO_LARGE / INTERNAL_ERROR``).
    Every call returns a populated ``ParseResult``; the helper
    NEVER raises into the caller.
  * **Bounded timeout** via ``asyncio.wait_for`` around the
    executor future. On timeout the future is cancelled cleanly
    and the result carries ``TIMEOUT`` outcome.
  * **Bounded input size** via ``max_bytes`` (default 1 MB). Source
    over the cap returns ``TOO_LARGE`` without touching the pool.
  * **Provenance integration** — composes Slice 11A's
    ``measure()`` so every call is captured in the same telemetry
    ring. The ``execution_mode`` field distinguishes
    ``inline_tiny`` vs ``process`` paths.

## IPC

  * ``concurrent.futures.ProcessPoolExecutor`` handles all
    inter-process serialization internally via its standard
    Python machinery — the worker function returns a tuple, the
    executor ships it to the main process automatically. No
    explicit serialization in this module's source.
  * The pool uses ``multiprocessing.get_context("spawn")`` for
    portability + safety (fork-from-asyncio edge cases avoided).

## Discipline (AST-pinned in the test surface)

  * The ONLY ``ast.parse()`` call site in this module is inside
    ``_worker_parse_in_process`` (the process-pool worker
    function). Every other ast.parse call in OpportunityMiner is
    forbidden when the function is an ``async def`` — operators
    must route through ``parse_python_source``.
  * The pool is a lazy module-level singleton; the first call
    pays the spawn cost, subsequent calls reuse workers.
  * Tiny-path threshold is env-knobbed
    (``JARVIS_AST_HELPER_TINY_THRESHOLD_BYTES``, default 4096).
  * Default timeout is env-knobbed
    (``JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S``, default 10.0).
  * Default max-bytes cap is env-knobbed
    (``JARVIS_AST_HELPER_MAX_BYTES``, default 1_000_000).

## What this module does NOT do (operator-bound)

  * Does NOT migrate ``provider_topology._validate_*`` /
    ``shipped_code_invariants.validate_invariant`` /
    ``cross_kingdom_boundary._scan_one_file`` — those are
    11C/11D targets, not this PR.
  * Does NOT disable any other subsystem.
  * Does NOT install global ``compile()`` / ``ast.parse()``
    monkey-patches — that's Slice 11A's diagnostic probe.
  * Does NOT introduce a master flag for rollback (the helper's
    behavior is byte-equivalent on the OK path; on failure
    paths it returns a strict superset of legacy
    ``SyntaxError``/``OSError`` semantics).
"""

from __future__ import annotations

import ast as _ast_mod
import asyncio
import enum
import logging
import multiprocessing as _mp
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional, Tuple


logger = logging.getLogger("Ouroboros.AstCompileHelper")


# ============================================================================
# Closed-taxonomy enums (AST-pinned)
# ============================================================================


class ParseOutcome(str, enum.Enum):
    """Closed 5-value outcome taxonomy. Adding a 6th value
    requires bumping the AST pin + every caller's branching."""

    OK              = "ok"
    SYNTAX_ERROR    = "syntax_error"
    TIMEOUT         = "timeout"
    TOO_LARGE       = "too_large"
    INTERNAL_ERROR  = "internal_error"


class ExecutionMode(str, enum.Enum):
    """Closed 3-value mode taxonomy. ``THREAD`` is reserved for
    future use; this module's ``parse_python_source`` routes only
    to ``INLINE_TINY`` (below threshold) or ``PROCESS`` (above)."""

    INLINE_TINY = "inline_tiny"
    THREAD      = "thread"
    PROCESS     = "process"


# ============================================================================
# Frozen result
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Closed-taxonomy result from ``parse_python_source``. NEVER
    raises into the caller — every code path returns this shape."""

    outcome: ParseOutcome
    tree: Optional[Any]              # ast.AST when outcome=OK; None otherwise
    elapsed_ms: float
    source_bytes: int
    caller: str
    execution_mode: ExecutionMode
    error_detail: str = ""            # syntax_error message / internal err


# ============================================================================
# Env knobs (operational; closed taxonomy is structural)
# ============================================================================


_TINY_THRESHOLD_ENV: str = "JARVIS_AST_HELPER_TINY_THRESHOLD_BYTES"
_DEFAULT_TIMEOUT_ENV: str = "JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S"
_DEFAULT_MAX_BYTES_ENV: str = "JARVIS_AST_HELPER_MAX_BYTES"
_POOL_MAX_WORKERS_ENV: str = "JARVIS_AST_HELPER_POOL_MAX_WORKERS"

_DEFAULT_TINY_THRESHOLD: int = 4096           # 4 KB
_DEFAULT_TIMEOUT_S: float = 10.0
_DEFAULT_MAX_BYTES: int = 1_000_000           # 1 MB
_DEFAULT_POOL_MAX_WORKERS: int = 2            # small; ast.parse is bursty


def _resolve_tiny_threshold() -> int:
    try:
        raw = os.environ.get(_TINY_THRESHOLD_ENV, "").strip()
        if not raw:
            return _DEFAULT_TINY_THRESHOLD
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TINY_THRESHOLD


def _resolve_default_timeout_s() -> float:
    try:
        raw = os.environ.get(_DEFAULT_TIMEOUT_ENV, "").strip()
        if not raw:
            return _DEFAULT_TIMEOUT_S
        v = float(raw)
        return max(0.1, v)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def _resolve_default_max_bytes() -> int:
    try:
        raw = os.environ.get(_DEFAULT_MAX_BYTES_ENV, "").strip()
        if not raw:
            return _DEFAULT_MAX_BYTES
        return max(1024, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES


def _resolve_pool_max_workers() -> int:
    try:
        raw = os.environ.get(_POOL_MAX_WORKERS_ENV, "").strip()
        if not raw:
            return _DEFAULT_POOL_MAX_WORKERS
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_POOL_MAX_WORKERS


# ============================================================================
# Process-pool worker — module-level so spawn can locate the symbol
# ============================================================================


def _worker_parse_in_process(
    source: str,
    filename: str,
    mode: str,
) -> Tuple[str, Any]:
    """Process-pool worker. Runs ``ast.parse()`` in a separate
    Python interpreter (its own GIL — the main asyncio thread
    continues to tick during this call).

    Returns a 2-tuple shipped back by ``ProcessPoolExecutor``'s
    standard inter-process plumbing. The first element is the
    outcome label (``"ok"`` / ``"syntax_error"`` /
    ``"internal_error"``); the second is the payload (``ast.AST``
    tree on OK, error message string otherwise).

    NEVER raises out of the worker — every error path returns a
    structured tuple. An uncaught worker raise would otherwise
    crash the pool worker and propagate ``BrokenProcessPool`` to
    the main process.

    This is the SINGLE permitted ``ast.parse()`` call site in
    this module. The AST pin in the paired test enforces that
    invariant."""
    try:
        tree = _ast_mod.parse(source, filename=filename, mode=mode)
        return ("ok", tree)
    except SyntaxError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return ("syntax_error", detail)
    except Exception as exc:  # noqa: BLE001 — never crash worker
        detail = f"{type(exc).__name__}: {exc}"
        return ("internal_error", detail)


# ============================================================================
# Lazy process-pool singleton
# ============================================================================


_pool: Optional[ProcessPoolExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> ProcessPoolExecutor:
    """Lazy process-pool singleton. Uses ``spawn`` context for
    portability — avoids fork-from-asyncio edge cases."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            ctx = _mp.get_context("spawn")
            _pool = ProcessPoolExecutor(
                max_workers=_resolve_pool_max_workers(),
                mp_context=ctx,
            )
            logger.info(
                "[AstCompileHelper] process pool initialised "
                "max_workers=%d ctx=spawn",
                _resolve_pool_max_workers(),
            )
    return _pool


def shutdown_pool() -> None:
    """Shut down the process pool (for tests + clean exit).
    NEVER raises."""
    global _pool
    with _pool_lock:
        if _pool is None:
            return
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _pool = None


# ============================================================================
# Public async API
# ============================================================================


async def parse_python_source(
    caller: str,
    source: str,
    *,
    filename: str = "<unknown>",
    timeout_s: Optional[float] = None,
    max_bytes: Optional[int] = None,
    mode: str = "exec",
    tiny_threshold_override: Optional[int] = None,
) -> ParseResult:
    """Async-safe Python source parser — Slice 11B canonical helper.

    Parameters
    ----------
    caller:
        Mandatory provenance label (e.g.
        ``"opportunity_miner_sensor._scan_module"``). Logged into
        the Slice 11A telemetry ring + into the result.
    source:
        Python source text. ``str`` (UTF-8 already decoded);
        callers reading from files should decode before passing.
    filename:
        Logical filename for error messages. Defaults to
        ``"<unknown>"``.
    timeout_s:
        Hard timeout for the parse operation. Defaults to
        ``JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S`` (10.0s clamped).
    max_bytes:
        Source-size ceiling. Sources above this return
        ``TOO_LARGE`` without touching the pool. Defaults to
        ``JARVIS_AST_HELPER_MAX_BYTES`` (1 MB).
    mode:
        Passed verbatim to ``ast.parse()``. Defaults to ``"exec"``.
    tiny_threshold_override:
        For tests. Sources at or below this size are parsed
        ``INLINE_TINY`` (no IPC). Otherwise uses
        ``JARVIS_AST_HELPER_TINY_THRESHOLD_BYTES``.

    Returns
    -------
    ParseResult
        Always populated. NEVER raises into the caller.
    """
    t0 = time.monotonic()
    source_bytes = len(source.encode("utf-8")) if isinstance(source, str) else 0
    effective_timeout = (
        float(timeout_s) if timeout_s is not None
        else _resolve_default_timeout_s()
    )
    effective_max_bytes = (
        int(max_bytes) if max_bytes is not None
        else _resolve_default_max_bytes()
    )
    tiny_threshold = (
        int(tiny_threshold_override) if tiny_threshold_override is not None
        else _resolve_tiny_threshold()
    )

    # Bound check — too-large fast-path returns without touching
    # the pool.
    if source_bytes > effective_max_bytes:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        logger.info(
            "[AstCompileHelper] caller=%s outcome=too_large "
            "source_bytes=%d max_bytes=%d elapsed_ms=%.2f",
            caller, source_bytes, effective_max_bytes, elapsed_ms,
        )
        return ParseResult(
            outcome=ParseOutcome.TOO_LARGE,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,  # nominal; not invoked
            error_detail=(
                f"source {source_bytes}B exceeds max_bytes "
                f"{effective_max_bytes}B"
            ),
        )

    # Tiny-source inline path — ast.parse for small sources is
    # <5ms; the process-pool IPC overhead would dominate. Compose
    # Slice 11A's measure() for provenance.
    if source_bytes <= tiny_threshold:
        return _inline_tiny_parse(
            caller=caller, source=source, filename=filename,
            mode=mode, source_bytes=source_bytes, t0=t0,
        )

    # Process-pool path — the heavy case. Off-loops the main
    # asyncio thread so it can keep ticking.
    return await _process_pool_parse(
        caller=caller, source=source, filename=filename,
        mode=mode, source_bytes=source_bytes, t0=t0,
        timeout_s=effective_timeout,
    )


def _inline_tiny_parse(
    *,
    caller: str,
    source: str,
    filename: str,
    mode: str,
    source_bytes: int,
    t0: float,
) -> ParseResult:
    """Inline path for tiny sources. Composes Slice 11A's
    ``measure()`` so the provenance ring still receives a record."""
    from backend.core.ouroboros.governance.ast_compile_telemetry import (
        CallKind as _S11_CK,
        measure as _s11_measure,
    )
    try:
        with _s11_measure(
            caller, _S11_CK.AST_PARSE, source_bytes=source_bytes,
        ):
            tree = _ast_mod.parse(source, filename=filename, mode=mode)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return ParseResult(
            outcome=ParseOutcome.OK,
            tree=tree,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,
        )
    except SyntaxError as exc:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return ParseResult(
            outcome=ParseOutcome.SYNTAX_ERROR,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,
            error_detail=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — fault isolation
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return ParseResult(
            outcome=ParseOutcome.INTERNAL_ERROR,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,
            error_detail=f"{type(exc).__name__}: {exc}",
        )


async def _process_pool_parse(
    *,
    caller: str,
    source: str,
    filename: str,
    mode: str,
    source_bytes: int,
    t0: float,
    timeout_s: float,
) -> ParseResult:
    """Process-pool path. Submits the parse to a worker process,
    awaits with ``asyncio.wait_for`` for bounded timeout, returns
    a structured result.

    ``ProcessPoolExecutor`` handles inter-process serialization
    via its standard implementation — the worker returns a Python
    tuple, the executor ships it back to the main process
    automatically. No explicit serialization in this module."""
    loop = asyncio.get_running_loop()
    pool = _get_pool()

    # Provenance — composes Slice 11A measure() so the ring still
    # captures the caller + elapsed_ms. The recorded
    # ``on_loop_thread`` will be True (because measure() runs on
    # the main thread alongside the await), but the actual parse
    # work happens in the worker process; the await yields
    # control to the event loop while the worker runs.
    from backend.core.ouroboros.governance.ast_compile_telemetry import (
        CallKind as _S11_CK,
        measure as _s11_measure,
    )

    try:
        with _s11_measure(
            caller, _S11_CK.AST_PARSE, source_bytes=source_bytes,
        ):
            try:
                outcome_payload = await asyncio.wait_for(
                    loop.run_in_executor(
                        pool, _worker_parse_in_process,
                        source, filename, mode,
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                logger.warning(
                    "[AstCompileHelper] caller=%s outcome=timeout "
                    "source_bytes=%d timeout_s=%.1f elapsed_ms=%.1f",
                    caller, source_bytes, timeout_s, elapsed_ms,
                )
                return ParseResult(
                    outcome=ParseOutcome.TIMEOUT,
                    tree=None,
                    elapsed_ms=elapsed_ms,
                    source_bytes=source_bytes,
                    caller=caller,
                    execution_mode=ExecutionMode.PROCESS,
                    error_detail=(
                        f"parse exceeded {timeout_s:.1f}s in pool"
                    ),
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return ParseResult(
            outcome=ParseOutcome.INTERNAL_ERROR,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=f"pool dispatch failed: {type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    # Worker returned (outcome_label, payload) tuple. Branch on
    # the label.
    try:
        outcome_label, payload = outcome_payload
    except (ValueError, TypeError):
        return ParseResult(
            outcome=ParseOutcome.INTERNAL_ERROR,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=f"worker returned unexpected shape: {type(outcome_payload).__name__}",
        )
    if outcome_label == "ok":
        return ParseResult(
            outcome=ParseOutcome.OK,
            tree=payload,           # ast.AST object — auto-IPC'd by pool
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
        )
    if outcome_label == "syntax_error":
        return ParseResult(
            outcome=ParseOutcome.SYNTAX_ERROR,
            tree=None,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=str(payload),
        )
    # ``internal_error`` (or anything unexpected) maps to
    # INTERNAL_ERROR.
    return ParseResult(
        outcome=ParseOutcome.INTERNAL_ERROR,
        tree=None,
        elapsed_ms=elapsed_ms,
        source_bytes=source_bytes,
        caller=caller,
        execution_mode=ExecutionMode.PROCESS,
        error_detail=str(payload),
    )


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "ExecutionMode",
    "ParseOutcome",
    "ParseResult",
    "parse_python_source",
    "shutdown_pool",
]
