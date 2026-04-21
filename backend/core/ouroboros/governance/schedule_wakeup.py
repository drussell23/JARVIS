"""
WakeupController — Slice 3 of the Scheduled Wake-ups arc.
==========================================================

CC-parity for the :tool:`ScheduleWakeup` primitive: a dynamic,
one-shot "wake me up in N seconds with this payload." When the
wake-up fires, a registered handler is invoked with the payload;
callers awaiting the returned :class:`asyncio.Future` get a
structured :class:`WakeupOutcome`.

Distinct from the :class:`ScheduledJob` primitive (Slice 2) because:

* **Dynamic** — scheduled at runtime, not declared ahead of time.
* **One-shot** — fires exactly once, then its record is terminal.
* **Delay-based** — ``delay_seconds`` from now, not a cron expression
  (the callee doesn't need to express a repeating cadence — this is
  for "come back to me in 5 minutes").
* **Cancellable** — the caller (or a REPL command) can cancel a
  pending wake-up before its deadline.

Mirrors the shape of :class:`PlanApprovalController` (Problem #7) and
:class:`InlinePromptController` (Permission arc Slice 2) so the three
primitives share a consistent mental model.

Manifesto alignment
-------------------

* §1 — scheduled-by-orchestrator, not by the model. The bounded
  payload is data; authority to act on it lives in the handler the
  orchestrator pre-registered via :class:`JobRegistry`. A future slice
  could expose a ``schedule_wakeup`` Venom tool, but the scheduling
  mechanism stays gated by its own env flag.
* §3 — the Future blocks only the awaiting coroutine; the event loop
  continues running.
* §7 — fail-closed. Lost timer / broken handler → outcome is
  ``FIRED`` with ``ok=False`` and a structured reason. Capacity or
  delay-bound violations raise at schedule time.
* §8 — every transition emits a ``[WakeupController]`` INFO log;
  Slice 4 bridges these to SSE.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Callable, Dict, List, Mapping, Optional, Tuple,
)

logger = logging.getLogger("Ouroboros.ScheduleWakeup")


WAKEUP_CONTROLLER_SCHEMA_VERSION: str = "schedule_wakeup.v1"


# ---------------------------------------------------------------------------
# Env knobs — bounds mirror CC's ScheduleWakeup guidance (60s–3600s)
# ---------------------------------------------------------------------------


def _default_min_delay_s() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_WAKEUP_MIN_DELAY_S", "60",
        )))
    except (TypeError, ValueError):
        return 60.0


def _default_max_delay_s() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_WAKEUP_MAX_DELAY_S", "3600",
        )))
    except (TypeError, ValueError):
        return 3600.0


def _default_max_pending() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_WAKEUP_MAX_PENDING", "32",
        )))
    except (TypeError, ValueError):
        return 32


def _reason_max_len() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_WAKEUP_REASON_MAX_LEN", "500",
        )))
    except (TypeError, ValueError):
        return 500


# ---------------------------------------------------------------------------
# State + outcome
# ---------------------------------------------------------------------------


STATE_PENDING = "pending"
STATE_FIRED = "fired"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


_TERMINAL_STATES = frozenset({STATE_FIRED, STATE_CANCELLED, STATE_FAILED})


class WakeupError(Exception):
    """Raised on illegal wake-up operations."""


class WakeupCapacityError(WakeupError):
    """Raised when the pending cap is reached."""


class WakeupStateError(WakeupError):
    """Illegal state transition."""


class WakeupDelayError(WakeupError):
    """delay_seconds out of allowed range."""


# ---------------------------------------------------------------------------
# Wakeup request + outcome records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeupRequest:
    """Operator/orchestrator intent for a scheduled wake-up.

    ``handler_name`` resolves through :class:`JobRegistry`; ``reason``
    is a short human note surfaced in logs + SSE for audit. ``payload``
    is opaque data passed to the handler at fire time.
    """

    wakeup_id: str
    handler_name: str
    delay_seconds: float
    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str = "orchestrator"   # matches HandlerSource.value convention
    created_at_ts: float = 0.0
    created_at_iso: str = ""
    fires_at_ts: float = 0.0
    schema_version: str = WAKEUP_CONTROLLER_SCHEMA_VERSION


@dataclass(frozen=True)
class WakeupOutcome:
    """Result delivered via the awaited :class:`asyncio.Future`.

    ``state`` is one of ``STATE_FIRED`` / ``STATE_CANCELLED`` /
    ``STATE_FAILED``; ``ok`` is True only when ``state=FIRED`` AND the
    handler returned without raising. ``handler_result`` is the value
    the handler returned (or None if it raised); ``error`` is the
    exception message if any.
    """

    wakeup_id: str
    state: str
    ok: bool
    scheduled_delay_s: float
    actual_delay_s: float
    fired_at_iso: str = ""
    handler_result: Optional[Any] = None
    error: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Internal pending record
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    request: WakeupRequest
    future: "asyncio.Future[WakeupOutcome]"
    state: str = STATE_PENDING
    timer_task: Optional["asyncio.Task[None]"] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# WakeupController
# ---------------------------------------------------------------------------


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(
        microsecond=0,
    ).isoformat()


class WakeupController:
    """Per-process controller for dynamic one-shot wake-ups.

    Handler resolution is delegated to a caller-supplied lookup
    (typically :func:`JobRegistry.get_handler`); constructing a
    :class:`WakeupController` without a resolver still works for
    testing, but any fire will return ``state=STATE_FAILED`` with
    ``error="no_handler_resolver"``.
    """

    def __init__(
        self,
        *,
        handler_resolver: Optional[Callable[[str], Any]] = None,
        min_delay_s: Optional[float] = None,
        max_delay_s: Optional[float] = None,
        max_pending: Optional[int] = None,
    ) -> None:
        self._resolver = handler_resolver
        self._min_delay = (
            min_delay_s if min_delay_s is not None else _default_min_delay_s()
        )
        self._max_delay = (
            max_delay_s if max_delay_s is not None else _default_max_delay_s()
        )
        if self._min_delay > self._max_delay:
            raise WakeupError(
                f"min_delay_s {self._min_delay} > max_delay_s {self._max_delay}"
            )
        self._cap = max_pending or _default_max_pending()
        self._lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._history: List[Dict[str, Any]] = []
        self._history_max = 256
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- public API ------------------------------------------------------

    def schedule(
        self,
        *,
        handler_name: str,
        delay_seconds: float,
        reason: str = "",
        payload: Optional[Mapping[str, Any]] = None,
        source: str = "orchestrator",
    ) -> "asyncio.Future[WakeupOutcome]":
        """Schedule a one-shot wake-up and return a Future.

        Raises :class:`WakeupDelayError` on out-of-range delay,
        :class:`WakeupCapacityError` when the pending cap is reached,
        :class:`WakeupError` on empty ``handler_name``.
        """
        if not isinstance(handler_name, str) or not handler_name.strip():
            raise WakeupError("handler_name must be non-empty")
        delay = float(delay_seconds)
        if delay < self._min_delay or delay > self._max_delay:
            raise WakeupDelayError(
                f"delay_seconds {delay} outside "
                f"[{self._min_delay}, {self._max_delay}]"
            )
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[WakeupOutcome]" = loop.create_future()
        now = time.time()
        wakeup_id = f"wk-{uuid.uuid4().hex[:10]}"
        trimmed_reason = (reason or "").strip()[: _reason_max_len()]
        request = WakeupRequest(
            wakeup_id=wakeup_id,
            handler_name=handler_name,
            delay_seconds=delay,
            reason=trimmed_reason,
            payload=dict(payload or {}),
            source=source,
            created_at_ts=now,
            created_at_iso=_utc_iso(now),
            fires_at_ts=now + delay,
        )

        with self._lock:
            non_terminal = sum(
                1 for p in self._pending.values() if not p.is_terminal
            )
            if non_terminal >= self._cap:
                raise WakeupCapacityError(
                    f"pending cap {self._cap} reached",
                )
            pending = _Pending(request=request, future=future)
            self._pending[wakeup_id] = pending

        pending.timer_task = loop.create_task(
            self._run_timer(wakeup_id, delay),
            name=f"wakeup-timer-{wakeup_id}",
        )
        logger.info(
            "[WakeupController] scheduled id=%s handler=%s delay_s=%.1f "
            "fires_at=%s reason=%r",
            wakeup_id, handler_name, delay,
            request.created_at_iso, trimmed_reason[:80],
        )
        self._fire("wakeup_scheduled", self.project(pending))
        return future

    def cancel(
        self, wakeup_id: str, *, reason: str = "",
    ) -> Optional[WakeupOutcome]:
        """Cancel a pending wake-up. Returns the outcome or None if unknown."""
        return self._resolve(
            wakeup_id,
            state=STATE_CANCELLED,
            ok=False,
            error=(reason or "cancelled")[: _reason_max_len()],
        )

    async def fire_now(self, wakeup_id: str) -> Optional[WakeupOutcome]:
        """Force an immediate fire (test/admin path).

        Returns None if the id is unknown; otherwise the outcome the
        Future will also resolve to.
        """
        with self._lock:
            pending = self._pending.get(wakeup_id)
            if pending is None:
                return None
            if pending.is_terminal:
                return None
            if pending.timer_task is not None and not pending.timer_task.done():
                pending.timer_task.cancel()
        return await self._do_fire(wakeup_id)

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for p in self._pending.values() if not p.is_terminal
            )

    def pending_ids(self) -> List[str]:
        with self._lock:
            return [
                wid for wid, p in self._pending.items()
                if not p.is_terminal
            ]

    def snapshot(self, wakeup_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p = self._pending.get(wakeup_id)
            return None if p is None else self.project(p)

    def snapshot_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self.project(p) for p in self._pending.values()]

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    # --- listeners -------------------------------------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, projection: Dict[str, Any]) -> None:
        payload = {"event_type": event_type, "projection": projection}
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[WakeupController] listener exception on %s: %s",
                    event_type, exc,
                )

    # --- timer / dispatch ------------------------------------------------

    async def _run_timer(self, wakeup_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        with self._lock:
            p = self._pending.get(wakeup_id)
            if p is None or p.is_terminal:
                return
        await self._do_fire(wakeup_id)

    async def _do_fire(self, wakeup_id: str) -> Optional[WakeupOutcome]:
        with self._lock:
            p = self._pending.get(wakeup_id)
            if p is None or p.is_terminal:
                return None

        handler = None
        error: Optional[str] = None
        if self._resolver is None:
            error = "no_handler_resolver"
        else:
            try:
                handler = self._resolver(p.request.handler_name)
            except Exception as exc:  # noqa: BLE001
                error = f"resolver_raise:{type(exc).__name__}:{exc}"
                handler = None
            if handler is None and error is None:
                error = f"no_handler:{p.request.handler_name}"

        result: Any = None
        if handler is not None:
            try:
                coro = handler(p.request, dict(p.request.payload))
                if asyncio.iscoroutine(coro):
                    result = await coro
                else:
                    result = coro
            except Exception as exc:  # noqa: BLE001
                error = f"handler_raise:{type(exc).__name__}:{exc}"

        state = STATE_FIRED if error is None else STATE_FAILED
        return self._resolve(
            wakeup_id,
            state=state,
            ok=(error is None),
            handler_result=result,
            error=error,
        )

    def _resolve(
        self,
        wakeup_id: str,
        *,
        state: str,
        ok: bool,
        handler_result: Any = None,
        error: Optional[str] = None,
    ) -> Optional[WakeupOutcome]:
        with self._lock:
            p = self._pending.get(wakeup_id)
            if p is None:
                return None
            if p.is_terminal:
                return None
            now = time.time()
            scheduled_delay = p.request.delay_seconds
            actual_delay = now - p.request.created_at_ts
            outcome = WakeupOutcome(
                wakeup_id=wakeup_id,
                state=state,
                ok=ok,
                scheduled_delay_s=scheduled_delay,
                actual_delay_s=actual_delay,
                fired_at_iso=_utc_iso(now),
                handler_result=handler_result,
                error=error,
                reason=p.request.reason,
            )
            p.state = state
            if p.timer_task is not None and not p.timer_task.done():
                try:
                    p.timer_task.cancel()
                except RuntimeError:
                    pass
            self._history.append({
                "wakeup_id": wakeup_id, "state": state, "ok": ok,
                "error": error, "actual_delay_s": actual_delay,
                "resolved_ts": now,
            })
            if len(self._history) > self._history_max:
                self._history.pop(0)
            future = p.future

        event_type = {
            STATE_FIRED: "wakeup_fired",
            STATE_CANCELLED: "wakeup_cancelled",
            STATE_FAILED: "wakeup_failed",
        }.get(state, "wakeup_resolved")
        logger.info(
            "[WakeupController] %s id=%s ok=%s actual_delay_s=%.1f "
            "error=%.200s",
            event_type, wakeup_id, ok, actual_delay, error or "",
        )
        if not future.done():
            _loop = future.get_loop()
            try:
                _loop.call_soon_threadsafe(future.set_result, outcome)
            except RuntimeError:
                try:
                    future.set_result(outcome)
                except Exception:  # noqa: BLE001
                    pass
        self._fire(event_type, self.project(p))
        return outcome

    # --- projection ------------------------------------------------------

    @staticmethod
    def project(pending: _Pending) -> Dict[str, Any]:
        """Sanitised dict safe for SSE / REPL display.

        Payload VALUES are deliberately excluded — operator data lives
        in the handler invocation, not on the wire.
        """
        r = pending.request
        return {
            "schema_version": r.schema_version,
            "wakeup_id": r.wakeup_id,
            "handler_name": r.handler_name,
            "delay_seconds": r.delay_seconds,
            "fires_at_iso": _utc_iso(r.fires_at_ts),
            "fires_at_ts": r.fires_at_ts,
            "created_at_iso": r.created_at_iso,
            "source": r.source,
            "reason": r.reason,
            "state": pending.state,
            "payload_keys": sorted(dict(r.payload).keys()),
        }

    # --- test helpers ----------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            for p in self._pending.values():
                if p.timer_task is not None and not p.timer_task.done():
                    try:
                        p.timer_task.cancel()
                    except RuntimeError:
                        pass
                if not p.future.done():
                    try:
                        p.future.cancel()
                    except Exception:  # noqa: BLE001
                        pass
            self._pending.clear()
            self._history.clear()
            self._listeners.clear()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_controller: Optional[WakeupController] = None
_controller_lock = threading.Lock()


def get_default_wakeup_controller() -> WakeupController:
    """Return the process-wide :class:`WakeupController`.

    On first construction, wires its handler resolver to the default
    :func:`get_default_job_registry`.
    """
    global _default_controller
    with _controller_lock:
        if _default_controller is None:
            from backend.core.ouroboros.governance.schedule_job import (
                get_default_job_registry,
            )
            reg = get_default_job_registry()
            _default_controller = WakeupController(
                handler_resolver=reg.get_handler,
            )
        return _default_controller


def reset_default_wakeup_controller() -> None:
    global _default_controller
    with _controller_lock:
        if _default_controller is not None:
            _default_controller.reset()
        _default_controller = None


__all__ = [
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_FIRED",
    "STATE_PENDING",
    "WAKEUP_CONTROLLER_SCHEMA_VERSION",
    "WakeupCapacityError",
    "WakeupController",
    "WakeupDelayError",
    "WakeupError",
    "WakeupOutcome",
    "WakeupRequest",
    "WakeupStateError",
    "get_default_wakeup_controller",
    "reset_default_wakeup_controller",
]

_ = Tuple  # silence unused-import guard
