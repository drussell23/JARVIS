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


_INPROCESS_ENV: str = "JARVIS_AST_HELPER_INPROCESS_ENABLED"


def ast_helper_inprocess_enabled() -> bool:
    """Slice 128 — run the heavy AST analyze IN-PROCESS (no ``ProcessPoolExecutor``)
    when set. Default **FALSE** (§33.1) → the main process keeps the spawn pool,
    byte-identical to pre-Slice-128.

    Load-bearing for the process-isolated Oracle (``oracle_ipc``): that worker is
    spawned ``daemon=True`` and a daemonic process cannot have children, so a
    nested pool crashes ("daemonic processes are not allowed to have children").
    The Oracle is ALREADY off the main loop (its own subprocess), so running the
    analyze in-process there is correct — the redundant nested pool is what
    failed. ``oracle_ipc._oracle_worker_main`` enables this before building the
    Oracle. NEVER raises."""
    try:
        return os.environ.get(_INPROCESS_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


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


def _proc_alive(p: Any) -> bool:
    """is_alive() that never raises (a torn-down handle may throw)."""
    try:
        return bool(p.is_alive())
    except Exception:  # noqa: BLE001
        return False


def shutdown_pool(*, deadline_s: float = 5.0) -> str:
    """Deterministically tear down the AST process pool. NEVER raises.

    Returns the teardown verdict: ``"idle"`` (no pool), ``"graceful"`` (workers
    drained within ``deadline_s``), or ``"escalated"`` (workers force-terminated).

    Three layers — because ``ProcessPoolExecutor.shutdown(cancel_futures=True)``
    only drops QUEUED futures; it cannot interrupt a worker already running an AST
    parse. That is the v25 / bt-2026-06-16 ``Shutting down The Oracle...`` wedge:
    an in-flight index left running workers that no public API could stop, so the
    process never exited cleanly (and ``session_outcome`` never reached ``complete``).

      1. ``shutdown(wait=False, cancel_futures=True)`` — drop the queue, don't block.
      2. bounded ``join(timeout)`` per worker — let in-flight parses finish gracefully.
      3. escalation — ``terminate()`` (then ``kill()``) any worker still alive past
         the deadline. The pool can't interrupt running workers; the OS can.
    """
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is None:
        return "idle"
    # Snapshot the worker handles FIRST — ProcessPoolExecutor.shutdown() nulls
    # ``_processes``, so capturing after Layer 1 loses them.
    procs = list((getattr(pool, "_processes", None) or {}).values())
    # Layer 1 — drop queued futures, return immediately (no join-hang).
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:                       # cancel_futures is 3.9+; degrade safely
        try:
            pool.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    # Layer 2 — bounded graceful drain of running workers.
    deadline = time.monotonic() + max(0.0, float(deadline_s))
    for p in procs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            p.join(timeout=remaining)
        except Exception:  # noqa: BLE001
            pass
    # Layer 3 — escalate: force-terminate stragglers the pool can't interrupt.
    stuck = [p for p in procs if _proc_alive(p)]
    if not stuck:
        return "graceful"
    for p in stuck:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.2)
    for p in stuck:
        if _proc_alive(p):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    logger.warning(
        "[AstCompileHelper] ORACLE_TEARDOWN_ESCALATION: force-terminated %d stuck "
        "worker(s) past %.1fs deadline", len(stuck), float(deadline_s),
    )
    return "escalated"


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
# Slice 32 — Oracle composition surface
# ============================================================================
#
# Closes the v25 control-plane wedge (bt-2026-05-27-194342): 25-min
# asyncio loop freeze caused by GIL contention from N
# ``asyncio.to_thread`` workers running ``CodeStructureVisitor.visit``
# on pure-Python CPU-bound AST walks. The default ThreadPoolExecutor
# can't help — every worker holds the GIL during the walk, the asyncio
# event loop only gets the GIL between releases.
#
# Slice 32 composes this module's existing spawn-context
# ``ProcessPoolExecutor`` singleton (operator-bound: "build cleanly on
# what already exists, no duplication"). Oracle becomes a second
# consumer alongside OpportunityMiner — sharing the pool's lifecycle,
# its closed taxonomies, its fail-closed semantics. No parallel pool.
#
# Payload discipline (IPC-pickle safety):
#
#   * The worker imports ``CodeStructureVisitor``, ``NodeData``,
#     ``EdgeData``, ``NodeID``, ``NodeType``, ``EdgeType`` LAZILY at
#     call time (worker is a spawn process — first call pays the
#     import cost, subsequent calls reuse).
#   * The worker returns ``(nodes_list, edges_list, content_hash,
#     worker_elapsed_ms)`` — ``nodes_list`` is ``list[NodeData]``,
#     ``edges_list`` is ``list[Tuple[NodeID, NodeID, EdgeData]]``.
#     All three dataclasses are ``@dataclass`` (frozen for NodeID),
#     transitively picklable. NodeType + EdgeType are enums (picklable).
#   * NO ``ast.AST`` ever crosses the IPC boundary (operator binding:
#     "never pass a raw, un-serializable ast.AST object across IPC").
#
# Slow-call alert: the existing pool path already bounds by
# ``asyncio.wait_for(timeout=timeout_s)``. Slice 32 additionally emits
# a structured ``oracle_slow_call`` warning when ``parent_await_ms``
# exceeds 30,000ms — satisfies operator's "alert without stalling"
# requirement (the loop keeps servicing siblings during the await).


