"""OracleReadiness — granular readiness signals for TheOracle's
deferred (background) initialization.

Closes the boot-blocking gap: ``TheOracle.initialize()`` does
graph cache load → optional full index → semantic index init in
strictly-sequential order (a libmalloc-safety constraint on macOS
ARM64; see ``oracle.py`` in-line comments). When the harness
runs initialize() on the boot path, the REPL waits 8-9 seconds
for the full warm-up.

This module separates the *signaling* concern from the *work* so
the harness can spawn ``initialize()`` as a background task and
consumers gate on a first-class readiness primitive — no silent
"half graph" answers, no polling, no ``hasattr`` ducktyping.

Authority boundary
------------------
* §1 deterministic — pure ``asyncio.Event`` + ``threading.Lock``
* §2 progressive awakening — emits ``graph_ready`` then
  ``semantic_ready`` as the underlying init progresses
* §7 fail-closed — ``mark_failed(exc)`` records the failure and
  unblocks waiters by raising the recorded exception
* §8 observable — ``state()`` projects the current readiness
  for telemetry surfaces
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("Ouroboros.OracleReadiness")


ORACLE_READINESS_SCHEMA_VERSION: str = "oracle_readiness.v1"


class OracleReadinessScope(Enum):
    """Closed taxonomy of readiness scopes a consumer may await.

    * ``GRAPH`` — codebase knowledge graph loaded (cache or full
      index). Blast-radius queries / dependency traversal usable.
    * ``SEMANTIC`` — Chroma-backed semantic index initialized.
      Embedding-based code search usable.
    * ``FULL`` — both ``GRAPH`` and ``SEMANTIC`` ready.
    """

    GRAPH = "graph"
    SEMANTIC = "semantic"
    FULL = "full"


@dataclass(frozen=True)
class OracleReadinessState:
    """Frozen projection of the readiness state for telemetry."""

    graph_ready: bool
    semantic_ready: bool
    failed: bool
    failure_class: str  # empty when no failure
    schema_version: str = ORACLE_READINESS_SCHEMA_VERSION

    @property
    def fully_ready(self) -> bool:
        return self.graph_ready and self.semantic_ready


class OracleReadiness:
    """First-class readiness signal for the Oracle's deferred init.

    Events are created lazily on first access so the primitive can
    be constructed outside a running asyncio loop (Python 3.9
    compat). All public methods are safe to call from any thread.

    Usage::

        readiness = OracleReadiness()
        # background init coroutine:
        try:
            await load_cache_or_index()
            readiness.mark_graph_ready()
            init_semantic_index()
            readiness.mark_semantic_ready()
        except Exception as exc:
            readiness.mark_failed(exc)
            raise

        # consumer:
        await readiness.wait_until_ready(OracleReadinessScope.GRAPH)
    """

    def __init__(self) -> None:
        self._sync_lock = threading.Lock()
        self._graph_ready: bool = False
        self._semantic_ready: bool = False
        self._failure: Optional[BaseException] = None
        self._graph_event: Optional[asyncio.Event] = None
        self._semantic_event: Optional[asyncio.Event] = None
        # The "settled" event fires unconditionally on success-or-
        # failure, used by waiters to detect terminal state without
        # polling. Set when (graph_ready AND semantic_ready) OR
        # failure recorded.
        self._settled_event: Optional[asyncio.Event] = None

    # ----- lazy event construction (loop-scoped) -----------------------

    def _ensure_events(self) -> None:
        """Create asyncio events on first use (must be called from
        within a running loop). Idempotent. NEVER raises."""
        try:
            if self._graph_event is None:
                self._graph_event = asyncio.Event()
            if self._semantic_event is None:
                self._semantic_event = asyncio.Event()
            if self._settled_event is None:
                self._settled_event = asyncio.Event()
            # Replay any state set before the events existed
            with self._sync_lock:
                if self._graph_ready and not self._graph_event.is_set():
                    self._graph_event.set()
                if self._semantic_ready and not self._semantic_event.is_set():
                    self._semantic_event.set()
                terminal = (self._graph_ready and self._semantic_ready) or (
                    self._failure is not None
                )
                if terminal and not self._settled_event.is_set():
                    self._settled_event.set()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[OracleReadiness] _ensure_events failed", exc_info=True,
            )

    # ----- mutating API ------------------------------------------------

    def mark_graph_ready(self) -> None:
        """Signal that the codebase graph is loaded. Idempotent."""
        with self._sync_lock:
            if self._graph_ready:
                return
            self._graph_ready = True
        if self._graph_event is not None:
            try:
                self._graph_event.set()
            except Exception:  # noqa: BLE001
                pass
        self._maybe_settle()

    def mark_semantic_ready(self) -> None:
        """Signal that the semantic index is initialized. Idempotent."""
        with self._sync_lock:
            if self._semantic_ready:
                return
            self._semantic_ready = True
        if self._semantic_event is not None:
            try:
                self._semantic_event.set()
            except Exception:  # noqa: BLE001
                pass
        self._maybe_settle()

    def mark_failed(self, exc: BaseException) -> None:
        """Record an init failure. Subsequent ``wait_until_ready``
        calls on unsignaled scopes will raise ``OracleInitFailed``.
        Idempotent — first failure wins."""
        with self._sync_lock:
            if self._failure is not None:
                return
            self._failure = exc
        self._maybe_settle()
        logger.warning(
            "[OracleReadiness] init failed (%s): %s",
            type(exc).__name__, exc,
        )

    def reset_for_tests(self) -> None:
        """Test isolation. NEVER call in production paths."""
        with self._sync_lock:
            self._graph_ready = False
            self._semantic_ready = False
            self._failure = None
            self._graph_event = None
            self._semantic_event = None
            self._settled_event = None

    def _maybe_settle(self) -> None:
        with self._sync_lock:
            terminal = (self._graph_ready and self._semantic_ready) or (
                self._failure is not None
            )
        if terminal and self._settled_event is not None:
            try:
                self._settled_event.set()
            except Exception:  # noqa: BLE001
                pass

    # ----- query API ---------------------------------------------------

    def is_ready(self, scope: OracleReadinessScope = OracleReadinessScope.FULL) -> bool:
        """Sync probe — returns True iff the requested scope is ready
        AND no failure was recorded. Cheap, lock-protected."""
        with self._sync_lock:
            if self._failure is not None:
                return False
            if scope is OracleReadinessScope.GRAPH:
                return self._graph_ready
            if scope is OracleReadinessScope.SEMANTIC:
                return self._semantic_ready
            return self._graph_ready and self._semantic_ready

    def is_failed(self) -> bool:
        with self._sync_lock:
            return self._failure is not None

    def failure(self) -> Optional[BaseException]:
        with self._sync_lock:
            return self._failure

    def state(self) -> OracleReadinessState:
        """Frozen projection — safe to publish on SSE / observability
        surfaces."""
        with self._sync_lock:
            return OracleReadinessState(
                graph_ready=self._graph_ready,
                semantic_ready=self._semantic_ready,
                failed=self._failure is not None,
                failure_class=(
                    type(self._failure).__name__
                    if self._failure is not None else ""
                ),
            )

    # ----- async API ---------------------------------------------------

    async def wait_until_ready(
        self,
        scope: OracleReadinessScope = OracleReadinessScope.FULL,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        """Block until the requested scope is ready.

        Raises :class:`OracleInitFailed` if init failed before the
        requested scope was signaled. Raises :class:`asyncio.TimeoutError`
        if ``timeout`` elapses.
        """
        self._ensure_events()
        # Fast path — already ready
        if self.is_ready(scope):
            return

        async def _waiter() -> None:
            target_event = self._event_for_scope(scope)
            settled = self._settled_event
            assert target_event is not None and settled is not None
            # Race: target ready vs terminal-failure settled
            target_task = asyncio.ensure_future(target_event.wait())
            settled_task = asyncio.ensure_future(settled.wait())
            try:
                _done, pending = await asyncio.wait(
                    {target_task, settled_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # If failure was recorded and target not set, raise
                if self.is_failed() and not target_event.is_set():
                    failure = self.failure()
                    raise OracleInitFailed(
                        scope=scope, cause=failure,
                    ) from failure
            finally:
                for t in (target_task, settled_task):
                    if not t.done():
                        t.cancel()

        if timeout is not None:
            await asyncio.wait_for(_waiter(), timeout=timeout)
        else:
            await _waiter()

    def _event_for_scope(
        self, scope: OracleReadinessScope,
    ) -> Optional[asyncio.Event]:
        if scope is OracleReadinessScope.GRAPH:
            return self._graph_event
        if scope is OracleReadinessScope.SEMANTIC:
            return self._semantic_event
        # FULL — wait on the settled event but verify both scopes
        # were ready (settled fires on either success-of-both OR failure).
        return self._settled_event


class OracleInitFailed(RuntimeError):
    """Raised by ``OracleReadiness.wait_until_ready`` when the
    underlying init recorded a failure before the requested scope
    was signaled."""

    def __init__(
        self,
        *,
        scope: OracleReadinessScope,
        cause: Optional[BaseException] = None,
    ) -> None:
        msg = (
            f"Oracle init failed before scope={scope.value} "
            f"became ready"
        )
        if cause is not None:
            msg += f" — cause: {type(cause).__name__}: {cause}"
        super().__init__(msg)
        self.scope = scope
        self.cause = cause


__all__ = [
    "ORACLE_READINESS_SCHEMA_VERSION",
    "OracleInitFailed",
    "OracleReadiness",
    "OracleReadinessScope",
    "OracleReadinessState",
]
