"""Slice 150 — The Sovereign Decoupling Matrix: reusable subprocess-compute substrate.

The event-loop starvation root cause (Slice 149) is GIL-bound CPU work — the
SemanticIndex k-means build (~9.8s) and posture collectors (~32s) hold the GIL even
when run via ``asyncio.to_thread``, because threads cannot escape the GIL. Processes
can. This module COMPOSES the proven ``oracle_ipc`` parent machinery
(``AsyncOracleProxy``: spawn → bounded respawn → async ``recv`` via executor →
fail-closed ``OracleNotReady``) to run arbitrary CPU-bound work in a dedicated
spawn-process, so the GovernedLoopService event loop NEVER stalls.

It deliberately REUSES ``AsyncOracleProxy`` (the parent side is worker-agnostic:
injectable ``spawn_fn`` + the generic ``{id, method, args}``→``{id, ok, result}``
protocol) rather than reinventing the IPC wheel — only the worker entrypoint and a
spawn helper are new. Workers built with :func:`run_worker_loop` speak the exact
protocol ``AsyncOracleProxy`` expects.

Gated ``JARVIS_COMPUTE_ISOLATION_ENABLED`` default-FALSE per §33.1. Fail-closed: a
crashed/hung/not-yet-ready worker yields ``OracleNotReady`` from ``proxy.call`` —
callers keep their existing in-process fallback (e.g. SemanticIndex.score against
the current centroid), so an isolation failure NEVER takes down the loop.
"""
from __future__ import annotations

import multiprocessing
import os
from typing import Any, Callable, Dict, Tuple

# Reuse the proven parent IPC machinery — compose, don't reinvent.
from backend.core.ouroboros.oracle_ipc import (
    AsyncOracleProxy,
    OracleNotReady,
)

_ENV_MASTER = "JARVIS_COMPUTE_ISOLATION_ENABLED"


def compute_isolation_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def spawn_worker(worker_main: Callable[[Any], None]) -> Tuple[Any, Any]:
    """Spawn ``worker_main(child_conn)`` in a fresh spawn-process. Returns
    ``(parent_conn, process)``. ``worker_main`` MUST be a top-level (spawn-picklable)
    function. Mirrors ``oracle_ipc._default_spawn``: spawn context (clean GIL, safe
    on macOS), duplex Pipe, daemon process, close the parent's child-end so an EOF
    propagates when the child dies."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(target=worker_main, args=(child_conn,), daemon=True)
    proc.start()
    try:
        child_conn.close()
    except Exception:  # noqa: BLE001
        pass
    return parent_conn, proc


def run_worker_loop(
    conn: Any,
    handlers: Dict[str, Callable[..., Any]],
    *,
    signal_ready: bool = True,
) -> None:
    """Worker-side request loop — runs IN the child process. Implements the
    ``oracle_ipc`` protocol so ``AsyncOracleProxy`` drives it unmodified:

      1. send ``{"control": "ready"}`` (so the proxy flips ``is_ready``),
      2. recv ``{"id", "method", "args", "kwargs"}``,
      3. dispatch ``handlers[method](*args, **kwargs)``,
      4. send ``{"id", "ok": True, "result": ...}`` or ``{"id", "ok": False, "error": ...}``.

    Fail-closed PER CALL: a handler exception is reported, the loop survives.
    ``{"control": "shutdown"}`` or a closed pipe (EOF/OSError) ends it cleanly.
    NEVER raises out of the worker. Results must be picklable (Pipe transport)."""
    if signal_ready:
        try:
            conn.send({"control": "ready"})
        except Exception:  # noqa: BLE001 — pipe already gone
            return
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break  # parent closed the pipe → exit
        if not isinstance(msg, dict):
            continue
        if msg.get("control") == "shutdown":
            break
        rid = msg.get("id")
        method = str(msg.get("method", ""))
        handler = handlers.get(method)
        if handler is None:
            try:
                conn.send({"id": rid, "ok": False, "error": f"no handler: {method}"})
            except Exception:  # noqa: BLE001
                break
            continue
        try:
            result = handler(*msg.get("args", []), **msg.get("kwargs", {}))
            conn.send({"id": rid, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001 — per-call failure, never fatal
            try:
                conn.send({"id": rid, "ok": False, "error": repr(exc)})
            except Exception:  # noqa: BLE001
                break


def make_compute_proxy(
    worker_main: Callable[[Any], None],
    **proxy_kwargs: Any,
) -> AsyncOracleProxy:
    """Build an ``AsyncOracleProxy`` (reused parent machinery) that spawns
    ``worker_main``. Callers ``await proxy.start()`` then ``await proxy.call(method,
    *args)``; an ``OracleNotReady`` return means the worker can't serve yet (hydrating
    / crashed / respawn-exhausted) → use your in-process fallback (fail-closed).

    Composition: only ``spawn_fn`` is overridden — the proxy's spawn/respawn/backoff/
    async-recv/fail-closed logic is the same code that runs the Oracle in production."""
    return AsyncOracleProxy(
        spawn_fn=lambda: spawn_worker(worker_main),
        **proxy_kwargs,
    )


def is_not_ready(result: Any) -> bool:
    """True if a ``proxy.call`` result is the fail-closed not-ready sentinel —
    the signal for a caller to fall back to its in-process path."""
    return isinstance(result, OracleNotReady)


__all__ = [
    "compute_isolation_enabled",
    "spawn_worker",
    "run_worker_loop",
    "make_compute_proxy",
    "is_not_ready",
    "OracleNotReady",
]
