"""Governance → SSE bridge — the O+V ↔ JARVIS-Apple wire.

Operator authorization 2026-07-19. Honest finding that motivated this
module: O+V's autonomous activity and JARVIS-Apple were NOT connected.

  * O+V autonomy events flow  EventEmitter.emit()
    → ``_bridge_to_spine`` → ``TrinityEventBus.publish_raw("autonomy.*")``.
  * JARVIS-Apple's native SSE reads the OTHER bus: the core
    ``EventStream`` (``broadcast_event`` → ``sse_stream`` →
    ``DeviceStreamManager.device_stream`` → the Swift client).

Nothing forwarded between the two buses, so the phone could reach the
backend but never *see O+V working*. This module is the missing
forwarder — and ONLY the forwarder (mandate 3, DRY): it subscribes to
the EXISTING TrinityEventBus autonomy topics and republishes each onto
the EXISTING EventStream ``governance`` channel. No new transport, no
duplicated event schema, no second SSE server.

Design invariants:
  * **Fault-isolated** — a forward failure never propagates back into
    the O+V loop that emitted the event (the source bus already treats
    the bridge as non-fatal; we honor that from this side too).
  * **No feedback loop** — we read TrinityEventBus and write EventStream;
    we never write back to TrinityEventBus, so an event cannot echo.
  * **Self-protecting under load** — the ``governance`` channel is
    ``LATEST_WINS``; a burst of high-volume autonomy events coalesces at
    the EventStream drop policy, and ``broadcast_event`` short-circuits
    to 0 when no native client is attached (zero cost when nobody's
    watching).
  * **Idempotent install** — ``install_governance_sse_bridge`` is safe
    to call from any boot path (supervisor, trinity); a second call is a
    no-op while the first bridge is live.
  * **NEVER raises** out of any public entry point.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Jarvis.GovernanceSSEBridge")

#: TrinityEventBus topic patterns that represent O+V activity worth
#: surfacing to the operator's phone. Env-overridable (comma-separated)
#: so the surface can widen without a code change (no hardcoding).
_DEFAULT_PATTERNS = "autonomy.#,governance.#,ouroboros.#"

#: EventStream channel the native client already subscribes to for O+V
#: lifecycle. Kept as a name (not a literal sprinkled around) so a
#: rename is one edit.
_SSE_CHANNEL = "governance"


def _patterns() -> List[str]:
    raw = os.environ.get("JARVIS_GOVERNANCE_SSE_PATTERNS", _DEFAULT_PATTERNS)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _enabled() -> bool:
    return os.environ.get(
        "JARVIS_GOVERNANCE_SSE_BRIDGE_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


class GovernanceSSEBridge:
    """Forwards O+V TrinityEventBus events onto the EventStream governance
    channel. One instance per backend process. Every method NEVER
    raises."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._sub_ids: List[str] = []
        self._bus: Any = None
        self._installed = False
        self.stats: Dict[str, int] = {
            "forwarded": 0, "dropped_no_stream": 0, "forward_errors": 0,
        }

    @property
    def installed(self) -> bool:
        return self._installed

    async def install(self) -> bool:
        """Subscribe to the O+V topics on TrinityEventBus. Returns True
        when the bridge is live (both buses present). Idempotent; NEVER
        raises."""
        if self._installed:
            return True
        if not _enabled():
            logger.debug("[GovernanceSSE] disabled by env")
            return False
        try:
            from backend.core.trinity_event_bus import get_event_bus_if_exists
            bus = get_event_bus_if_exists()
            if bus is None:
                # Backend not fully awake yet — caller may retry later.
                return False
            self._bus = bus
            for pattern in _patterns():
                try:
                    sub_id = await bus.subscribe(pattern, self._on_event)
                    self._sub_ids.append(sub_id)
                except Exception:  # noqa: BLE001 — one bad pattern isn't fatal
                    logger.debug("[GovernanceSSE] subscribe failed pattern=%s",
                                 pattern, exc_info=True)
            self._installed = bool(self._sub_ids)
            if self._installed:
                logger.info(
                    "[GovernanceSSE] live — O+V→JARVIS-Apple wire up "
                    "(%d topic patterns)", len(self._sub_ids),
                )
            return self._installed
        except Exception:  # noqa: BLE001
            logger.debug("[GovernanceSSE] install degraded", exc_info=True)
            return False

    async def _on_event(self, event: Any) -> None:
        """TrinityEventBus handler: translate the O+V event into a
        governance SSE frame and broadcast it. Fault-isolated — a
        forward failure is counted, never raised."""
        try:
            from backend.core.event_stream import get_event_stream_if_initialized
            es = get_event_stream_if_initialized()
            if es is None:
                self.stats["dropped_no_stream"] += 1
                return
            payload = self._render(event)
            sent = await es.broadcast_event(_SSE_CHANNEL, payload)
            if sent > 0:
                self.stats["forwarded"] += 1
            else:
                # No native client attached — cheap no-op, not an error.
                self.stats["dropped_no_stream"] += 1
        except Exception:  # noqa: BLE001
            self.stats["forward_errors"] += 1
            logger.debug("[GovernanceSSE] forward degraded", exc_info=True)

    @staticmethod
    def _render(event: Any) -> Dict[str, Any]:
        """Map a TrinityEvent → the native client's governance frame.
        Defensive against partial/duck-typed events. NEVER raises."""
        topic = ""
        inner: Dict[str, Any] = {}
        op_id = ""
        try:
            topic = str(getattr(event, "topic", "") or "")
            raw = getattr(event, "payload", None)
            if isinstance(raw, dict):
                inner = raw
                op_id = str(raw.get("op_id", "") or "")
        except Exception:  # noqa: BLE001
            pass
        # A stable, self-describing frame the Swift client can render as
        # "O+V is doing X". ``type`` names the render family; the raw O+V
        # payload rides along untouched for detail views.
        return {
            "type": "ov_activity",
            "topic": topic,
            "event": topic.split(".")[-1] if topic else "activity",
            "op_id": op_id,
            "source": "ouroboros",
            "detail": inner,
        }

    async def uninstall(self) -> None:
        """Release every subscription. Idempotent; NEVER raises."""
        bus, self._bus = self._bus, None
        subs, self._sub_ids = self._sub_ids, []
        self._installed = False
        if bus is None:
            return
        for sub_id in subs:
            try:
                await bus.unsubscribe(sub_id)
            except Exception:  # noqa: BLE001
                pass


# Process-wide singleton (one bridge per backend).
_BRIDGE: Optional[GovernanceSSEBridge] = None


async def install_governance_sse_bridge() -> Optional[GovernanceSSEBridge]:
    """Idempotent boot-seam entry point. Returns the live bridge, or None
    if the buses aren't ready / the bridge is disabled. Safe to call from
    the supervisor boot AND retried later. NEVER raises."""
    global _BRIDGE
    try:
        if _BRIDGE is not None and _BRIDGE.installed:
            return _BRIDGE
        if _BRIDGE is None:
            _BRIDGE = GovernanceSSEBridge()
        ok = await _BRIDGE.install()
        return _BRIDGE if ok else None
    except Exception:  # noqa: BLE001
        return None


def get_governance_sse_bridge() -> Optional[GovernanceSSEBridge]:
    return _BRIDGE


async def reset_governance_sse_bridge() -> None:
    global _BRIDGE
    if _BRIDGE is not None:
        await _BRIDGE.uninstall()
    _BRIDGE = None


__all__ = [
    "GovernanceSSEBridge",
    "install_governance_sse_bridge",
    "get_governance_sse_bridge",
    "reset_governance_sse_bridge",
]
