"""Conception Proposal Bridge — ranked blueprints → intake router (Gap 3).

The final unification path. The conception value model (``conception_value_model``)
ranks self-conceived work by expected value; DreamEngine produces the conceived
work as ``ImprovementBlueprint``s. Until now nothing carried a *highly ranked*
blueprint into active execution — the ranked scores modulated one sensor's
confidence but no proposal reached the intake queue on its own. This bridge is
that missing translation layer, and NOTHING MORE:

  * **Event-driven** — it reacts to blueprint production (a DreamEngine
    ``register_blueprint_observer`` callback), never polls. On each event it
    evaluates the *current* top-N batch so the threshold is batch-relative.
  * **Dynamic threshold** — a proposal routes iff its EV clears
    ``max(ev_floor, batch_mean_EV)``. Distribution-relative; the only absolute is
    an env floor (default 0.5 = the neutral prior), so a COLD organism — whose
    every EV sits at neutral×cost < floor — routes nothing until real signal
    lifts a proposal above its peers AND above neutral. No hardcoded cutoff.
  * **DRY** — it consumes the value model's EMITTED ``ExpectedValue`` verbatim
    (never recomputes weights), builds the envelope with the existing
    ``make_envelope`` schema, submits through the existing
    ``UnifiedIntakeRouter.ingest``, and records to the existing
    ``proactive_proposal_surface`` ledger. It owns no queue.
  * **Bulletproof dedup** — a durable, TTL+capacity-bounded registry keyed by
    ``blueprint_id`` is the authoritative "already routed / in execution" guard
    (survives across events, unlike the router's 300s window); the envelope's
    ``dedup_key`` (via ``evidence['signature'] = blueprint_id``) is a second
    structural guard inside the router. A blueprint already routed is dropped.

Discipline: authority-free (no gate/policy/orchestrator imports), never raises,
default-OFF master flag (a new autonomous routing capability is opt-in).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CONCEPTION_BRIDGE_SCHEMA_VERSION = "conception_bridge.1"

_TRUTHY = ("1", "true", "yes", "on")

# Router ingest results that mean "the proposal is in the queue" — all of them
# imply the blueprint is now routed and must be deduped on subsequent events.
_ROUTED_RESULTS = frozenset({"enqueued", "pending_ack", "deduplicated"})


# ---------------------------------------------------------------------------
# Env surface
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_CONCEPTION_BRIDGE_ENABLED`` (default FALSE — opt-in)."""
    return os.environ.get(
        "JARVIS_CONCEPTION_BRIDGE_ENABLED", "false",
    ).strip().lower() in _TRUTHY


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _ev_floor() -> float:
    """Absolute EV floor below which nothing routes (default 0.5 = neutral)."""
    return min(1.0, max(0.0, _envf("JARVIS_CONCEPTION_BRIDGE_EV_FLOOR", 0.5)))


def _max_per_route() -> int:
    return max(1, _envi("JARVIS_CONCEPTION_BRIDGE_MAX_PER_ROUTE", 3))


def _dedup_ttl_s() -> float:
    return max(60.0, _envf("JARVIS_CONCEPTION_BRIDGE_DEDUP_TTL_S", 3600.0))


def _dedup_capacity() -> int:
    return max(16, _envi("JARVIS_CONCEPTION_BRIDGE_DEDUP_CAPACITY", 512))


def _top_n() -> int:
    return max(1, _envi("JARVIS_CONCEPTION_BRIDGE_TOP_N", 5))


# ---------------------------------------------------------------------------
# Outcome artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingOutcome:
    """One blueprint's disposition through the bridge. ``routed`` True iff it
    reached the intake queue this pass."""

    blueprint_id: str
    ev: float
    scope: str
    threshold: float
    routed: bool
    reason: str            # "routed" | "below_threshold" | "duplicate" | "ingest_<result>" | "error"
    ingest_result: str = ""


# ---------------------------------------------------------------------------
# Durable dedup registry — authoritative "already routed / in execution" guard
# ---------------------------------------------------------------------------


