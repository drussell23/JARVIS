"""Cognitive Integration Bus — Slice 101 (Asynchronous Cognitive Awakening).

A *thin* adapter that lets dormant "cognitive" substrates (belief revision,
counterfactual rehearsal, predictive postmortem, autonomous graduation,
cognitive load shedding, MCP output scanning) react to Orchestrator/GLS
lifecycle transitions WITHOUT bolting blocking synchronous calls into the FSM
hot path.

DESIGN (verify-first, 2026-06-05):
  * We do NOT build a new pub/sub. We COMPOSE the production-hardened
    ``backend.core.trinity_event_bus.TrinityEventBus`` — async delivery via
    priority queues + per-handler ``asyncio.wait_for`` fault isolation, so a
    slow/broken cognitive subscriber can never stall the FSM. ``SkillObserver``
    already subscribes to this bus; we follow that precedent.
  * The orchestrator hot path only ever calls :func:`publish_lifecycle_event`,
    which is **sync-safe, fire-and-forget, and NEVER raises**: it schedules a
    publish coroutine on the running loop and returns immediately. If the master
    flag is off, the bus was never created, or there is no running loop, it is a
    silent no-op (legacy byte-identical).
  * Subscribers are **observational** — they record/forecast and feed back only
    through existing advisory channels (prompt-injected priors, risk-floor).
    They hold NO authority over the FSM (Manifesto §1 invariant preserved).

Master flag ``JARVIS_COGNITIVE_BUS_ENABLED`` — §33.1 default-FALSE. When off,
publish is a no-op and no subscribers register, so the system is identical to
pre-Slice-101 behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("ouroboros.cognitive_bus")

_ENV_ENABLED = "JARVIS_COGNITIVE_BUS_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")

# --- Lifecycle vocabulary (closed) -----------------------------------------
# Namespaced under a dedicated topic root so cognitive subscribers can match a
# single wildcard pattern and never collide with the existing Trinity topics.
_TOPIC_PREFIX = "ouroboros.lifecycle"

LIFECYCLE_PRE_APPLY = "pre_apply"
LIFECYCLE_POST_APPLY = "post_apply"
LIFECYCLE_POST_FAILURE = "post_failure"
LIFECYCLE_SESSION_END = "session_end"
LIFECYCLE_INTAKE_ACCEPT = "intake_accept"
LIFECYCLE_TOOL_EMIT = "tool_emit"

_LIFECYCLE_KINDS = frozenset(
    {
        LIFECYCLE_PRE_APPLY,
        LIFECYCLE_POST_APPLY,
        LIFECYCLE_POST_FAILURE,
        LIFECYCLE_SESSION_END,
        LIFECYCLE_INTAKE_ACCEPT,
        LIFECYCLE_TOOL_EMIT,
    }
)


def cognitive_bus_enabled() -> bool:
    """§33.1 master — default FALSE. Never raises."""
    try:
        raw = os.environ.get(_ENV_ENABLED)
        if raw is None:
            return False
        return raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — env access must never break the hot path
        return False


def lifecycle_topic(kind: str) -> str:
    """Full topic string for a lifecycle kind, e.g. ``ouroboros.lifecycle.pre_apply``."""
    return f"{_TOPIC_PREFIX}.{kind}"


def lifecycle_pattern() -> str:
    """Wildcard pattern matching every lifecycle topic (for subscribers)."""
    return f"{_TOPIC_PREFIX}.*"


def is_lifecycle_kind(kind: str) -> bool:
    return kind in _LIFECYCLE_KINDS


async def _safe_publish(
    bus: Any,
    topic: str,
    data: Dict[str, Any],
    priority: Any,
    correlation_id: Optional[str],
) -> None:
    """Await the bus publish, swallowing ALL errors so the scheduled task can
    never surface an unretrieved exception (e.g. RuntimeError 'not running')."""
    try:
        from backend.core.trinity_event_bus import RepoType

        await bus.publish_raw(
            topic,
            data,
            priority=priority,
            target=RepoType.JARVIS,  # local-only: no cross-repo lifecycle noise
            persist=False,           # high-volume telemetry; never hits the WAL
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget; never propagate
        logger.debug("[CognitiveBus] publish swallowed: %s", exc)


def publish_lifecycle_event(
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    priority: Any = None,
    correlation_id: Optional[str] = None,
) -> bool:
    """Fire-and-forget publish of an FSM lifecycle transition. SYNC-SAFE and
    NEVER raises — safe to drop into any orchestrator/GLS seam.

    Returns True iff a publish task was actually scheduled (master on + bus
    running + running loop present + known kind); False on any inert path. The
    boolean is for tests/telemetry only — callers ignore it.
    """
    try:
        if not cognitive_bus_enabled():
            return False
        if kind not in _LIFECYCLE_KINDS:
            return False

        from backend.core.trinity_event_bus import (
            EventPriority,
            get_event_bus_if_exists,
            is_event_bus_running,
        )

        if not is_event_bus_running():
            # Do NOT create a bus from the hot path; if nobody booted it, skip.
            return False
        bus = get_event_bus_if_exists()
        if bus is None:
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (sync context with no event loop) — best-effort skip.
            return False

        prio = priority if priority is not None else EventPriority.NORMAL
        data = dict(payload or {})
        data.setdefault("lifecycle_kind", kind)
        loop.create_task(
            _safe_publish(bus, lifecycle_topic(kind), data, prio, correlation_id)
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the hot path must never see us raise
        logger.debug("[CognitiveBus] publish_lifecycle_event swallowed: %s", exc)
        return False


# --- Subscriber registry ----------------------------------------------------
# A cognitive subscriber is just (pattern, async handler). We wrap each handler
# so a buggy subscriber can never propagate (belt-and-suspenders on top of the
# bus's own per-handler fault isolation).

CognitiveHandler = Callable[[Any], Awaitable[None]]


class CognitiveSubscriber:
    """A bound (pattern, handler) pair plus a human label for telemetry."""

    __slots__ = ("label", "pattern", "handler")

    def __init__(self, label: str, pattern: str, handler: CognitiveHandler) -> None:
        self.label = label
        self.pattern = pattern
        self.handler = handler


def _wrap_handler(sub: CognitiveSubscriber) -> CognitiveHandler:
    async def _guarded(event: Any) -> None:
        try:
            await sub.handler(event)
        except Exception as exc:  # noqa: BLE001 — one bad subscriber never escapes
            logger.debug("[CognitiveBus] subscriber %s swallowed: %s", sub.label, exc)

    return _guarded


async def register_cognitive_subscribers(
    subscribers: Iterable[CognitiveSubscriber],
    *,
    bus: Any = None,
) -> List[str]:
    """Subscribe each cognitive subscriber to the bus. Called ONCE at boot (not
    the hot path), so here it is fine to get-or-create the bus. Returns the list
    of subscription IDs. Inert (returns []) when the master flag is off. Never
    raises; a single failed subscribe is skipped, not fatal.
    """
    if not cognitive_bus_enabled():
        return []
    sub_ids: List[str] = []
    try:
        if bus is None:
            from backend.core.trinity_event_bus import (
                RepoType,
                get_trinity_event_bus,
            )

            bus = await get_trinity_event_bus(RepoType.JARVIS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CognitiveBus] bus acquisition failed: %s", exc)
        return []

    for sub in subscribers:
        try:
            sid = await bus.subscribe(sub.pattern, _wrap_handler(sub))
            sub_ids.append(sid)
            logger.debug("[CognitiveBus] registered subscriber %s -> %s", sub.label, sub.pattern)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CognitiveBus] subscribe failed for %s: %s", sub.label, exc)
            continue
    return sub_ids
