"""Slice 112 — Process-Isolated Oracle: fault-tolerant async IPC.

The Oracle's graph cache is a ~1.1 GB / 2.5 M-node ``networkx.DiGraph`` whose
``pickle.loads`` is **GIL-bound** (~166 s). In-process — even via
``asyncio.to_thread`` — that deserialize freezes the engine's single event loop
(empirically: 165 s of total loop silence), starving the FSM *and* the co-booted
God-Tier gateway. ``to_thread`` cannot help (the worker thread holds the GIL).

The structural fix is **process isolation**: the Oracle runs in its OWN OS
process (its OWN GIL), so the heavy deserialize + the in-RAM networkx graph
(which `shortest_path`/`simple_cycles` require) never touch the engine loop. The
engine talks to it through :class:`AsyncOracleProxy` — a small async IPC client
covering the ~5 methods the engine actually calls (``initialize``,
``incremental_update``, ``get_metrics``, ``get_context_for_improvement``,
``shutdown``).

Resilience invariant (operator-bound): if the Oracle subprocess dies (OOM kill,
segfault, anything), the proxy catches the severed connection, narrates a
high-severity ``OracleCrash`` event, fails in-flight calls cleanly, and
autonomously respawns the process (bounded backoff) — **without ever crashing
the GovernedLoopService**. While the Oracle is hydrating (or crashed/respawning)
the proxy returns a structured :class:`OracleNotReady` so the engine + UI keep
running and degrade gracefully (compose :class:`OracleReadiness` semantics).

Master ``JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED`` — §33.1 default-FALSE. When
off, the engine uses the in-process Oracle exactly as before (byte-identical).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

# asyncio imported lazily inside coroutines so the module imports cleanly in any
# context (the child worker creates its own loop).
import asyncio

logger = logging.getLogger("ouroboros.oracle_ipc")

_TRUTHY = ("1", "true", "yes", "on")

_ENV_MASTER = "JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED"
_ENV_MAX_RESPAWNS = "JARVIS_ORACLE_MAX_RESPAWNS"
_ENV_RESPAWN_BACKOFF_S = "JARVIS_ORACLE_RESPAWN_BACKOFF_S"

# The exact, audited engine-facing surface (verify-first: these are the only
# Oracle methods orchestrator/GLS call). Anything else is rejected by the worker.
_ALLOWED_METHODS = frozenset({
    "incremental_update",
    "get_metrics",
    "get_context_for_improvement",
})


def process_isolation_enabled() -> bool:
    """§33.1 master — default FALSE. NEVER raises."""
    try:
        raw = os.environ.get(_ENV_MASTER)
        return bool(raw) and raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:  # noqa: BLE001
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:  # noqa: BLE001
        return default


# ===========================================================================
# Structured states
# ===========================================================================


@dataclass(frozen=True)
class OracleNotReady:
    """Structured sentinel returned by the proxy when the isolated Oracle cannot
    serve a query yet: hydrating its graph, init-failed, or crashed/respawning.
    Callers test ``isinstance(result, OracleNotReady)`` and degrade gracefully
    (run context-free tasks) instead of blocking or crashing."""

    reason: str = "hydrating"      # hydrating | init_failed | respawning | exhausted
    elapsed_s: float = 0.0
    respawns: int = 0


class OracleRemoteError(RuntimeError):
    """Raised in the engine when the isolated Oracle handled a call but the
    method itself raised. The remote traceback string is preserved."""


class OracleCrash:
    """High-severity marker for a severed Oracle subprocess (narrated)."""

    KIND = "oracle.crash"


# ===========================================================================
# Child worker — runs INSIDE the isolated process (own GIL)
# ===========================================================================


def _oracle_worker_main(conn: Any) -> None:
    """Top-level (spawn-picklable) child entrypoint. Builds + initializes a
    TheOracle in THIS process (the 1.1 GB load happens here, never on the engine
    loop), signals readiness, then serves requests until shutdown/EOF. NEVER
    lets an exception escape uncaught — a dying worker closes the pipe, which the
    parent proxy detects as a crash."""
    try:
        asyncio.run(_oracle_worker_async(conn))
    except Exception as exc:  # noqa: BLE001 — last-resort; pipe close signals parent
        try:
            conn.send({"control": "init_failed", "error": repr(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


async def _oracle_worker_async(conn: Any) -> None:
    from backend.core.ouroboros.oracle import TheOracle

    oracle = TheOracle()
    loop = asyncio.get_running_loop()
    try:
        await oracle.initialize()
    except Exception as exc:  # noqa: BLE001
        conn.send({"control": "init_failed", "error": repr(exc)})
        return
    conn.send({"control": "ready"})

    while True:
        try:
            msg = await loop.run_in_executor(None, conn.recv)
        except (EOFError, OSError):
            break  # parent closed the pipe → exit
        if not isinstance(msg, dict):
            continue
        if msg.get("control") == "shutdown":
            try:
                await oracle.shutdown()
            except Exception:  # noqa: BLE001
                pass
            break
        rid = msg.get("id")
        method = str(msg.get("method", ""))
        if method not in _ALLOWED_METHODS:
            conn.send({"id": rid, "ok": False, "error": f"method not allowed: {method}"})
            continue
        try:
            fn = getattr(oracle, method)
            res = fn(*msg.get("args", []), **msg.get("kwargs", {}))
            if asyncio.iscoroutine(res):
                res = await res
            conn.send({"id": rid, "ok": True, "result": res})
        except Exception as exc:  # noqa: BLE001 — per-call failure, not fatal
            conn.send({"id": rid, "ok": False, "error": repr(exc)})


# ===========================================================================
# Default spawn (real subprocess) — injectable for tests
# ===========================================================================


def _default_spawn() -> Tuple[Any, Any]:
    """Spawn the Oracle worker in a fresh process (spawn context — safe on macOS
    + clean GIL). Returns (parent_conn, process). NEVER raises into the caller's
    happy path beyond what Process.start would."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(target=_oracle_worker_main, args=(child_conn,), daemon=True)
    proc.start()
    # The parent holds its own copy of the child end open until start(); close it
    # now so an EOF propagates correctly when the child dies.
    try:
        child_conn.close()
    except Exception:  # noqa: BLE001
        pass
    return parent_conn, proc


