"""Bounded cancellation guard — surgical socket abort primitive (Slice 7b).

Empirical context — bt-2026-05-21-214521 X-ray (Slice 6 trace):

    14:55:03  → ClaudeProvider stream timeout=188.1s
    14:58:58  ← stream terminated via CancelledError:
                  elapsed=234.9s  budget=188.1s  ← +47s OVERRUN

The provider stream survived 47 seconds past its ``asyncio.wait_for``
budget. The mechanism: when ``wait_for`` cancels the inner streaming
coroutine, the ``CancelledError`` propagates DOWN the await chain.
Inside aiohttp's ``response.__aexit__`` / ``session.__aexit__`` the
cleanup path may attempt to drain remaining bytes from the socket.
If the server has gone silent, that drain blocks on the OS ``recv()``
syscall — which does not honour Python-level cancellation. The
coroutine can't propagate the ``CancelledError`` until ``recv()``
returns (e.g. on TCP keepalive timeout, many seconds later).

``asyncio.wait_for`` is a cooperative request, not a guarantee.

This module is the surgical primitive. The mechanism:

  * **Capture** the response's ``connection.transport`` reference at
    ``arm()`` time (after the response is available but before the
    streaming body is consumed).
  * **Schedule** a ``loop.call_later(deadline_s + grace_s, fire)``
    callback at ``__aenter__`` time — independent of any task, runs
    directly from the event loop.
  * **On clean exit**: ``__aexit__`` cancels the scheduled callback.
    No abort fires.
  * **On overrun**: the event loop fires ``_fire_abort`` synchronously
    at the scheduled instant. It calls ``transport.abort()`` — the
    canonical aiohttp Transport method that **surgically severs the
    socket file descriptor** without touching any other connection
    in the pool. The next ``recv()`` returns EOF immediately,
    unblocking every parent ``__aexit__`` in the chain within
    microseconds.

The shared-pool binding (operator):

    "If the ClientSession is shared across concurrent agents, calling
    session._connector.close() will violently terminate ALL active
    connections in the pool. You must ensure the severance is
    surgical to the specific stalled stream. Prioritize using
    response.connection.transport.abort() (or an equivalent targeted
    transport severance) to kill the specific file descriptor
    without nuking healthy concurrent streams in a shared pool."

This module HONOURS that binding structurally — the only abort call
in the entire file is ``transport.abort()`` on the captured per-stream
transport. ``connector.close()``, ``session.close()``, and any other
pool-wide operation is forbidden by the Slice 7b AST pin.

Design properties (operator-bound):

  * **Single primitive — no polling, no watchdog task, no asyncio.sleep
    loop.** ``loop.call_later`` is the canonical asyncio scheduler for
    a one-shot future callback.
  * **Master-flag default FALSE.** When ``JARVIS_BOUNDED_CANCELLATION
    _GUARD_ENABLED`` is off, the guard is a strict no-op — wrapping
    existing code is byte-identical to no guard at all. The flag
    flips to TRUE in Slice 7d (the wiring slice) only after the
    primitive itself has been merged + tested.
  * **NEVER raises.** Every internal step is wrapped; an aborted
    abort silently degrades to the legacy 47s ghost path.
  * **Pure-data plus event-loop integration.** No I/O, no
    ``os.environ`` reads (env knobs resolved once at construction).
    No new threads. No new HTTP client.
  * **Callback-shaped telemetry.** The guard takes an
    ``on_overrun`` callable that fires when abort triggers; Slice 7d
    wires this to ``StreamEventBroker.publish_cancellation_overrun``.
    The primitive itself is broker-free.

Slice 7b is the PRIMITIVE. Wiring into ``ClaudeProvider`` happens
in Slice 7d behind the same master flag (which flips to TRUE on
that PR — safety guardrail per §33.1)."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from typing import Any, Callable, Optional


logger = logging.getLogger("Ouroboros.BoundedCancellationGuard")


# ============================================================================
# Closed taxonomy — guard lifecycle states
# ============================================================================


class GuardState(str, enum.Enum):
    """Closed 4-value lifecycle state. Adding a 5th state requires
    bumping the AST pin in the paired test."""

    PENDING       = "pending"        # constructed, not yet entered
    ARMED         = "armed"          # __aenter__ done, deadline scheduled
    DISARMED      = "disarmed"       # clean exit, callback cancelled
    ABORTED       = "aborted"        # deadline fired, transport.abort() called


# ============================================================================
# Env knobs
# ============================================================================


_MASTER_FLAG_ENV: str = "JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED"
_GRACE_MS_ENV: str = "JARVIS_PROVIDER_CANCELLATION_GRACE_MS"

_DEFAULT_GRACE_MS: int = 500
_GRACE_MS_MIN: int = 50      # below this, false positives explode
_GRACE_MS_MAX: int = 5000    # above this, the bound stops being meaningful


def guard_enabled() -> bool:
    """Master gate. Default FALSE per §33.1 (substrate ships first;
    wiring slice 7d flips this TRUE). NEVER raises."""
    try:
        return os.environ.get(_MASTER_FLAG_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def _resolve_grace_ms() -> int:
    """Read the grace-period env knob with clamp. NEVER raises."""
    try:
        raw = os.environ.get(_GRACE_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_GRACE_MS
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_GRACE_MS
    return max(_GRACE_MS_MIN, min(_GRACE_MS_MAX, v))


# ============================================================================
# Transport extraction — defensive across aiohttp response shapes
# ============================================================================


def _extract_transport(response_or_transport: Any) -> Any:
    """Return the per-FD transport reference from an aiohttp
    ``ClientResponse`` (preferred) or a raw transport object
    (defensive). Returns None if neither shape applies.

    The aiohttp ClientResponse exposes ``.connection`` after the
    response is open; ``connection.transport`` is the per-stream
    transport. After ``release()`` the connection becomes None.
    NEVER raises — failure-soft."""
    try:
        # ClientResponse path — preferred.
        connection = getattr(response_or_transport, "connection", None)
        if connection is not None:
            transport = getattr(connection, "transport", None)
            if transport is not None and hasattr(transport, "abort"):
                return transport
        # Raw transport / TransportLike duck path — defensive.
        if hasattr(response_or_transport, "abort") and hasattr(
            response_or_transport, "is_closing"
        ):
            return response_or_transport
    except Exception:  # noqa: BLE001
        pass
    return None


# ============================================================================
# Primitive — BoundedCancellationGuard
# ============================================================================


class BoundedCancellationGuard:
    """Surgical-severance async context manager for aiohttp streams.

    Use:

        guard = BoundedCancellationGuard(
            deadline_s=stream_timeout_s,
            grace_ms=500,
            on_overrun=publish_cancellation_overrun,
        )
        async with guard:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, ...) as response:
                    guard.arm(response)
                    async for chunk in response.content:
                        ...

    If the stream completes normally before ``deadline_s + grace_ms``,
    the scheduled abort is cancelled and the guard exits as DISARMED.

    If the stream is still ongoing at ``deadline_s + grace_ms`` (the
    47-second ghost scenario), the event loop fires ``_fire_abort``
    synchronously, which calls ``response.connection.transport.abort()``.
    The socket FD is gone instantly; any pending ``recv()`` returns
    EOF; aiohttp's ``__aexit__`` chains unwind within microseconds.
    The guard exits as ABORTED.

    Master flag ``JARVIS_BOUNDED_CANCELLATION_GUARD_ENABLED`` default
    FALSE — when off, the guard is a strict no-op (no scheduling,
    no arming, no abort). Slice 7d's wiring flips this TRUE."""

    def __init__(
        self,
        *,
        deadline_s: float,
        grace_ms: Optional[int] = None,
        on_overrun: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._deadline_s: float = float(deadline_s)
        self._grace_ms: int = (
            grace_ms if grace_ms is not None else _resolve_grace_ms()
        )
        self._on_overrun = on_overrun
        self._transport: Any = None
        self._abort_handle: Optional[asyncio.TimerHandle] = None
        self._started_at: Optional[float] = None
        self._state: GuardState = GuardState.PENDING
        # Cached at construction so the contract is sticky for this
        # guard instance — flipping the env mid-stream does not
        # mutate behaviour.
        self._master_enabled: bool = guard_enabled()

    # ---- public introspection ----

    @property
    def state(self) -> GuardState:
        return self._state

    @property
    def is_armed(self) -> bool:
        return self._state == GuardState.ARMED

    @property
    def aborted(self) -> bool:
        return self._state == GuardState.ABORTED

    @property
    def grace_ms(self) -> int:
        return self._grace_ms

    @property
    def deadline_s(self) -> float:
        return self._deadline_s

    @property
    def master_enabled(self) -> bool:
        return self._master_enabled

    # ---- arm — capture transport, schedule abort callback ----

    def arm(self, response_or_transport: Any) -> bool:
        """Capture the per-stream transport for potential surgical
        abort. Returns True iff a transport was successfully captured.

        Idempotent — calling arm() twice on the same guard is safe;
        the second call is a no-op. NEVER raises."""
        if not self._master_enabled:
            return False
        if self._transport is not None:
            return True  # already armed — idempotent
        transport = _extract_transport(response_or_transport)
        if transport is None:
            logger.debug(
                "[BoundedCancellationGuard] arm(): could not extract "
                "transport from %r — abort callback will be a no-op",
                type(response_or_transport).__name__,
            )
            return False
        self._transport = transport
        return True

    # ---- context-manager surface ----

    async def __aenter__(self) -> "BoundedCancellationGuard":
        if not self._master_enabled:
            # Strict no-op when master flag OFF — byte-identical to
            # not wrapping the call at all.
            return self
        self._started_at = time.monotonic()
        # Schedule the surgical-abort callback at
        # deadline_s + grace. loop.call_later is the canonical
        # asyncio primitive for one-shot future callbacks — runs
        # directly from the event loop, independent of any task,
        # no polling, no watchdog overhead.
        try:
            loop = asyncio.get_running_loop()
            self._abort_handle = loop.call_later(
                self._deadline_s + (self._grace_ms / 1000.0),
                self._fire_abort,
            )
            self._state = GuardState.ARMED
        except RuntimeError:
            # No running loop — defensive. Should never happen in
            # production (we're inside an async-with). Stay PENDING.
            logger.debug(
                "[BoundedCancellationGuard] __aenter__: no running "
                "loop — guard degraded to no-op",
            )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> bool:
        # Cancel the scheduled abort regardless of how we exit. If
        # the abort already fired (state == ABORTED), TimerHandle.
        # cancel() is a no-op.
        if self._abort_handle is not None:
            try:
                self._abort_handle.cancel()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[BoundedCancellationGuard] __aexit__: abort "
                    "handle cancel raised — ignoring",
                )
            self._abort_handle = None
        # Only mark DISARMED if we were ARMED + the abort didn't
        # already fire. ABORTED is terminal.
        if self._state == GuardState.ARMED:
            self._state = GuardState.DISARMED
        # Never suppress.
        return False

    # ---- the surgical-severance primitive itself ----

    def _fire_abort(self) -> None:
        """Synchronously sever the captured transport. Called from
        the event loop via ``loop.call_later`` when the deadline +
        grace expires before the stream completes.

        The abort is **surgical** — ``transport.abort()`` is the
        per-FD aiohttp Transport method (NOT
        ``connector.close()`` which would nuke every active
        connection in a shared pool). The operator's
        shared-pool binding is enforced by the Slice 7b AST pin
        in the paired test.

        NEVER raises — defensive at every step. A failed abort
        silently degrades to the legacy ghost-cancellation path."""
        # Guard against double-fire (e.g. __aexit__ raced with the
        # callback). TimerHandle.cancel() makes this unlikely but
        # not impossible.
        if self._state != GuardState.ARMED:
            return
        # No transport captured — nothing to abort. The deadline
        # still expired but there's nothing surgical to do.
        if self._transport is None:
            self._state = GuardState.ABORTED
            return
        try:
            # Pre-check: aiohttp may have closed the transport on
            # its own (e.g. a clean server close arrived right at
            # the deadline). is_closing() is the canonical
            # stdlib-Transport check.
            if hasattr(self._transport, "is_closing"):
                try:
                    if self._transport.is_closing():
                        # Already gone — record state, skip abort.
                        self._state = GuardState.ABORTED
                        return
                except Exception:  # noqa: BLE001
                    pass
            # SURGICAL SEVERANCE.
            #
            # transport.abort() closes the file descriptor at the
            # OS level WITHOUT a graceful shutdown. Any pending
            # recv() in aiohttp's stream-reader returns EOF
            # immediately. The cancellation propagates through the
            # entire __aexit__ chain within microseconds.
            #
            # This is a SYNC method on asyncio.Transport. We are
            # already running on the event loop (call_later
            # callback) so this executes inline.
            self._transport.abort()
            self._state = GuardState.ABORTED
            overrun_s = 0.0
            if self._started_at is not None:
                overrun_s = (
                    time.monotonic() - self._started_at - self._deadline_s
                )
            logger.info(
                "[BoundedCancellationGuard] surgical abort fired — "
                "overrun=%.3fs grace=%dms (transport=%s)",
                overrun_s, self._grace_ms,
                type(self._transport).__name__,
            )
            # Fire the overrun callback (Slice 7d wires this to the
            # StreamEventBroker SSE publish). Callback is sync —
            # caller may schedule async work via create_task if
            # needed.
            if self._on_overrun is not None:
                try:
                    self._on_overrun(overrun_s)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[BoundedCancellationGuard] on_overrun "
                        "callback raised — ignoring",
                    )
        except Exception:  # noqa: BLE001
            # Whatever went wrong — log and stay quiet. The legacy
            # 47-second ghost path is the fallback.
            logger.debug(
                "[BoundedCancellationGuard] _fire_abort: abort "
                "raised — degrading to legacy path",
                exc_info=True,
            )
            self._state = GuardState.ABORTED


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "BoundedCancellationGuard",
    "GuardState",
    "guard_enabled",
]
