"""Gap #3 Slice 3 — autonomy → IDE SSE bridge for worktree topology.

A pure translator. Subscribes to two existing autonomy
``EventEmitter`` event types:

  * ``EXECUTION_GRAPH_STATE_CHANGED`` — emitted by ``SubagentScheduler``
    on every graph-level transition (CREATED → RUNNING → COMPLETED /
    FAILED / CANCELLED).
  * ``WORK_UNIT_STATE_CHANGED`` — emitted on every per-unit
    transition (RUNNING → COMPLETED / FAILED / CANCELLED).

Translates each into a ``StreamEventBroker.publish`` call against
the IDE SSE stream:

  * ``EXECUTION_GRAPH_STATE_CHANGED`` →
    ``EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED``.
  * ``WORK_UNIT_STATE_CHANGED`` →
    ``EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED``.

## Why a bridge (not a scheduler edit)

The scheduler already runs ``_emit_graph_event`` /
``_emit_unit_event`` at every transition; the autonomy
``EventEmitter`` already supports multiple subscribers per event
type with fault-isolated dispatch. Adding a translator subscriber
means:

  * **ZERO changes to the scheduler** — the cage discipline of
    autonomy/ stays untouched.
  * **One-way dependency** — bridge depends on autonomy +
    ide_observability_stream; neither depends on the bridge.
  * **Runtime-attachable** — operators can install / uninstall
    the bridge without restarting the scheduler.
  * **Default-off** — when ``JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED``
    is unset, ``install_default_bridge`` is a no-op (the SSE
    stream stays empty for these event types until graduation).

## Authority surface

  * Imports stdlib + autonomy types (read-only) +
    ide_observability_stream (publish only). ZERO authority-
    carrying imports.
  * Best-effort handler bodies: any internal exception is logged
    at DEBUG and swallowed so the autonomy event chain is never
    disrupted (the EventEmitter already fault-isolates handlers,
    but defense-in-depth holds the bridge to its own contract).
  * No subprocess, no env mutation, no filesystem I/O, no network.

## Default-off

``JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED`` (default ``false`` until
Slice 5 graduation). When off, ``install_default_bridge`` is a
no-op and the bridge handlers (if installed manually) short-
circuit to no-op publishes.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventType,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED,
    EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED,
    StreamEventBroker,
    get_default_broker,
)

logger = logging.getLogger(__name__)


WORKTREE_TOPOLOGY_SSE_BRIDGE_SCHEMA_VERSION: str = (
    "worktree_topology_sse_bridge.1"
)


# ---------------------------------------------------------------------------
# Master flag (default-off until Slice 5 graduation)
# ---------------------------------------------------------------------------


def worktree_topology_sse_enabled() -> bool:
    """``JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED`` (default ``false``
    until Slice 5). NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "",
        ).strip().lower()
        if raw == "":
            return False
        return raw in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Payload extraction helpers
# ---------------------------------------------------------------------------
#
# The autonomy EventEmitter delivers ``EventEnvelope`` instances —
# we extract op_id + payload and re-publish on the broker. Both
# helpers are total: they never raise, returning sensible defaults
# on any malformed input.


def _extract_op_id(event: Any) -> str:
    """Pull op_id off an EventEnvelope. Returns empty string when
    absent / malformed. NEVER raises."""
    try:
        op_id = getattr(event, "op_id", None)
        if op_id is None:
            return ""
        return str(op_id)
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _extract_payload(event: Any) -> Mapping[str, Any]:
    """Pull payload dict off an EventEnvelope. Returns empty dict
    when absent / malformed. NEVER raises."""
    try:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return {}
        return payload
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Bridge class
# ---------------------------------------------------------------------------


class WorktreeTopologySSEBridge:
    """Translator: autonomy ``EventEmitter`` → IDE
    ``StreamEventBroker``.

    Construction binds a broker reference. ``install`` registers
    two handlers on a caller-supplied ``EventEmitter``; the
    handlers stay registered until ``uninstall`` is called or the
    emitter is destroyed.

    The bridge is reusable: calling ``install`` on multiple
    emitters registers a handler on each (use case: a process
    with multiple emitters per layer all feeding the same IDE
    panel). Idempotent on a single emitter — repeat
    ``install`` calls add multiple subscriptions, so callers
    should hold a single bridge instance per emitter.
    """

    def __init__(self, broker: Optional[StreamEventBroker] = None) -> None:
        self._broker = broker

    def _get_broker(self) -> StreamEventBroker:
        if self._broker is not None:
            return self._broker
        return get_default_broker()

    def install(self, emitter: Any) -> None:
        """Register the two translator handlers on ``emitter``.
        Best-effort: swallowed exceptions on subscribe failures.

        The emitter is duck-typed — any object exposing
        ``.subscribe(event_type, handler)`` is accepted. NEVER
        raises into the caller."""
        try:
            emitter.subscribe(
                EventType.EXECUTION_GRAPH_STATE_CHANGED,
                self._handle_graph_event,
            )
            emitter.subscribe(
                EventType.WORK_UNIT_STATE_CHANGED,
                self._handle_unit_event,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WorktreeSSEBridge] install raised: %s", exc,
            )

    async def _handle_graph_event(self, event: Any) -> None:
        """Translate ``EXECUTION_GRAPH_STATE_CHANGED`` →
        ``worktree_topology_updated`` SSE. NEVER raises."""
        try:
            if not worktree_topology_sse_enabled():
                return
            op_id = _extract_op_id(event)
            payload = dict(_extract_payload(event))
            self._get_broker().publish(
                EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED,
                op_id, payload,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WorktreeSSEBridge] graph handler raised: %s",
                exc,
            )

    async def _handle_unit_event(self, event: Any) -> None:
        """Translate ``WORK_UNIT_STATE_CHANGED`` →
        ``worktree_unit_state_changed`` SSE. NEVER raises."""
        try:
            if not worktree_topology_sse_enabled():
                return
            op_id = _extract_op_id(event)
            payload = dict(_extract_payload(event))
            self._get_broker().publish(
                EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED,
                op_id, payload,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[WorktreeSSEBridge] unit handler raised: %s",
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def install_default_bridge(
    emitter: Any,
    *,
    broker: Optional[StreamEventBroker] = None,
) -> Optional[WorktreeTopologySSEBridge]:
    """Convenience: build a bridge with the default broker (or a
    caller-supplied one) and install it on ``emitter``.

    When the master flag is OFF, this is a no-op and returns
    ``None`` — caller should not assume an installed bridge.
    When ON, returns the freshly-installed bridge so callers can
    later detach if needed.

    NEVER raises."""
    try:
        if not worktree_topology_sse_enabled():
            return None
        bridge = WorktreeTopologySSEBridge(broker=broker)
        bridge.install(emitter)
        logger.info(
            "[WorktreeSSEBridge] installed (broker=%s)",
            "default" if broker is None else "custom",
        )
        return bridge
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[WorktreeSSEBridge] install_default_bridge "
            "raised: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "WORKTREE_TOPOLOGY_SSE_BRIDGE_SCHEMA_VERSION",
    "WorktreeTopologySSEBridge",
    "install_default_bridge",
    "worktree_topology_sse_enabled",
]