class _RoutedRegistry:
    """Bounded, TTL-evicted set of routed ``blueprint_id``s. Thread-safe."""

    def __init__(self) -> None:
        self._items: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        ttl = _dedup_ttl_s()
        cap = _dedup_capacity()
        # TTL eviction (oldest-first; insertion order == time order).
        while self._items:
            bid, ts = next(iter(self._items.items()))
            if now - ts > ttl:
                self._items.pop(bid, None)
            else:
                break
        # Capacity eviction.
        while len(self._items) > cap:
            self._items.popitem(last=False)

    def is_routed(self, blueprint_id: str, now: float) -> bool:
        with self._lock:
            self._evict(now)
            return blueprint_id in self._items

    def mark(self, blueprint_id: str, now: float) -> None:
        with self._lock:
            self._items.pop(blueprint_id, None)   # refresh insertion order
            self._items[blueprint_id] = now
            self._evict(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class ConceptionProposalBridge:
    """Event-driven translation layer: ranked blueprints → intake router."""

    def __init__(
        self,
        *,
        value_scorer: Optional[Callable[[Any], Any]] = None,
        ledger_emit: Optional[Callable[..., Any]] = None,
        registry: Optional[_RoutedRegistry] = None,
    ) -> None:
        self._score = value_scorer          # default resolved lazily (DRY)
        self._ledger_emit = ledger_emit     # default resolved lazily
        self._registry = registry or _RoutedRegistry()
        # observability counters
        self._routed_total = 0
        self._dropped_duplicate = 0
        self._below_threshold = 0
        self._errors = 0
        self._events = 0

    # -- axis resolution (lazy, DRY) --------------------------------------

    def _score_blueprint(self, bp: Any) -> Any:
        if self._score is not None:
            return self._score(bp)
        from backend.core.ouroboros.governance.conception_value_model import (
            score_blueprint,
        )
        return score_blueprint(bp)

    def _emit_to_ledger(self, bp: Any, ev: Any) -> None:
        try:
            emit = self._ledger_emit
            if emit is None:
                from backend.core.ouroboros.governance.proactive_proposal_surface import (
                    emit_proposal,
                )
                emit = emit_proposal
            emit(
                kind="opportunity",
                signal_source="auto_proposed",
                summary=str(getattr(bp, "title", "") or getattr(bp, "description", "")),
                rationale=str(getattr(ev, "rationale", "")),
                priority_hint=float(getattr(ev, "ev", 0.5)),
            )
        except Exception:  # noqa: BLE001 — ledger record is best-effort audit
            logger.debug("[ConceptionBridge] ledger emit skipped", exc_info=True)

    # -- core reactor -----------------------------------------------------

    async def route(
        self,
        blueprints: Sequence[Any],
        router: Any,
        *,
        now: Optional[float] = None,
    ) -> List[RoutingOutcome]:
        """Route the batch's above-threshold, non-duplicate blueprints into the
        intake router. Event-reactor — call this on blueprint production, not on
        a timer. Never raises; returns per-blueprint outcomes."""
        if not master_enabled() or router is None:
            return []
        now = now if now is not None else time.time()
        self._events += 1
        try:
            # 1. Score (consume the value model's EMITTED EV; never recompute).
            scored: List[Tuple[Any, Any]] = []
            for bp in (blueprints or ()):
                bid = str(getattr(bp, "blueprint_id", "") or "")
                tfs = tuple(getattr(bp, "target_files", ()) or ())
                if not bid or not tfs:
                    continue
                try:
                    ev = self._score_blueprint(bp)
                except Exception:  # noqa: BLE001
                    self._errors += 1
                    continue
                scored.append((bp, ev))

            # 2. Drop already-routed / in-execution (mandate 4 — durable guard).
            fresh = [
                (bp, ev) for (bp, ev) in scored
                if not self._registry.is_routed(str(bp.blueprint_id), now)
            ]
            outcomes: List[RoutingOutcome] = []
            for (bp, ev) in scored:
                if self._registry.is_routed(str(bp.blueprint_id), now):
                    self._dropped_duplicate += 1
                    outcomes.append(RoutingOutcome(
                        blueprint_id=str(bp.blueprint_id), ev=float(ev.ev),
                        scope=str(ev.scope), threshold=0.0, routed=False,
                        reason="duplicate"))
            if not fresh:
                return outcomes

            # 3. Dynamic, batch-relative threshold. No hardcoded cutoff — the
            #    quality bar rises with the batch mean and never drops below the
            #    neutral floor.
            evs = [float(ev.ev) for (_bp, ev) in fresh]
            threshold = max(_ev_floor(), sum(evs) / len(evs))

            survivors = sorted(
                [(bp, ev) for (bp, ev) in fresh if float(ev.ev) >= threshold],
                key=lambda pe: float(pe[1].ev), reverse=True,
            )[:_max_per_route()]
            survivor_ids = {str(bp.blueprint_id) for (bp, _ev) in survivors}

            for (bp, ev) in fresh:
                if str(bp.blueprint_id) not in survivor_ids:
                    self._below_threshold += 1
                    outcomes.append(RoutingOutcome(
                        blueprint_id=str(bp.blueprint_id), ev=float(ev.ev),
                        scope=str(ev.scope), threshold=threshold, routed=False,
                        reason="below_threshold"))

            # 4. Translate + ingest each survivor.
            for (bp, ev) in survivors:
                outcomes.append(await self._route_one(bp, ev, router, threshold, now))
            return outcomes
        except Exception:  # noqa: BLE001 — reactor never leaks into the loop
            self._errors += 1
            logger.debug("[ConceptionBridge] route pass faulted", exc_info=True)
            return []

    async def _route_one(
        self, bp: Any, ev: Any, router: Any, threshold: float, now: float,
    ) -> RoutingOutcome:
        bid = str(bp.blueprint_id)
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )

            envelope = make_envelope(
                source="auto_proposed",
                description=str(getattr(bp, "description", "") or getattr(bp, "title", "")),
                target_files=tuple(str(t) for t in bp.target_files),
                repo=str(getattr(bp, "repo", "") or ""),
                confidence=max(0.0, min(1.0, float(ev.ev))),
                urgency="low",
                evidence={
                    "sensor": "conception_bridge",
                    # `signature` drives the router's own dedup_key — deterministic
                    # per blueprint, a second structural guard against duplicates.
                    "signature": bid,
                    "blueprint_id": bid,
                    "category": str(getattr(bp, "category", "")),
                    "ev": round(float(ev.ev), 4),
                    "threshold": round(float(threshold), 4),
                    "conception_value": ev.to_dict() if hasattr(ev, "to_dict") else {},
                },
                requires_human_ack=True,  # proactive conception surfaces for ack
            )
            result = str(await router.ingest(envelope))
            # Mark routed on ANY in-queue result (incl. router-side dedup).
            if result in _ROUTED_RESULTS:
                self._registry.mark(bid, now)
                self._routed_total += 1
                self._emit_to_ledger(bp, ev)
                return RoutingOutcome(
                    blueprint_id=bid, ev=float(ev.ev), scope=str(ev.scope),
                    threshold=threshold, routed=True, reason="routed",
                    ingest_result=result)
            return RoutingOutcome(
                blueprint_id=bid, ev=float(ev.ev), scope=str(ev.scope),
                threshold=threshold, routed=False, reason=f"ingest_{result}",
                ingest_result=result)
        except Exception:  # noqa: BLE001
            self._errors += 1
            logger.debug("[ConceptionBridge] ingest faulted for %s", bid, exc_info=True)
            return RoutingOutcome(
                blueprint_id=bid, ev=float(getattr(ev, "ev", 0.0)),
                scope=str(getattr(ev, "scope", "")), threshold=threshold,
                routed=False, reason="error")

    # -- event wiring -----------------------------------------------------

    def make_observer(
        self, router: Any, blueprint_source: Callable[[], Sequence[Any]],
    ) -> Callable[[Any], Any]:
        """Return an async DreamEngine blueprint observer. On each
        blueprint-computed event it pulls the CURRENT batch (so the threshold is
        batch-relative) and routes it. Fail-soft."""
        async def _observer(_blueprint: Any) -> None:
            try:
                batch = list(blueprint_source() or ())
            except Exception:  # noqa: BLE001
                batch = []
            if batch:
                await self.route(batch, router)
        return _observer

    # -- observability ----------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "schema_version": CONCEPTION_BRIDGE_SCHEMA_VERSION,
            "enabled": master_enabled(),
            "ev_floor": _ev_floor(),
            "max_per_route": _max_per_route(),
            "dedup_ttl_s": _dedup_ttl_s(),
            "dedup_registry_size": len(self._registry),
            "counters": {
                "events": self._events,
                "routed_total": self._routed_total,
                "dropped_duplicate": self._dropped_duplicate,
                "below_threshold": self._below_threshold,
                "errors": self._errors,
            },
        }

    def reset_for_tests(self) -> None:
        self._registry.reset()
        self._routed_total = self._dropped_duplicate = 0
        self._below_threshold = self._errors = self._events = 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_default_bridge: Optional[ConceptionProposalBridge] = None
_singleton_lock = threading.Lock()


def get_default_bridge() -> ConceptionProposalBridge:
    global _default_bridge
    with _singleton_lock:
        if _default_bridge is None:
            _default_bridge = ConceptionProposalBridge()
        return _default_bridge


def reset_bridge_for_tests() -> None:
    global _default_bridge
    with _singleton_lock:
        if _default_bridge is not None:
            _default_bridge.reset_for_tests()
        _default_bridge = None


def snapshot() -> dict:
    """Module-level observability projection (safe when disabled)."""
    try:
        return get_default_bridge().snapshot()
    except Exception:  # noqa: BLE001
        return {"schema_version": CONCEPTION_BRIDGE_SCHEMA_VERSION, "enabled": False}
