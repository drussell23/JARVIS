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

import asyncio
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


def _queue_max() -> int:
    try:
        return max(16, int(os.environ.get("JARVIS_GOVERNANCE_SSE_QUEUE", "256")))
    except (TypeError, ValueError):
        return 256


def _conflate_threshold() -> int:
    """A drained batch larger than this is CONFLATED into one summary
    frame instead of forwarded 1:1 (the buffer-bloat guard)."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_GOVERNANCE_SSE_CONFLATE_THRESHOLD", "10")))
    except (TypeError, ValueError):
        return 10


def _drain_window_s() -> float:
    """Coalescing window — after the first event the pump waits this long
    for more to arrive so a burst drains as one batch."""
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_GOVERNANCE_SSE_DRAIN_WINDOW_S", "0.05")))
    except (TypeError, ValueError):
        return 0.05


class GovernanceSSEBridge:
    """Forwards O+V TrinityEventBus events onto the EventStream governance
    channel. One instance per backend process. Every method NEVER
    raises."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._sub_ids: List[str] = []
        self._bus: Any = None
        self._installed = False
        # ---- Async backpressure (Buffer Bloat Guard, mandate 2) ----
        # The bus handler NEVER broadcasts inline; it enqueues into a
        # bounded queue drained by a single pump. Under a flood the pump
        # CONFLATES a whole batch into ONE summary frame, so 200 rapid
        # autonomy events can't fan out into 200 TCP writes and bloat /
        # BrokenPipe the EventStream generator.
        self._queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(
            maxsize=_queue_max())
        self._pump_task: Optional[asyncio.Task] = None
        self.stats: Dict[str, int] = {
            "forwarded": 0, "dropped_no_stream": 0, "forward_errors": 0,
            "enqueued": 0, "conflated": 0, "batches": 0, "queue_overflow": 0,
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
                # Start the single drain pump (backpressure worker).
                if self._pump_task is None or self._pump_task.done():
                    self._pump_task = asyncio.get_event_loop().create_task(
                        self._pump(), name="governance-sse-pump")
                logger.info(
                    "[GovernanceSSE] live — O+V→JARVIS-Apple wire up "
                    "(%d topic patterns, backpressure queue=%d)",
                    len(self._sub_ids), _queue_max(),
                )
            return self._installed
        except Exception:  # noqa: BLE001
            logger.debug("[GovernanceSSE] install degraded", exc_info=True)
            return False

    async def _on_event(self, event: Any) -> None:
        """TrinityEventBus handler — NEVER broadcasts inline. It renders +
        ENQUEUES onto the bounded backpressure queue; the pump does the
        actual (rate-limited, conflating) broadcast. A full queue conflates
        by dropping the oldest so a flood can never block the bus. Fault-
        isolated; NEVER raises."""
        try:
            payload = self._render(event)
            self.stats["enqueued"] += 1
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Buffer bloat guard: shed the oldest, keep the newest —
                # under sustained flood we favor freshness over completeness.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(payload)
                    self.stats["queue_overflow"] += 1
                except Exception:  # noqa: BLE001
                    self.stats["queue_overflow"] += 1
        except Exception:  # noqa: BLE001
            self.stats["forward_errors"] += 1
            logger.debug("[GovernanceSSE] enqueue degraded", exc_info=True)

    async def _pump(self) -> None:
        """Single drain worker (the backpressure heart). Awaits one event,
        coalesces the burst behind it within a short window, then EITHER
        forwards the batch (small) or CONFLATES it into one summary daemon
        frame (flood > threshold) — decoupling the bus from the TCP writes.
        NEVER raises out of the loop."""
        window = _drain_window_s()
        threshold = _conflate_threshold()
        while True:
            try:
                first = await self._queue.get()
                batch = [first]
                # Coalesce everything that arrives within the window.
                if window > 0:
                    try:
                        await asyncio.sleep(window)
                    except asyncio.CancelledError:
                        raise
                while True:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self._flush_batch(batch, threshold)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.stats["forward_errors"] += 1
                logger.debug("[GovernanceSSE] pump degraded", exc_info=True)

    async def _flush_batch(
        self, batch: List[Dict[str, Any]], threshold: int,
    ) -> None:
        """Broadcast a drained batch. ≤ threshold → 1:1; > threshold →
        ONE conflated summary frame (buffer-bloat guard). NEVER raises."""
        try:
            from backend.core.event_stream import (
                get_event_stream_if_initialized,
            )
            es = get_event_stream_if_initialized()
            if es is None:
                self.stats["dropped_no_stream"] += len(batch)
                return
            self.stats["batches"] += 1
            frames: List[Dict[str, Any]]
            if len(batch) > threshold:
                # Conflate: one summary + a pointer to the latest op.
                self.stats["conflated"] += len(batch)
                frames = [self._conflated_frame(batch)]
            else:
                frames = batch
            for payload in frames:
                sent = await es.broadcast_event(_SSE_CHANNEL, payload)
                if sent > 0:
                    self.stats["forwarded"] += 1
                else:
                    self.stats["dropped_no_stream"] += 1
        except Exception:  # noqa: BLE001
            self.stats["forward_errors"] += 1
            logger.debug("[GovernanceSSE] flush degraded", exc_info=True)

    @staticmethod
    def _conflated_frame(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Collapse a flood into ONE daemon frame carrying the count + the
        most-recent op — protecting the SSE generator from the storm."""
        from backend.api.sse_contract import daemon_payload
        n = len(batch)
        latest = batch[-1] if batch else {}
        return daemon_payload(
            command_id=str(latest.get("command_id") or "batch"),
            narration_text=f"{n} background operations",
            narration_priority="low",
            source_brain="ouroboros",
            extra={"type": "ov_activity_batch", "conflated": n,
                   "latest": latest},
        )

    #: Backend lifecycle discriminators the native HUD's Adaptive UI State
    #: Machine (Slice F) reacts to. Forwarded verbatim as ``lifecycle`` so the
    #: Swift ``SystemStatusStore`` can transition deterministically.
    _LIFECYCLE_TYPES = frozenset({
        "SYSTEM_HYDRATING", "SYSTEM_READY", "SYSTEM_DEGRADED", "OUROBOROS_FAULT",
    })

    @staticmethod
    def _render(event: Any) -> Dict[str, Any]:
        """Map a TrinityEvent → a DaemonEvent-shaped payload (mandate 2 —
        the exact Swift ``DaemonEvent`` Codable contract:
        ``command_id / narration_text / narration_priority / source_brain``).
        Defensive against partial/duck-typed events. NEVER raises."""
        from backend.api.sse_contract import daemon_payload
        topic = ""
        inner: Dict[str, Any] = {}
        op_id = ""
        try:
            topic = str(getattr(event, "topic", "") or "")
            raw = getattr(event, "payload", None)
            if isinstance(raw, dict):
                inner = raw
                op_id = str(raw.get("op_id", "") or raw.get("command_id", "") or "")
        except Exception:  # noqa: BLE001
            pass
        verb = topic.split(".")[-1] if topic else "activity"
        raw_type = str(inner.get("type", "") or "")

        # An explicitly-narrated lifecycle/system event (SYSTEM_HYDRATING,
        # SYSTEM_READY, SYSTEM_DEGRADED, OUROBOROS_FAULT, PROVIDER_DEGRADED,
        # FAILOVER_PROVEN, PACKAGE_RECOVERY …) already carries its own rich
        # narration + a ``type`` discriminator. Forward BOTH untouched (Slice F
        # root-cause fix): the previous code overwrote them with a generic
        # "O+V: hydration", so the native HUD was structurally blind to the
        # organism's boot lifecycle. The ``lifecycle`` key drives the Swift
        # Adaptive UI State Machine; the rich text drives the overlay.
        explicit = str(inner.get("narration_text", "") or "")
        if explicit:
            extra: Dict[str, Any] = {"type": raw_type or "ov_activity",
                                     "topic": topic, "event": verb, "op_id": op_id}
            if raw_type in GovernanceSSEBridge._LIFECYCLE_TYPES:
                extra["lifecycle"] = raw_type
            # Forward select structured detail for richer consumers (ignored by
            # the Codable's four required keys, so wire-safe).
            for k in ("state", "reason", "provider", "recovered", "module",
                      "outcome", "exhausted", "next_backoff_s"):
                if k in inner:
                    extra[k] = inner[k]
            return daemon_payload(
                command_id=op_id or "ouroboros",
                narration_text=explicit,
                narration_priority=str(inner.get("narration_priority", "") or "normal"),
                source_brain=str(inner.get("source_brain", "") or "ouroboros"),
                extra=extra,
            )

        # Generic, un-narrated O+V activity (governance topics) — unchanged.
        # Failures narrate at higher priority so the HUD surfaces them.
        prio = "high" if (inner.get("success") is False
                          or "fail" in verb or "error" in verb) else "normal"
        return daemon_payload(
            command_id=op_id or "ouroboros",
            narration_text=f"O+V: {verb}" + (f" ({op_id})" if op_id else ""),
            narration_priority=prio,
            source_brain="ouroboros",
            extra={"type": "ov_activity", "topic": topic, "event": verb,
                   "op_id": op_id, "detail": inner},
        )

    async def uninstall(self) -> None:
        """Release every subscription + stop the pump. Idempotent; NEVER
        raises."""
        bus, self._bus = self._bus, None
        subs, self._sub_ids = self._sub_ids, []
        self._installed = False
        pump, self._pump_task = self._pump_task, None
        if pump is not None and not pump.done():
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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
