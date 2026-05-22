"""Canonical AST/compile helper — Slice 11 Phase 11B (+ 11B-fix).

Closes the empirical wedge from bt-2026-05-22-013824 (Slice 11A
provenance soak): 85 on-loop ``ast.parse()`` calls totalling 101.3
seconds of asyncio event-loop blocking time, dominant caller
``opportunity_miner_sensor._scan_module``.

## 11B → 11B-fix (operator-bound, bt-2026-05-22-055230)

The original 11B helper routed parse off-loop but **still returned
an ``ast.AST`` across the IPC boundary**, and OpportunityMiner then
ran ``_analyze_file`` (six AST walks) on the main thread. The
acceptance soak proved the residual starvation: 31
``[ControlPlaneStarvation]`` events, max lag 37.7 s, 171.7 s of
loop-block time.

**11B-fix scope (operator-authorized):**

  1. Off-loop parse **and** heavy analysis for OpportunityMiner via
     ``analyze_python_source_for_opportunity_miner`` — the worker
     returns a small primitive payload (``OpportunityAnalysisPayload``),
     never an ``ast.AST``.
  2. Telemetry truth: the process path **no longer** wraps the await
     in ``measure(AST_PARSE)`` (that records misleading
     ``on_loop=True ast_parse`` events for IPC roundtrip time). The
     inline-tiny path still records via ``measure()`` because the
     parse genuinely happens on-loop. Process-mode telemetry is now
     emitted as structured ``[AstCompileHelper]`` log lines carrying
     ``execution_mode=process`` + ``worker_elapsed_ms`` (provenance
     stays auditable without conflating IPC time with parse time).
  3. Default ``max_workers=1`` so two CPU-burning workers can't
     starve the parent's I/O on a laptop-class control plane.
  4. ``parse_python_source`` retained for narrow callers that
     genuinely need the AST — but its docstring carries a loud
     warning: it is **not enough** for callers that immediately
     perform heavy AST walks (those are the real loop killers).

## Architecture

  * **Process pool** isolation via ``concurrent.futures.ProcessPoolExecutor``
    with ``multiprocessing.get_context("spawn")``. Each worker has its
    own GIL; the asyncio main thread keeps ticking during the parse.
  * **Inline tiny** path for sources ≤ 4 KB — ``ast.parse()`` returns
    in <5 ms and IPC overhead would dominate.
  * **Closed taxonomies**: ``ParseOutcome`` (5) + ``AnalyzeOutcome`` (5)
    + ``ExecutionMode`` (3). Adding a 6th value requires bumping the
    paired AST pins and every consumer branch.
  * **Bounded** by timeout (``asyncio.wait_for``) + max_bytes.
  * **Fail-closed**: every code path returns a populated result —
    helpers NEVER raise into the caller.

## Public API (after 11B-fix)

  * ``await parse_python_source(caller, source, ...)`` — returns
    ``ParseResult`` including ``ast.AST`` on OK. Use ONLY when the
    caller has a real need for the parsed tree AND will not perform
    a heavy walk on the main thread afterwards.
  * ``await analyze_python_source_for_opportunity_miner(caller,
    source, ...)`` — returns ``AnalysisResult`` with the 7
    ``OpportunityAnalysisPayload`` fields the sensor needs. Parse +
    all six dimension calculations happen in the worker; no
    ``ast.AST`` ever crosses the IPC boundary.

## Discipline (AST-pinned in the test surface)

  * The ONLY ``ast.parse()`` call sites in this module are
    ``_worker_parse_in_process``, ``_worker_analyze_in_process``,
    and ``_inline_tiny_parse``.
  * The ONLY ``ast.walk`` / ``ast.iter_child_nodes`` call sites are
    inside ``_worker_analyze_in_process`` — heavy AST walks happen
    only across the process boundary for the analyze helper.
  * The pool is a lazy module-level singleton; first call pays the
    spawn cost, subsequent calls reuse workers.
  * Tiny-path threshold env-knobbed (``JARVIS_AST_HELPER_TINY_THRESHOLD_BYTES``, default 4096).
  * Default timeout env-knobbed (``JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S``, default 10.0).
  * Default max-bytes cap env-knobbed (``JARVIS_AST_HELPER_MAX_BYTES``, default 1_000_000).
  * Default pool max workers env-knobbed (``JARVIS_AST_HELPER_POOL_MAX_WORKERS``, default **1**).

## What this module does NOT do

  * Does NOT migrate ``provider_topology._validate_*`` /
    ``shipped_code_invariants.validate_invariant`` /
    ``cross_kingdom_boundary._scan_one_file`` — those remain 11C/11D
    targets per operator scope.
  * Does NOT install global ``compile()`` / ``ast.parse()``
    monkey-patches — that's Slice 11A's diagnostic probe.
  * Does NOT disable any other subsystem.
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


class AnalyzeOutcome(str, enum.Enum):
    """Closed 5-value outcome taxonomy for the analyze helper
    (mirror of ParseOutcome). Adding a 6th value requires bumping
    the AST pin + every consumer branch."""

    OK              = "ok"
    SYNTAX_ERROR    = "syntax_error"
    TIMEOUT         = "timeout"
    TOO_LARGE       = "too_large"
    INTERNAL_ERROR  = "internal_error"


class ExecutionMode(str, enum.Enum):
    """Closed 3-value mode taxonomy. ``THREAD`` is reserved for
    future use; this module's helpers route only to
    ``INLINE_TINY`` (below threshold) or ``PROCESS`` (above)."""

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


@dataclass(frozen=True)
class OpportunityAnalysisPayload:
    """Small, primitive-only payload returned by the analyze
    helper's worker. Mirrors the seven fields of OpportunityMiner's
    legacy ``_FileAnalysis``. Picklable by trivial IPC — NO
    ``ast.AST`` ever crosses the process boundary.

    Field semantics match ``backend/core/ouroboros/governance/
    intake/sensors/opportunity_miner_sensor.py::_FileAnalysis``
    byte-for-byte; the worker computes the same six dimensions
    with the same helper logic (re-implemented inside the worker
    so the worker doesn't have to import the sensor module).
    """

    cyclomatic_complexity: int = 0
    max_function_length: int = 0
    cognitive_complexity: int = 0
    duplicate_block_count: int = 0
    import_fan_out: int = 0
    todo_fixme_count: int = 0
    total_lines: int = 0


@dataclass(frozen=True)
class AnalysisResult:
    """Closed-taxonomy result from
    ``analyze_python_source_for_opportunity_miner``. NEVER raises
    into the caller. ``payload`` is populated only on OK; on
    failure outcomes it carries the zero-value payload (sentinel
    that the caller should skip the file)."""

    outcome: AnalyzeOutcome
    payload: OpportunityAnalysisPayload
    elapsed_ms: float                 # total parent-await wall-clock
    worker_elapsed_ms: float          # worker-side measured time (0.0 when not in worker)
    source_bytes: int
    caller: str
    execution_mode: ExecutionMode
    error_detail: str = ""


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
# 11B-fix: one worker by default. Two CPU-burning workers can
# starve the parent's I/O on a laptop-class control plane; operators
# with multi-core headroom can raise via JARVIS_AST_HELPER_POOL_MAX_WORKERS.
_DEFAULT_POOL_MAX_WORKERS: int = 1


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

    Permitted ``ast.parse()`` call site. The AST pin in the paired
    test enforces that this + ``_worker_analyze_in_process`` +
    ``_inline_tiny_parse`` are the only sites."""
    try:
        tree = _ast_mod.parse(source, filename=filename, mode=mode)
        return ("ok", tree)
    except SyntaxError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return ("syntax_error", detail)
    except Exception as exc:  # noqa: BLE001 — never crash worker
        detail = f"{type(exc).__name__}: {exc}"
        return ("internal_error", detail)


# ---- Worker-side analysis primitives (11B-fix) ---------------------
#
# These are re-implementations of OpportunityMiner's six dimension
# helpers, lifted into the worker so the parent process never
# performs an AST walk. The semantics are byte-equivalent to
# ``opportunity_miner_sensor._cyclomatic_complexity`` etc., enforced
# by a metrics-parity unit test in the paired test surface.
#
# The functions are deliberately self-contained (no sensor-module
# import) so the spawn worker can resolve them on import of this
# module alone. ``ast.walk`` + ``ast.iter_child_nodes`` are
# AST-pinned to live only inside ``_worker_analyze_in_process``.


def _worker_cyclomatic_complexity(tree: Any) -> int:
    """Count branching nodes (if/for/while/with/except/and/or)."""
    branch_types = (
        _ast_mod.If, _ast_mod.For, _ast_mod.While, _ast_mod.With,
        _ast_mod.ExceptHandler, _ast_mod.BoolOp,
    )
    count = 1  # baseline (legacy contract)
    for node in _ast_mod.walk(tree):
        if isinstance(node, branch_types):
            count += 1
    return count


def _worker_max_function_length(tree: Any) -> int:
    """Longest function/method body in lines."""
    max_len = 0
    fn_types = (_ast_mod.FunctionDef, _ast_mod.AsyncFunctionDef)
    for node in _ast_mod.walk(tree):
        if isinstance(node, fn_types):
            end_lineno = getattr(node, "end_lineno", None)
            lineno = getattr(node, "lineno", None)
            if end_lineno and lineno:
                length = end_lineno - lineno + 1
                if length > max_len:
                    max_len = length
    return max_len


def _worker_cognitive_complexity(tree: Any) -> int:
    """Simplified cognitive complexity — branches weighted by
    nesting depth. Matches the sensor's recursive ``_walk`` body
    line for line. The inner helper is named with the ``_worker_``
    prefix so the AST cage pin (no ``ast.iter_child_nodes`` outside
    ``_worker_*``) accepts it."""
    score = 0
    nesting_types = (
        _ast_mod.If, _ast_mod.For, _ast_mod.While, _ast_mod.With,
        _ast_mod.ExceptHandler,
    )
    increment_types = (
        _ast_mod.If, _ast_mod.For, _ast_mod.While, _ast_mod.With,
        _ast_mod.ExceptHandler, _ast_mod.BoolOp,
    )

    def _worker_cognitive_walk(node: Any, depth: int) -> None:
        nonlocal score
        child_depth = depth
        if isinstance(node, nesting_types):
            child_depth = depth + 1
        if isinstance(node, increment_types):
            score += 1 + depth
        for child in _ast_mod.iter_child_nodes(node):
            _worker_cognitive_walk(child, child_depth)

    _worker_cognitive_walk(tree, 0)
    return score


def _worker_duplicate_block_count(source: str) -> int:
    """Count near-duplicate 4-line windows by md5. No AST work."""
    import hashlib as _hashlib
    from collections import defaultdict as _defaultdict

    lines = [
        ln.strip() for ln in source.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(lines) < 4:
        return 0

    window_hashes = _defaultdict(int)
    for i in range(len(lines) - 3):
        window = "\n".join(lines[i:i + 4])
        h = _hashlib.md5(window.encode(), usedforsecurity=False).hexdigest()
        window_hashes[h] += 1
    return sum(1 for cnt in window_hashes.values() if cnt > 1)


def _worker_import_fan_out(tree: Any) -> int:
    """Distinct top-level modules imported."""
    modules = set()
    for node in _ast_mod.walk(tree):
        if isinstance(node, _ast_mod.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, _ast_mod.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return len(modules)


def _worker_todo_fixme_count(source: str) -> int:
    """TODO/FIXME/HACK/XXX comment markers."""
    import re as _re
    return len(
        _re.findall(
            r"#\s*(?:TODO|FIXME|HACK|XXX)\b", source, _re.IGNORECASE,
        )
    )


def _worker_analyze_in_process(
    source: str,
    filename: str,
) -> Tuple[str, Any]:
    """Process-pool worker — parses + computes all 7 OpportunityMiner
    metrics inside the worker process. The worker returns a
    primitive-only payload (``Tuple[int, ...]``) ; **no ast.AST
    crosses the IPC boundary**.

    Outcome labels: ``"ok"`` / ``"syntax_error"`` / ``"internal_error"``.
    On OK the payload is a 8-tuple:
      (worker_elapsed_ms, cc, mfl, cog, dup, fanout, todos, total_lines).

    NEVER raises out of the worker. Permitted ``ast.parse`` + walk
    site (AST-pinned)."""
    t0 = time.monotonic()
    try:
        tree = _ast_mod.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        return ("syntax_error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ("internal_error", f"{type(exc).__name__}: {exc}")

    try:
        cc = _worker_cyclomatic_complexity(tree)
        mfl = _worker_max_function_length(tree)
        cog = _worker_cognitive_complexity(tree)
        dup = _worker_duplicate_block_count(source)
        fanout = _worker_import_fan_out(tree)
        todos = _worker_todo_fixme_count(source)
        total_lines = len(source.splitlines())
    except Exception as exc:  # noqa: BLE001
        return ("internal_error", f"analyze: {type(exc).__name__}: {exc}")

    worker_elapsed_ms = (time.monotonic() - t0) * 1000.0
    return (
        "ok",
        (worker_elapsed_ms, cc, mfl, cog, dup, fanout, todos, total_lines),
    )


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

    .. warning::

        **Not sufficient for callers that immediately perform a
        heavy AST walk.** The ``ast.AST`` returned via ``result.tree``
        crosses the IPC boundary (large pickle + GIL-held unpickle in
        the parent), and any subsequent ``ast.walk`` on the main
        thread will starve the asyncio control plane — this is exactly
        the wedge that motivated 11B-fix. If your caller is going to
        walk the tree, route through
        ``analyze_python_source_for_opportunity_miner`` (or a sibling
        purpose-built helper that returns a small primitive payload).

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
    automatically. No explicit serialization in this module.

    11B-fix: the previous version wrapped this await in
    ``measure(AST_PARSE)`` which produced misleading
    ``on_loop=True ast_parse`` provenance records — the await DOES
    run on the loop thread but does NOT do the parse. The wrap is
    gone. Process-mode telemetry is emitted at outcome boundary as
    a single ``[AstCompileHelper] execution_mode=process …``
    structured log line. The loop-block oracle is
    ``[ControlPlaneStarvation]`` from
    ``control_plane_watchdog.py``."""
    loop = asyncio.get_running_loop()
    pool = _get_pool()

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
            "execution_mode=process source_bytes=%d "
            "timeout_s=%.1f parent_await_ms=%.1f",
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
        # 11B-fix: structured process-mode log line at outcome
        # boundary; replaces the misleading measure(AST_PARSE) ring
        # entry. Logged at debug threshold to avoid flooding.
        logger.debug(
            "[AstCompileHelper] caller=%s outcome=ok "
            "execution_mode=process source_bytes=%d "
            "parent_await_ms=%.1f",
            caller, source_bytes, elapsed_ms,
        )
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
# Public API — analyze helper (11B-fix)
# ============================================================================


_ZERO_PAYLOAD = OpportunityAnalysisPayload()


async def analyze_python_source_for_opportunity_miner(
    caller: str,
    source: str,
    *,
    filename: str = "<unknown>",
    timeout_s: Optional[float] = None,
    max_bytes: Optional[int] = None,
    tiny_threshold_override: Optional[int] = None,
) -> AnalysisResult:
    """Off-loop parse + 6-dimension analysis for OpportunityMiner.

    The worker performs the parse **and** the six AST-walk-based
    dimension calculations (cyclomatic, max-fn-length, cognitive,
    duplicate-block, import-fan-out, todo-fixme) in a separate
    process. The parent receives a small primitive payload — **no
    ``ast.AST`` ever crosses the IPC boundary**.

    Parameters mirror ``parse_python_source``: ``caller`` is a
    mandatory provenance label; ``filename`` is logical (for error
    messages); ``timeout_s`` defaults to
    ``JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S``; ``max_bytes`` defaults
    to ``JARVIS_AST_HELPER_MAX_BYTES``.

    Returns ``AnalysisResult`` — outcome ∈ ``AnalyzeOutcome`` plus
    ``payload: OpportunityAnalysisPayload``. NEVER raises into the
    caller; failure outcomes carry the zero-value payload and the
    caller should skip the file (mirrors legacy ``errors += 1;
    continue`` semantics).

    Execution mode follows the same tiny-threshold logic as
    ``parse_python_source``: sources ≤ ``tiny_threshold`` are
    parsed + analyzed inline (the work is genuinely cheap and IPC
    overhead would dominate), sources above the threshold go
    through the process pool. The inline path still records via
    Slice 11A ``measure()`` because it really runs on-loop; the
    process path emits a single structured log line.
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

    # Too-large fast-path.
    if source_bytes > effective_max_bytes:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        logger.info(
            "[AstCompileHelper] caller=%s outcome=too_large kind=analyze "
            "source_bytes=%d max_bytes=%d elapsed_ms=%.2f",
            caller, source_bytes, effective_max_bytes, elapsed_ms,
        )
        return AnalysisResult(
            outcome=AnalyzeOutcome.TOO_LARGE,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,  # nominal; not invoked
            error_detail=(
                f"source {source_bytes}B exceeds max_bytes "
                f"{effective_max_bytes}B"
            ),
        )

    # Inline-tiny path. ast.parse + the six dimension calcs are
    # cheap for small sources; IPC overhead would dominate. The
    # measure() wrapper is genuine here — the work truly runs
    # on-loop.
    if source_bytes <= tiny_threshold:
        return _inline_tiny_analyze(
            caller=caller, source=source, filename=filename,
            source_bytes=source_bytes, t0=t0,
        )

    # Process-pool path — the heavy case.
    return await _process_pool_analyze(
        caller=caller, source=source, filename=filename,
        source_bytes=source_bytes, t0=t0,
        timeout_s=effective_timeout,
    )


def _inline_tiny_analyze(
    *,
    caller: str,
    source: str,
    filename: str,
    source_bytes: int,
    t0: float,
) -> AnalysisResult:
    """Inline path for tiny sources. Parses + analyzes on the
    caller's thread. Composes Slice 11A's ``measure()`` because the
    work truly runs on-loop."""
    from backend.core.ouroboros.governance.ast_compile_telemetry import (
        CallKind as _S11_CK,
        measure as _s11_measure,
    )
    try:
        with _s11_measure(
            caller, _S11_CK.AST_PARSE, source_bytes=source_bytes,
        ):
            outcome_label, payload = _worker_analyze_in_process(
                source, filename,
            )
    except Exception as exc:  # noqa: BLE001 — fault isolation
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return AnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    return _result_from_worker_payload(
        outcome_label=outcome_label,
        payload=payload,
        caller=caller,
        source_bytes=source_bytes,
        elapsed_ms=elapsed_ms,
        execution_mode=ExecutionMode.INLINE_TINY,
    )


async def _process_pool_analyze(
    *,
    caller: str,
    source: str,
    filename: str,
    source_bytes: int,
    t0: float,
    timeout_s: float,
) -> AnalysisResult:
    """Process-pool path — parse + analysis in worker, primitive
    payload only across IPC. NO ``measure(AST_PARSE)`` wrapper —
    the await runs on-loop but the work does not, and
    ``measure()`` would record misleading on-loop ast_parse
    events. Telemetry is emitted as structured log lines at
    outcome boundary instead."""
    loop = asyncio.get_running_loop()
    pool = _get_pool()

    try:
        worker_tuple = await asyncio.wait_for(
            loop.run_in_executor(
                pool, _worker_analyze_in_process, source, filename,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        logger.warning(
            "[AstCompileHelper] caller=%s outcome=timeout kind=analyze "
            "execution_mode=process source_bytes=%d timeout_s=%.1f "
            "parent_await_ms=%.1f",
            caller, source_bytes, timeout_s, elapsed_ms,
        )
        return AnalysisResult(
            outcome=AnalyzeOutcome.TIMEOUT,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=f"analyze exceeded {timeout_s:.1f}s in pool",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return AnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=f"pool dispatch failed: {type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    try:
        outcome_label, payload = worker_tuple
    except (ValueError, TypeError):
        return AnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=(
                f"worker returned unexpected shape: "
                f"{type(worker_tuple).__name__}"
            ),
        )

    result = _result_from_worker_payload(
        outcome_label=outcome_label,
        payload=payload,
        caller=caller,
        source_bytes=source_bytes,
        elapsed_ms=elapsed_ms,
        execution_mode=ExecutionMode.PROCESS,
    )

    # Process-mode telemetry: single structured log line. Threshold
    # at debug to keep the log clean unless ops are tuning.
    if result.outcome == AnalyzeOutcome.OK:
        logger.debug(
            "[AstCompileHelper] caller=%s outcome=ok kind=analyze "
            "execution_mode=process source_bytes=%d "
            "worker_elapsed_ms=%.1f parent_await_ms=%.1f",
            caller, source_bytes,
            result.worker_elapsed_ms, elapsed_ms,
        )
    return result


def _result_from_worker_payload(
    *,
    outcome_label: str,
    payload: Any,
    caller: str,
    source_bytes: int,
    elapsed_ms: float,
    execution_mode: ExecutionMode,
) -> AnalysisResult:
    """Decode the worker's (label, payload) tuple into an
    ``AnalysisResult``. Shared by inline-tiny and process paths so
    the decode logic lives in exactly one place."""
    if outcome_label == "ok":
        try:
            (worker_elapsed_ms, cc, mfl, cog, dup, fanout, todos,
             total_lines) = payload
        except (ValueError, TypeError):
            return AnalysisResult(
                outcome=AnalyzeOutcome.INTERNAL_ERROR,
                payload=_ZERO_PAYLOAD,
                elapsed_ms=elapsed_ms,
                worker_elapsed_ms=0.0,
                source_bytes=source_bytes,
                caller=caller,
                execution_mode=execution_mode,
                error_detail=(
                    f"ok payload shape unexpected: "
                    f"{type(payload).__name__}"
                ),
            )
        return AnalysisResult(
            outcome=AnalyzeOutcome.OK,
            payload=OpportunityAnalysisPayload(
                cyclomatic_complexity=int(cc),
                max_function_length=int(mfl),
                cognitive_complexity=int(cog),
                duplicate_block_count=int(dup),
                import_fan_out=int(fanout),
                todo_fixme_count=int(todos),
                total_lines=int(total_lines),
            ),
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=float(worker_elapsed_ms),
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=execution_mode,
        )
    if outcome_label == "syntax_error":
        return AnalysisResult(
            outcome=AnalyzeOutcome.SYNTAX_ERROR,
            payload=_ZERO_PAYLOAD,
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=0.0,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=execution_mode,
            error_detail=str(payload),
        )
    # ``internal_error`` or anything unexpected → INTERNAL_ERROR.
    return AnalysisResult(
        outcome=AnalyzeOutcome.INTERNAL_ERROR,
        payload=_ZERO_PAYLOAD,
        elapsed_ms=elapsed_ms,
        worker_elapsed_ms=0.0,
        source_bytes=source_bytes,
        caller=caller,
        execution_mode=execution_mode,
        error_detail=str(payload),
    )


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "AnalysisResult",
    "AnalyzeOutcome",
    "ExecutionMode",
    "OpportunityAnalysisPayload",
    "ParseOutcome",
    "ParseResult",
    "analyze_python_source_for_opportunity_miner",
    "parse_python_source",
    "shutdown_pool",
]
