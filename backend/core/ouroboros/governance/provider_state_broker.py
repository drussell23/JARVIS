"""Provider-state → broker bridge — the Sentinel's SQLite signal onto the bus.

The headless Sentinel writes provider_state to SQLite (Single-Writer, no bus
access). This bridge runs IN the main O+V process: it polls that SQLite row and,
on a DEGRADED↔HEALTHY transition, publishes ``provider_state_changed`` on the
``StreamEventBroker`` with the full resilience snapshot. That single emission
fans out to BOTH the in-process TUI breadcrumb listener AND the
``/observability/stream`` SSE (→ the JARVIS HUD) — so operators see the recovery
the instant the Sentinel confirms it, with no polling in the UI.

Read-only over SQLite; the only side effect is a best-effort broker publish.
Never raises on the telemetry path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("Ouroboros.ProviderStateBroker")

EmitFn = Callable[[dict], Any]


def build_provider_snapshot(conn, *, provider: str = "doubleword",
                            now: Optional[float] = None) -> dict:
    """Compose the resilience snapshot from the telemetry conn — the SAME source
    the ``/provider`` dashboard renders. Never raises."""
    snap: dict = {"provider": provider, "state": "UNKNOWN"}
    try:
        from backend.core.ouroboros.governance.provider_state import get_provider_state
        from backend.core.ouroboros.governance.provider_jitter import (
            jitter_index, required_consecutive_passes,
        )
        from backend.core.ouroboros.governance.dw_outage_forecaster import (
            forecast_ttr, ttft_slope,
        )
    except Exception:  # noqa: BLE001
        return snap
    if conn is None:
        return snap
    try:
        st = get_provider_state(conn, provider) or {}
        snap.update({
            "state": st.get("state", "UNKNOWN"),
            "reason": st.get("reason", ""),
            "updated_ts": st.get("updated_ts"),
            "jitter": jitter_index(conn, provider, now=now),
            "threshold": required_consecutive_passes(conn, provider, now=now),
            "ttft_slope": ttft_slope(conn, provider, now=now),
            "forecast_ttr": forecast_ttr(conn),
        })
    except Exception:  # noqa: BLE001
        pass
    return snap


def format_provider_breadcrumb(payload: dict) -> str:
    """The calm one-line breadcrumb the TUI prints on a transition. Plain text
    (the listener adds color). Never raises."""
    try:
        prov = payload.get("provider", "doubleword")
        state = payload.get("state", "?")
        if state == "HEALTHY":
            reason = payload.get("reason", "") or ""
            tail = f" — {reason}" if reason else " — recovered, stable"
            return f"provider  {prov} ● HEALTHY ✓{tail}"
        ji = payload.get("jitter", "?")
        slope = payload.get("ttft_slope")
        grad = ""
        if isinstance(slope, (int, float)):
            grad = ", ΔTTFT stabilizing" if slope < 0 else ", ΔTTFT worsening"
        thr = payload.get("threshold")
        thr_s = f", need {thr} stable passes" if thr else ""
        return f"provider  {prov} ● {state} → jitter {ji}{grad}{thr_s}"
    except Exception:  # noqa: BLE001
        return "provider state changed"


class ProviderStateWatcher:
    """Polls the provider_state SQLite row and emits a snapshot on each
    DEGRADED↔HEALTHY transition. Injectable clock/emit for testing."""

    def __init__(
        self, conn, emit_fn: EmitFn, *, provider: str = "doubleword",
        now_fn: Optional[Callable[[], float]] = None, poll_s: float = 5.0,
        emit_on_first: bool = False,
    ) -> None:
        self._conn = conn
        self._emit = emit_fn
        self._provider = provider
        self._now = now_fn or time.time
        self._poll_s = poll_s
        self._emit_on_first = emit_on_first
        self._last_state: Optional[str] = None

    async def poll_once(self) -> Optional[dict]:
        """One read. Emits + returns the snapshot on a state change, else None."""
        snap = build_provider_snapshot(self._conn, provider=self._provider, now=self._now())
        state = snap.get("state")
        if state == self._last_state:
            return None
        prev = self._last_state
        self._last_state = state
        if prev is None and not self._emit_on_first:
            return None                     # first observation — establish baseline
        snap["previous_state"] = prev
        try:
            self._emit(snap)
        except Exception:  # noqa: BLE001 — a bad emit never kills the watcher
            logger.debug("[ProviderStateWatcher] emit failed", exc_info=True)
        return snap

    async def run(
        self, *, sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
        max_polls: Optional[int] = None,
    ) -> None:
        sleep = sleep_fn or asyncio.sleep
        polls = 0
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            await sleep(self._poll_s)


def start_provider_state_watcher(
    *, provider: str = "doubleword", poll_s: float = 5.0,
) -> Optional["asyncio.Task"]:
    """Production wiring: open the telemetry DB, publish transitions on the
    default broker via ``publish_provider_state_changed``. Returns the task, or
    None if the substrate/broker is unavailable. Never raises."""
    try:
        from backend.core.ouroboros.governance.dw_outage_forecaster import open_forecast_db
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_provider_state_changed,
        )
        conn = open_forecast_db()
        if conn is None:
            return None
        watcher = ProviderStateWatcher(
            conn, lambda snap: publish_provider_state_changed(snap),
            provider=provider, poll_s=poll_s,
        )
        return asyncio.ensure_future(watcher.run())
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "ProviderStateWatcher",
    "build_provider_snapshot",
    "format_provider_breadcrumb",
    "start_provider_state_watcher",
]
