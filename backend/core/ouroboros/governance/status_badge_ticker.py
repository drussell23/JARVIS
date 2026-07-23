"""Live Status-Line Badge — an async, provider-agnostic, width-responsive ticker.

Keeps provider resilience state persistently visible in the REPL bottom toolbar
WITHOUT blocking input or shattering the layout:

  * **Event-fed, not polled** — the cache is updated from the SAME
    ``provider_state_changed`` broker payloads the ``/provider`` verb's telemetry
    derives from (debounced: the watcher only emits on a transition). No separate
    SQLite polling loop for the bar.
  * **Async-decoupled** — a tick coroutine advances the ticker + invalidates the
    prompt on its OWN task and explicitly yields to the event loop; the actual
    render is a fast pure format (what ``bottom_toolbar`` calls synchronously).
  * **Multi-provider rotating ticker** — if several providers are registered
    (DW, J-Prime, …) the badge CYCLES one provider per tick rather than
    overflowing the width.
  * **Resize-resilient** — the renderer takes the live width and aggressively
    truncates with an ellipsis rather than letting text-wrap break the TUI.

No hardcoded "DoubleWord"; provider names come from the telemetry. Pure format +
in-memory cache; touches no model-selection path (Fable never flagged). Never
raises.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable, Dict, List, Optional

_SHORT = {"doubleword": "dw", "gcp-jprime": "jprime", "jprime": "jprime",
          "claude-api": "claude", "claude": "claude"}


def _short_name(provider: str) -> str:
    p = (provider or "").lower()
    return _SHORT.get(p, p[:8] if p else "?")


def _truncate(s: str, width: Optional[int]) -> str:
    """Truncate to *width* with a trailing ellipsis — never wrap. Never raises."""
    if width is None or width <= 0:
        return s
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def render_badge(
    providers: Dict[str, dict], index: int, width: Optional[int],
) -> str:
    """Pure render of ONE provider's compact badge (the ticker's current index),
    truncated to *width*. Empty string when no providers. Never raises."""
    try:
        if not providers:
            return ""
        names: List[str] = sorted(providers.keys())
        n = len(names)
        prov = names[index % n]
        snap = providers.get(prov, {}) or {}
        state = str(snap.get("state", "?"))
        parts = [f"{_short_name(prov)}:●{state}"]
        ji = snap.get("jitter")
        if ji is not None:
            parts.append(f"j{ji}")
        slope = snap.get("ttft_slope")
        if isinstance(slope, (int, float)):
            parts.append("▼" if slope < 0 else "▲")
        ttr = snap.get("forecast_ttr")
        if isinstance(ttr, (int, float)) and state != "HEALTHY":
            parts.append(f"~{int(ttr)}s")
        if n > 1:
            parts.append(f"({(index % n) + 1}/{n})")
        return _truncate(" ".join(parts), width)
    except Exception:  # noqa: BLE001
        return ""


class StatusBadgeTicker:
    """Thread-safe provider cache + rotating index + async tick loop."""

    def __init__(self, *, invalidate: Optional[Callable[[], None]] = None) -> None:
        self._providers: Dict[str, dict] = {}
        self._index = 0
        self._invalidate = invalidate
        self._lock = threading.Lock()

    # -- cache (event-fed) ----------------------------------------------

    def update(self, provider: str, snapshot: dict) -> None:
        if not provider:
            return
        with self._lock:
            self._providers[provider] = dict(snapshot or {})

    def on_provider_event(self, payload: dict) -> None:
        """Consume a ``provider_state_changed`` broker payload. Never raises."""
        try:
            prov = (payload or {}).get("provider")
            if prov:
                self.update(prov, payload)
        except Exception:  # noqa: BLE001
            pass

    def provider_count(self) -> int:
        with self._lock:
            return len(self._providers)

    # -- render (fast, sync — what bottom_toolbar calls) ----------------

    def render(self, width: Optional[int]) -> str:
        with self._lock:
            return render_badge(self._providers, self._index, width)

    # -- async cadence (decoupled; explicitly yields) -------------------

    async def tick(self) -> None:
        """Advance the rotating ticker, invalidate the prompt, and EXPLICITLY
        yield to the event loop so REPL input is never blocked. Never raises."""
        with self._lock:
            if self._providers:
                self._index += 1
        if self._invalidate is not None:
            try:
                self._invalidate()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0)   # cooperative yield — proof it won't block input

    async def run(
        self, interval_s: float = 4.0, *,
        sleep_fn: Optional[Callable[[float], "asyncio.Future"]] = None,
        max_ticks: Optional[int] = None,
    ) -> None:
        sleep = sleep_fn or asyncio.sleep
        ticks = 0
        while True:
            try:
                await sleep(interval_s)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return


_default: Optional[StatusBadgeTicker] = None
_default_lock = threading.Lock()


def get_default_ticker() -> StatusBadgeTicker:
    """Process-local singleton shared by the broker listener (writer), the tick
    task, and the bottom-toolbar segment (reader)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = StatusBadgeTicker()
    return _default


def set_invalidate(invalidate: Optional[Callable[[], None]]) -> None:
    get_default_ticker()._invalidate = invalidate  # noqa: SLF001 — module seam


__all__ = [
    "StatusBadgeTicker",
    "get_default_ticker",
    "render_badge",
    "set_invalidate",
]