@dataclass(frozen=True)
class OracleAnalysisResult:
    """Closed-taxonomy result from ``analyze_python_source_for_oracle``.

    Reuses the ``AnalyzeOutcome`` 5-value taxonomy (no new enum —
    failure modes are identical to OpportunityMiner's). ``nodes`` +
    ``edges`` are the structurally-equivalent payload that
    ``Oracle._read_parse_visit_blocking`` returns; on any non-OK
    outcome both are empty tuples (sentinel for "skip this file").

    NEVER raises into the caller — every code path returns this shape.
    """

    outcome: AnalyzeOutcome
    nodes: Tuple[Any, ...] = ()          # tuple of NodeData
    edges: Tuple[Any, ...] = ()          # tuple of (NodeID, NodeID, EdgeData)
    content_hash: str = ""
    elapsed_ms: float = 0.0              # total parent-await wall-clock
    worker_elapsed_ms: float = 0.0       # worker-side measured time
    source_bytes: int = 0
    caller: str = ""
    execution_mode: ExecutionMode = ExecutionMode.INLINE_TINY
    error_detail: str = ""


def _worker_analyze_for_oracle_in_process(
    source: str,
    filename: str,
    repo_name: str,
    relative_path: str,
) -> Tuple[str, Any]:
    """Process-pool worker for Oracle's _index_file path.

    Runs ``ast.parse()`` + ``CodeStructureVisitor.visit(tree)`` inside
    a separate Python interpreter (its own GIL — the main asyncio
    thread keeps ticking during the walk).

    Returns ``("ok", (nodes_list, edges_list, content_hash,
    worker_elapsed_ms))`` on success, ``("syntax_error", detail)`` on
    parse failure, ``("internal_error", detail)`` for anything else
    (including the lazy oracle import failing in the worker).

    NEVER raises out of the worker — an uncaught raise would crash
    the pool worker and propagate ``BrokenProcessPool`` to the parent.

    Permitted ``ast.parse()`` call site (AST-pinned alongside the
    OpportunityMiner workers). The visitor walk happens in
    ``CodeStructureVisitor.visit`` — that's the heavy CPU-bound work
    we're isolating from the asyncio event loop.
    """
    import hashlib as _hashlib_w
    t0 = time.monotonic()

    # Lazy oracle import — runs ONCE per spawn worker, cached for the
    # life of that worker process. Avoids a main-process import cycle
    # (oracle.py imports ast_compile_helper.py for the public coro
    # below; the worker importing oracle.py at module-init would
    # cycle).
    try:
        from backend.core.ouroboros.oracle import (  # noqa: WPS433
            CodeStructureVisitor as _CSV,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return (
            "internal_error",
            f"oracle import failed in worker: {type(exc).__name__}: {exc}",
        )

    try:
        content_hash = _hashlib_w.md5(source.encode("utf-8")).hexdigest()
    except Exception as exc:  # noqa: BLE001
        return (
            "internal_error",
            f"hash failed: {type(exc).__name__}: {exc}",
        )

    try:
        tree = _ast_mod.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        return ("syntax_error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ("internal_error", f"parse: {type(exc).__name__}: {exc}")

    try:
        visitor = _CSV(repo_name, relative_path, source)
        visitor.visit(tree)
    except Exception as exc:  # noqa: BLE001
        return ("internal_error", f"visit: {type(exc).__name__}: {exc}")

    worker_elapsed_ms = (time.monotonic() - t0) * 1000.0
    return (
        "ok",
        (
            list(visitor.nodes),
            list(visitor.edges),
            content_hash,
            worker_elapsed_ms,
        ),
    )


_ORACLE_SLOW_CALL_ALERT_MS_ENV: str = "JARVIS_ORACLE_SLOW_CALL_ALERT_MS" # env var for ops to set the slow-call alert threshold (ms) 
_DEFAULT_ORACLE_SLOW_CALL_ALERT_MS: float = 30_000.0 # default slow-call alert threshold (30s) — operators can adjust via env var; set high to avoid noise, since the pool call already has a hard timeout and this is just an alert, not a kill switch

# This is a pure alert threshold — it does NOT affect the hard timeout of the pool call, which 
# remains at 10s by default (configurable via JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S). The alert is 
# for ops to be aware of slow calls that are approaching the hard timeout, without actually killing 
# the call. The pool call's hard timeout is the real kill switch to prevent runaway calls; this 
# alert is just a heads-up for ops to investigate if they see it frequently, or if they want to 
# adjust the hard timeout based on observed call durations.
def _resolve_oracle_slow_call_alert_ms() -> float:
    try: # defensive parsing of the env var, with a fail-safe default if it's not set or invalid
        raw = os.environ.get(_ORACLE_SLOW_CALL_ALERT_MS_ENV, "").strip() # type: ignore 
        if not raw: # empty or whitespace-only means "use the default" 
            return _DEFAULT_ORACLE_SLOW_CALL_ALERT_MS # fail-safe default 
        return max(0.0, float(raw)) # clamp to non-negative, since negative doesn't make sense for a time threshold 
    except (TypeError, ValueError): # in case of invalid env var value, log a warning and return the default 
        return _DEFAULT_ORACLE_SLOW_CALL_ALERT_MS # fail-safe default

# Resolve the alert threshold at module load time, so we don't have to parse the env var on every call. 
# This is just a single float value that the module-level functions can reference. Operators can set 
# the env var before starting the service, and it will take effect without needing a code change or 
# redeploy. The default is 30 seconds, which is intentionally high to avoid noise, since the pool call 
# already has a hard timeout (default 10s) that will kill runaway calls. This alert is just a heads-up 
# for ops to investigate if they see it frequently, or if they want to adjust the hard timeout based 
# on observed call durations. 
async def analyze_python_source_for_oracle(
    caller: str, # mandatory provenance label (e.g. "oracle._index_file") for structured logging and telemetry 
    source: str, # decoded Python source text; caller is responsible for the read_text(encoding="utf-8") step, to keep the file-read cost on the parent and avoid doing it in the worker on every call (also keeps it symmetric with how OpportunityMiner pre-decodes)
    *, # keyword-only parameters for clarity and to avoid mistakes in argument order; most have defaults 
    filename: str = "<unknown>", # logical filename for ast.parse error messages; does not affect the IPC payload or the analysis logic, just used for error reporting in the worker if the parse fails
    repo_name: str = "", # forwarded to CodeStructureVisitor.__init__; does not affect the IPC payload or the analysis logic, just included in the worker for any repo-specific logic the visitor might have (e.g. special handling for certain repos) 
    relative_path: str = "", # forwarded to CodeStructureVisitor.__init__; does not affect the IPC payload or the analysis logic, just included in the worker for any path-specific logic the visitor might have (e.g. special handling for certain paths) 
    timeout_s: Optional[float] = None, # hard timeout for the parse+walk operation; defaults to JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S (10s); Oracle scans can include large generated files, so we want a hard timeout to prevent runaway calls; operators can raise this via env if they have larger files and want to allow more time, but the default should be sufficient for most cases and prevents stalling the loop indefinitely; beyond JARVIS_ORACLE_SLOW_CALL_ALERT_MS (default 30s) a structured oracle_slow_call warning is logged but the operation continues to completion (or hard timeout) — this satisfies the operator requirement to "alert without stalling" for slow calls that are approaching the hard timeout   
    max_bytes: Optional[int] = None, # source-size ceiling; above this returns TOO_LARGE without touching the pool, to fail fast on files that are too big to analyze and avoid the overhead of dispatching to the worker; defaults to JARVIS_AST_HELPER_MAX_BYTES, which is a reasonable upper bound for analyzable files and can be adjusted by operators if needed 
    tiny_threshold_override: Optional[int] = None, # for tests; sources at/below this size are inline-parsed and analyzed without going through the pool, since the work is genuinely cheap and IPC overhead would dominate; defaults to _resolve_tiny_threshold(), which is typically around 4KB based on empirical measurements of parse+walk times for small files; tests can set this to a smaller value to force the inline path for more cases, or to a larger value to test the process pool path for smaller files 
) -> OracleAnalysisResult: # always populated; never raises into the caller; on any non-OK outcome nodes and edges are empty tuples (sentinel: "skip this file" — mirrors legacy _read_parse_visit_blocking returning None) 
    """Slice 32 — off-loop parse + visitor walk for Oracle._index_file.

    Composition counterpart to
    ``analyze_python_source_for_opportunity_miner``. The worker
    performs ``ast.parse()`` **and** ``CodeStructureVisitor.visit(tree)``
    in a separate process; the parent receives only primitive-equivalent
    dataclass payloads (``NodeData`` + ``EdgeData`` + ``NodeID``). NO
    ``ast.AST`` ever crosses the IPC boundary.

    Parameters
    ----------
    caller:
        Mandatory provenance label
        (e.g. ``"oracle._index_file"``). Logged into the structured
        ``[AstCompileHelper]`` log line on each call.
    source:
        Decoded Python source text. Caller is responsible for the
        ``read_text(encoding="utf-8")`` step (kept on the parent so
        the worker doesn't pay the file-read cost on every call —
        symmetric with how OpportunityMiner pre-decodes).
    filename:
        Logical filename for ``ast.parse`` error messages.
    repo_name:
        Forwarded to ``CodeStructureVisitor.__init__``.
    relative_path:
        Forwarded to ``CodeStructureVisitor.__init__``.
    timeout_s:
        Hard timeout for the parse+walk operation. Defaults to
        ``JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S`` (10s). Oracle scans
        can include large generated files; operators can raise via
        env. Beyond ``JARVIS_ORACLE_SLOW_CALL_ALERT_MS`` (default
        30s) a structured ``oracle_slow_call`` warning is logged but
        the operation continues to completion (or hard timeout).
    max_bytes:
        Source-size ceiling. Above this returns ``TOO_LARGE`` without
        touching the pool.
    tiny_threshold_override:
        For tests. Sources at/below this size are inline-parsed.

    Returns
    -------
    OracleAnalysisResult
        Always populated. NEVER raises into the caller. On any
        non-OK outcome ``nodes`` and ``edges`` are empty tuples
        (sentinel: "skip this file" — mirrors legacy
        ``_read_parse_visit_blocking`` returning ``None``).
    """
    t0 = time.monotonic() # start the clock immediately on call entry, to capture the full parent-await time including any early fast-paths; the worker will report its own elapsed time for the parse+walk, and the parent can calculate the pure await time by subtracting the worker time from the total elapsed time at the outcome boundary 
    
    # Resolve parameters with defaults. The caller can override the defaults for timeout, max_bytes, 
    # and tiny_threshold, but the common case is to rely on the defaults which are set based on 
    # empirical measurements and operational experience. The source_bytes calculation is 
    # straightforward — we need it for the too-large fast-path and for logging; if source is not a 
    # string (defensive), we treat it as 0 bytes to avoid false positives on the size check. 
    source_bytes = (
        len(source.encode("utf-8")) if isinstance(source, str) else 0 # source_bytes is the length of the UTF-8 encoded source, which is what matters for the size checks and logging; if source is not a string, we defensively treat it as 0 bytes to avoid false positives on the size check, since we can't analyze non-string sources anyway 
    )

    # Resolve the effective timeout, max_bytes, and tiny_threshold based on the provided parameters 
    # or the defaults. This allows the caller to override these values if needed (e.g. for testing 
    # or for specific cases), while still having reasonable defaults for the common case. The 
    # effective_timeout is used for the process pool path; the max_bytes is used for the too-large 
    # fast-path; the tiny_threshold determines whether we take the inline path or the process pool path. 
    effective_timeout = (
        float(timeout_s) if timeout_s is not None # if the caller provided a timeout_s, we use it; otherwise we resolve the default timeout from the environment variable or the hardcoded default 
        else _resolve_default_timeout_s() # this function reads the JARVIS_AST_HELPER_DEFAULT_TIMEOUT_S env var and falls back to a hardcoded default if it's not set or invalid; this allows operators to configure the default timeout without changing code, while still having a reasonable default for safety
    )

    # The effective_max_bytes is the ceiling for source size; if the source exceeds this, we return 
    # TOO_LARGE without dispatching to the worker, to fail fast and avoid the overhead of IPC for files 
    # that are too big to analyze. The tiny_threshold determines whether we take the inline path (for 
    # small sources where the work is cheap and IPC overhead would dominate) or the process pool 
    # path (for larger sources where we want to isolate the CPU-bound work from the asyncio event 
    # loop). Both of these thresholds can be overridden by the caller, but they have sensible defaults 
    # based on empirical measurements and operational experience.
    effective_max_bytes = (
        int(max_bytes) if max_bytes is not None # if the caller provided a max_bytes, we use it; otherwise we resolve the default max_bytes from the environment variable or the hardcoded default
        else _resolve_default_max_bytes() # this function reads the JARVIS_AST_HELPER_MAX_BYTES env var and falls back to a hardcoded default if it's not set or invalid; this allows operators to configure the max_bytes threshold without changing code, while still having a reasonable default to prevent trying to analyze files that are too large
    )

    # The tiny_threshold determines the cutoff for taking the inline path versus the process pool path. 
    # For sources at or below this size, we parse and analyze inline on the caller's thread, since the 
    # work is genuinely cheap (empirically under 5ms for sources around 4KB) and IPC overhead would 
    # dominate. For sources above this threshold, we dispatch to the process pool to isolate the 
    # CPU-bound work from the asyncio event loop. The default tiny_threshold is based on empirical 
    # measurements of parse+walk times for small files, but it can be overridden by the caller (e.g. 
    # in tests) if they want to force more cases through the inline path or the process pool path.
    tiny_threshold = (
        int(tiny_threshold_override) # if the caller provided a tiny_threshold_override, we use it; otherwise we resolve the default tiny_threshold from the environment variable or the hardcoded default
        if tiny_threshold_override is not None # we check explicitly for None to allow the caller to set it to 0 or any other value; if it's not None, we use the provided value; if it is None, we resolve the default tiny_threshold from the environment variable or the hardcoded default; this allows operators to configure the tiny threshold without changing code, while still having a reasonable default based on empirical measurements of when the inline path is actually faster than the process pool path 
        else _resolve_tiny_threshold() # this function reads the JARVIS_AST_HELPER_TINY_THRESHOLD_BYTES env var and falls back to a hardcoded default if it's not set or invalid; this allows operators to configure the tiny threshold without changing code, while still having a reasonable default based on empirical measurements of when the inline path is actually faster than the process pool path 
    )

    # Too-large fast-path. If the source exceeds the effective_max_bytes, we return TOO_LARGE without 
    # dispatching to the worker, to fail fast and avoid the overhead of IPC for files that are too big 
    # to analyze. We also log this event with an info level, since it's a normal occurrence that we want 
    # to be aware of but it's not necessarily a problem (e.g. some generated files might be large and 
    # we just want to skip them). The log includes the caller, the source size, the max_bytes threshold,
    #  and the elapsed time up to this point.
    if source_bytes > effective_max_bytes:
        # Since this is a fast-path that returns early, we want to capture the elapsed time up to this 
        # point for telemetry purposes. This includes the time taken to calculate source_bytes and resolve 
        # the effective thresholds, which is part of the overall cost of handling this file even though 
        # we don't dispatch to the worker. We log this at the info level, since it's a normal occurrence 
        # that we want to be aware of but it's not necessarily a problem (e.g. some generated files might 
        # be large and we just want to skip them). The log includes the caller, the source size, the 
        # max_bytes threshold, and the elapsed time up to this point. Then we return an 
        # OracleAnalysisResult with the TOO_LARGE outcome, including the elapsed time, source size, 
        # caller, execution mode (inline tiny, since we didn't dispatch), and an error detail message 
        # explaining that the source exceeds the max_bytes threshold. 
        elapsed_ms = (time.monotonic() - t0) * 1000.0 
        logger.info(
            "[AstCompileHelper] caller=%s outcome=too_large "
            "kind=analyze_oracle source_bytes=%d max_bytes=%d "
            "elapsed_ms=%.2f",
            caller, source_bytes, effective_max_bytes, elapsed_ms,
        )
        # We return an OracleAnalysisResult with the TOO_LARGE outcome, including the elapsed time, 
        # source size, caller, execution mode (inline tiny, since we didn't dispatch), and an error 
        # detail message explaining that the source exceeds the max_bytes threshold. The nodes and 
        # edges are empty tuples as a sentinel for "skip this file". 
        return OracleAnalysisResult(
            # We use the TOO_LARGE outcome to indicate that the file was too big to analyze, and we 
            # didn't even attempt to parse it. This is a normal case that we want to be able to 
            # distinguish from other failure modes (e.g. syntax errors or internal errors), since 
            # it just means we skipped the file due to size. The caller can check for this outcome and 
            # decide how to handle it (e.g. log it, alert on it, etc.) without treating it as an error 
            # in the analysis logic. 
            outcome=AnalyzeOutcome.TOO_LARGE,
            # Since we didn't attempt to parse the file, we set nodes and edges to empty tuples as a 
            # sentinel for "skip this file". This mirrors the legacy behavior of _read_parse_visit_blocking 
            # returning None for files that were too large or had syntax errors. The content_hash is 
            # also empty since we didn't compute it. The elapsed_ms captures the time taken up to this 
            # point, which includes the size check and any parameter resolution. The worker_elapsed_ms 
            # is 0 since we didn't dispatch to the worker. The execution_mode is INLINE_TINY since we 
            # handled this on the caller's thread without dispatching. The error_detail explains that 
            # the source exceeds the max_bytes threshold, which can be useful for debugging or 
            # operational awareness. 
            elapsed_ms=elapsed_ms,
            # The worker_elapsed_ms is 0 since we didn't dispatch to the worker. This distinguishes this 
            # case from a case where we dispatched to the worker but it took a long time or failed; in 
            # this case we didn't even try to parse it, so the worker time is 0.  
            source_bytes=source_bytes,
            # The caller is included in the result for structured logging and telemetry purposes, so we 
            # can track which callers are encountering too-large files. This is important for 
            # operational awareness and for identifying if certain parts of the codebase are more 
            # likely to have large files that exceed the threshold. The execution_mode is INLINE_TINY 
            # since we handled this on the caller's thread without dispatching to the worker. The 
            # error_detail explains that the source exceeds the max_bytes threshold, which can be 
            # useful for debugging or operational awareness. 
            caller=caller,
            # The execution_mode is INLINE_TINY since we handled this on the caller's thread without 
            # dispatching to the worker. This distinguishes it from cases where we dispatched to the 
            # worker and it failed or took a long time; in this case we didn't even try to parse it, 
            # so it's an inline fast-path result. The error_detail explains that the source exceeds 
            # the max_bytes threshold, which can be useful for debugging or operational awareness. 
            execution_mode=ExecutionMode.INLINE_TINY,
            error_detail=(
                f"source {source_bytes}B exceeds max_bytes "
                f"{effective_max_bytes}B"
            ),
        )

    # Inline-tiny path. For ≤4KB files the visitor walk is so cheap
    # (<5ms) that IPC overhead would dominate — direct-call the
    # worker on the asyncio thread. We accept this minor block: a
    # 5ms parse for a 4KB file is below the ControlPlaneWatchdog
    # threshold (500ms).
    if source_bytes <= tiny_threshold:
        return _inline_tiny_analyze_for_oracle(
            caller=caller, source=source, filename=filename,
            repo_name=repo_name, relative_path=relative_path,
            source_bytes=source_bytes, t0=t0,
        )

    # Process-pool path — the heavy case (the v25 wedge driver).
    return await _process_pool_analyze_for_oracle(
        caller=caller, source=source, filename=filename,
        repo_name=repo_name, relative_path=relative_path,
        source_bytes=source_bytes, t0=t0,
        timeout_s=effective_timeout,
    )


def _inline_tiny_analyze_for_oracle(
    *,
    caller: str,
    source: str,
    filename: str,
    repo_name: str,
    relative_path: str,
    source_bytes: int,
    t0: float,
) -> OracleAnalysisResult:
    """Tiny-source inline path — runs the worker fn directly on the
    asyncio thread. <5ms for sources ≤ tiny_threshold; below the
    ControlPlaneWatchdog 500ms threshold."""
    try:
        outcome_label, payload = _worker_analyze_for_oracle_in_process(
            source, filename, repo_name, relative_path,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.INLINE_TINY,
            error_detail=f"{type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    return _oracle_result_from_worker_payload(
        outcome_label=outcome_label,
        payload=payload,
        caller=caller,
        source_bytes=source_bytes,
        elapsed_ms=elapsed_ms,
        execution_mode=ExecutionMode.INLINE_TINY,
    )


async def _process_pool_analyze_for_oracle(
    *,
    caller: str,
    source: str,
    filename: str,
    repo_name: str,
    relative_path: str,
    source_bytes: int,
    t0: float,
    timeout_s: float,
) -> OracleAnalysisResult:
    """Process-pool path — the heavy case. Submits to spawn worker,
    awaits with bounded ``asyncio.wait_for``, returns structured
    result. Asyncio main thread keeps ticking during the await — the
    GIL contention that wedged v25 is now in the child process."""
    loop = asyncio.get_running_loop()
    # Slice 128 — when in-process mode is on (the isolated Oracle subprocess,
    # where a nested ProcessPoolExecutor would crash daemonic), dispatch the
    # worker on a thread instead of the spawn pool. The worker is CPU-bound but
    # we are already off the MAIN loop (this runs in the Oracle's own process);
    # the thread keeps THIS subprocess's IPC loop responsive during the parse.
    # Default off → the spawn pool path is byte-identical to pre-Slice-128.
    if ast_helper_inprocess_enabled():
        _dispatch = asyncio.to_thread(
            _worker_analyze_for_oracle_in_process,
            source, filename, repo_name, relative_path,
        )
    else:
        pool = _get_pool()
        _dispatch = loop.run_in_executor(
            pool, _worker_analyze_for_oracle_in_process,
            source, filename, repo_name, relative_path,
        )

    try:
        worker_tuple = await asyncio.wait_for(
            _dispatch,
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        logger.warning(
            "[AstCompileHelper] caller=%s outcome=timeout "
            "kind=analyze_oracle execution_mode=process "
            "source_bytes=%d timeout_s=%.1f parent_await_ms=%.1f",
            caller, source_bytes, timeout_s, elapsed_ms,
        )
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.TIMEOUT,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=(
                f"analyze_oracle exceeded {timeout_s:.1f}s in pool"
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=(
                f"pool dispatch failed: {type(exc).__name__}: {exc}"
            ),
        )

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    try:
        outcome_label, payload = worker_tuple
    except (ValueError, TypeError):
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.INTERNAL_ERROR,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=ExecutionMode.PROCESS,
            error_detail=(
                f"worker returned unexpected shape: "
                f"{type(worker_tuple).__name__}"
            ),
        )

    result = _oracle_result_from_worker_payload(
        outcome_label=outcome_label,
        payload=payload,
        caller=caller,
        source_bytes=source_bytes,
        elapsed_ms=elapsed_ms,
        execution_mode=ExecutionMode.PROCESS,
    )

    # Slow-call alert — operator binding: "log an event plane alert
    # without stalling". Fires at WARNING level when total parent
    # await exceeds the configured threshold (default 30s). The
    # operation already completed; the asyncio loop did not block
    # (the await yielded continuously); this is observability only.
    alert_threshold_ms = _resolve_oracle_slow_call_alert_ms()
    if (
        alert_threshold_ms > 0.0
        and elapsed_ms > alert_threshold_ms
        and result.outcome == AnalyzeOutcome.OK
    ):
        logger.warning(
            "[AstCompileHelper] oracle_slow_call caller=%s "
            "execution_mode=process source_bytes=%d "
            "parent_await_ms=%.1f worker_elapsed_ms=%.1f "
            "threshold_ms=%.1f — loop kept ticking; this is "
            "observability, not abort",
            caller, source_bytes, elapsed_ms,
            result.worker_elapsed_ms, alert_threshold_ms,
        )
    elif result.outcome == AnalyzeOutcome.OK:
        logger.debug(
            "[AstCompileHelper] caller=%s outcome=ok "
            "kind=analyze_oracle execution_mode=process "
            "source_bytes=%d worker_elapsed_ms=%.1f "
            "parent_await_ms=%.1f",
            caller, source_bytes,
            result.worker_elapsed_ms, elapsed_ms,
        )
    return result


def _oracle_result_from_worker_payload(
    *,
    outcome_label: str,
    payload: Any,
    caller: str,
    source_bytes: int,
    elapsed_ms: float,
    execution_mode: ExecutionMode,
) -> OracleAnalysisResult:
    """Decode worker ``(label, payload)`` tuple into
    ``OracleAnalysisResult``. Shared by inline-tiny and process
    paths so the decode logic lives in exactly one place
    (mirrors ``_result_from_worker_payload`` for OpportunityMiner)."""
    if outcome_label == "ok":
        try:
            nodes_list, edges_list, content_hash, worker_elapsed_ms = (
                payload
            )
        except (ValueError, TypeError):
            return OracleAnalysisResult(
                outcome=AnalyzeOutcome.INTERNAL_ERROR,
                elapsed_ms=elapsed_ms,
                source_bytes=source_bytes,
                caller=caller,
                execution_mode=execution_mode,
                error_detail=(
                    f"ok payload shape unexpected: "
                    f"{type(payload).__name__}"
                ),
            )
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.OK,
            nodes=tuple(nodes_list),
            edges=tuple(edges_list),
            content_hash=str(content_hash),
            elapsed_ms=elapsed_ms,
            worker_elapsed_ms=float(worker_elapsed_ms),
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=execution_mode,
        )
    if outcome_label == "syntax_error":
        return OracleAnalysisResult(
            outcome=AnalyzeOutcome.SYNTAX_ERROR,
            elapsed_ms=elapsed_ms,
            source_bytes=source_bytes,
            caller=caller,
            execution_mode=execution_mode,
            error_detail=str(payload),
        )
    return OracleAnalysisResult(
        outcome=AnalyzeOutcome.INTERNAL_ERROR,
        elapsed_ms=elapsed_ms,
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
    "OracleAnalysisResult",
    "ParseOutcome",
    "ParseResult",
    "analyze_python_source_for_opportunity_miner",
    "analyze_python_source_for_oracle",
    "parse_python_source",
    "shutdown_pool",
]