# ===========================================================================
# AsyncOracleProxy — engine-side client (fault-tolerant)
# ===========================================================================


class AsyncOracleProxy:
    """Async, crash-tolerant proxy to the process-isolated Oracle.

    ``spawn_fn`` returns ``(connection, process)`` — injected in tests with a
    controllable fake so crash/respawn paths are deterministic without a real
    1.1 GB subprocess. ``narrator`` (optional) receives ``OracleCrash`` events
    (e.g. a DaemonNarrator) — best-effort, never fatal."""

    def __init__(
        self,
        *,
        spawn_fn: Optional[Callable[[], Tuple[Any, Any]]] = None,
        narrator: Any = None,
        max_respawns: Optional[int] = None,
        respawn_backoff_s: Optional[float] = None,
    ) -> None:
        self._spawn_fn = spawn_fn or _default_spawn
        self._narrator = narrator
        self._max_respawns = max_respawns if max_respawns is not None else _env_int(_ENV_MAX_RESPAWNS, 3)
        self._backoff = respawn_backoff_s if respawn_backoff_s is not None else _env_float(_ENV_RESPAWN_BACKOFF_S, 2.0)

        self._conn: Any = None
        self._proc: Any = None
        self._reader_task: Optional["asyncio.Task"] = None
        self._pending: Dict[int, "asyncio.Future"] = {}
        self._next_id = 0
        self._ready = False
        self._init_failed = False
        self._started_at = 0.0
        self._respawns = 0
        self._closing = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the Oracle process + begin reading. Returns immediately — the
        Oracle hydrates in the background; queries return OracleNotReady until
        the worker signals ready."""
        self._closing = False
        await self._spawn()

    async def _spawn(self) -> None:
        self._ready = False
        self._init_failed = False
        self._started_at = time.time()
        self._conn, self._proc = self._spawn_fn()
        self._reader_task = asyncio.ensure_future(self._reader())

    async def shutdown(self) -> None:
        self._closing = True
        try:
            if self._conn is not None:
                self._conn.send({"control": "shutdown"})
        except Exception:  # noqa: BLE001
            pass
        # Close the connection BEFORE awaiting the reader: the reader is blocked
        # in ``conn.recv()`` inside a default-executor thread, and cancelling the
        # task does NOT interrupt that C-level blocking call. Closing the fd
        # makes the recv raise (EOF/OSError) so the thread returns — otherwise
        # the event loop cannot shut its default executor down (teardown hangs).
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._terminate_proc()

    def _terminate_proc(self) -> None:
        try:
            if self._proc is not None and self._proc.is_alive():
                self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    # -- readiness ----------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _not_ready(self, reason: Optional[str] = None) -> OracleNotReady:
        r = reason or ("init_failed" if self._init_failed else "hydrating")
        return OracleNotReady(reason=r, elapsed_s=time.time() - self._started_at, respawns=self._respawns)

    # -- request/response ---------------------------------------------------

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke an Oracle method across IPC. Returns the result, or an
        :class:`OracleNotReady` if the Oracle can't serve yet, or raises
        :class:`OracleRemoteError` if the remote method itself failed. NEVER
        raises a connection/transport error into the caller — those degrade to
        OracleNotReady + trigger a respawn."""
        if not self._ready:
            return self._not_ready()
        loop = asyncio.get_running_loop()
        rid = self._next_id
        self._next_id += 1
        fut: "asyncio.Future" = loop.create_future()
        self._pending[rid] = fut
        try:
            self._conn.send({"id": rid, "method": method, "args": list(args), "kwargs": dict(kwargs)})
        except (BrokenPipeError, OSError, EOFError):
            self._pending.pop(rid, None)
            await self._handle_crash("send failed (pipe severed)")
            return self._not_ready("respawning")
        return await fut

    # Convenience wrappers for the audited surface ------------------------
    async def incremental_update(self, changed_files: Any = None) -> Any:
        return await self.call("incremental_update", changed_files)

    async def get_metrics(self) -> Any:
        return await self.call("get_metrics")

    async def get_context_for_improvement(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call("get_context_for_improvement", *args, **kwargs)

    # -- reader + crash handling -------------------------------------------

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                msg = await loop.run_in_executor(None, self._conn.recv)
            except (EOFError, OSError):
                if not self._closing:
                    await self._handle_crash("connection severed (worker died)")
                return
            except asyncio.CancelledError:
                return
            if not isinstance(msg, dict):
                continue
            ctrl = msg.get("control")
            if ctrl == "ready":
                self._ready = True
                logger.info("[OracleIPC] isolated Oracle ready (hydrated in %.1fs)", time.time() - self._started_at)
                continue
            if ctrl == "init_failed":
                self._init_failed = True
                self._fail_all(f"oracle init failed: {msg.get('error')}")
                continue
            rid = msg.get("id")
            fut = self._pending.pop(rid, None)
            if fut is not None and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(OracleRemoteError(str(msg.get("error"))))

    def _fail_all(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(OracleRemoteError(reason))
        self._pending.clear()

    async def _handle_crash(self, reason: str) -> None:
        """Catch a severed Oracle, narrate, fail pending calls, and respawn with
        bounded backoff. NEVER raises — a crash must not propagate into GLS."""
        self._ready = False
        self._fail_all(f"oracle crash: {reason}")
        self._terminate_proc()
        await self._narrate_crash(reason)
        if self._closing:
            return
        if self._respawns >= self._max_respawns:
            logger.error("[OracleIPC] respawn budget exhausted (%d) — Oracle stays DEGRADED; "
                         "engine continues context-free", self._max_respawns)
            return
        self._respawns += 1
        delay = self._backoff * self._respawns
        logger.warning("[OracleIPC] respawning isolated Oracle (#%d) after %.1fs: %s",
                       self._respawns, delay, reason)
        try:
            await asyncio.sleep(delay)
            if not self._closing:
                await self._spawn()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("[OracleIPC] respawn failed: %s", exc)

    async def _narrate_crash(self, reason: str) -> None:
        """Best-effort high-severity OracleCrash narration. NEVER raises."""
        if self._narrator is None:
            return
        payload = {"op_id": "oracle", "reason": f"Oracle subprocess crashed: {reason}",
                   "respawns": self._respawns}
        try:
            on_event = getattr(self._narrator, "on_event", None)
            if on_event is not None:
                res = on_event(OracleCrash.KIND, payload)
                if asyncio.iscoroutine(res):
                    await res
        except Exception:  # noqa: BLE001
            logger.debug("[OracleIPC] crash narration swallowed", exc_info=True)
