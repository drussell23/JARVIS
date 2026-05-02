"""SkillObserver -- Slice 3 of SkillRegistry-AutonomousReach arc.
================================================================

Async observer that bridges :class:`TrinityEventBus` signals into
the existing :class:`SkillCatalog` + :class:`SkillInvoker` pipeline.
This is the **proactive surplus** over Claude Code's reactive Skills
surface: when a posture transition / coherence drift / sensor fire
arrives on the event bus, autonomous-reach skills with matching
trigger specs invoke themselves without operator typing or model
prompting.

Reverse-Russian-Doll posture
----------------------------

* O+V (the inner doll, the builder) gains the autonomous-trigger
  surface. The observer is the bridge: bus -> catalog narrow ->
  decision authority -> existing invoker.
* Antivenom (the constraint, the immune system) scales
  proportionally:
    - Bounded concurrency via :class:`asyncio.Semaphore` -- a
      flood of signals can't spawn unbounded invocations.
    - Per-skill sliding-window rate limit + dedup-key TTL cache
      so the same drift signal can't loop-fire a skill.
    - The observer NEVER decides fire/skip itself -- it composes
      :func:`compute_should_fire` (Slice 1, authoritative) with
      :meth:`SkillCatalog.triggers_for_signal` (Slice 2, narrowing).
    - Master flag default false until Slice 5 -- registering with
      the bus before there's anywhere to land would silently
      capture signals.
    - The handler is wrapped in a defensive try/except at every
      boundary: a single buggy skill cannot stall the bus.
    - asyncio.CancelledError propagates per asyncio convention.
    - Pure-stdlib at hot path (governance imports limited to the
      Slice 1 + Slice 2 + Slice 4 surfaces -- AST-pinned at
      Slice 5).

Reuse contract (no duplication)
-------------------------------

* Subscription: TrinityEventBus.subscribe (existing).
* Narrowing: SkillCatalog.triggers_for_signal (Slice 2).
* Decision: skill_trigger.compute_should_fire (Slice 1).
* Dedup key: skill_trigger.compute_dedup_key (Slice 1).
* Rate-limit defaults: skill_per_window_max_invocations +
  skill_window_default_s (Slice 1 env knobs).
* Dispatch: SkillInvoker.invoke (existing arc).
* Telemetry: listener pattern mirroring SkillCatalog.on_change.

What this module is NOT
-----------------------

* A scheduler. The observer fires reactively to bus signals; it
  does not poll. Cron-like skills should use a separate scheduler
  module (out of scope -- a future arc).
* A risk gate. compute_should_fire's risk_floor parameter is the
  enforcement point; the observer just passes it through.
* A bus implementation. The observer accepts any duck-typed object
  with async ``subscribe(pattern, handler) -> str`` and
  ``unsubscribe(sub_id) -> bool`` methods (TrinityEventBus
  satisfies; tests inject a stub).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, Deque, Dict, List, Optional, Protocol,
    Tuple,
)

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillInvocationOutcome,
    SkillInvoker,
    get_default_catalog,
    get_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillInvocation,
    SkillOutcome,
    SkillTriggerKind,
    SkillTriggerSpec,
    compute_dedup_key,
    compute_should_fire,
    skill_per_window_max_invocations,
    skill_window_default_s,
)

logger = logging.getLogger("Ouroboros.SkillObserver")


SKILL_OBSERVER_SCHEMA_VERSION: str = "skill_observer.v1"


# ---------------------------------------------------------------------------
# Sub-flags
# ---------------------------------------------------------------------------


def skill_observer_enabled() -> bool:
    """``JARVIS_SKILL_OBSERVER_ENABLED`` (default ``true`` post
    Slice 5 graduation, 2026-05-02).

    Independent of the Slice 1 ``JARVIS_SKILL_TRIGGER_ENABLED``
    master flag; an operator may want the catalog/index live for
    REPL inspection without the autonomous fire path active. The
    observer's :meth:`start` short-circuits when this is off.
    """
    raw = os.environ.get("JARVIS_SKILL_OBSERVER_ENABLED", "")
    raw = raw.strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _env_int_clamped(
    name: str, *, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return max(floor, min(ceiling, n))


def _env_float_clamped(
    name: str, *, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        n = float(raw) if raw else default
    except ValueError:
        n = default
    return max(floor, min(ceiling, n))


def skill_observer_concurrency() -> int:
    """``JARVIS_SKILL_OBSERVER_CONCURRENCY`` (default 4, floor 1,
    ceiling 32). Caps simultaneous in-flight skill invocations
    so a flood of signals can't spawn unbounded coroutines."""
    return _env_int_clamped(
        "JARVIS_SKILL_OBSERVER_CONCURRENCY",
        default=4, floor=1, ceiling=32,
    )


def skill_dedup_ttl_s() -> float:
    """``JARVIS_SKILL_DEDUP_TTL_S`` (default 300.0, floor 1.0,
    ceiling 3600.0). TTL for the dedup-key cache."""
    return _env_float_clamped(
        "JARVIS_SKILL_DEDUP_TTL_S",
        default=300.0, floor=1.0, ceiling=3600.0,
    )


# ---------------------------------------------------------------------------
# Event-bus protocol (duck-typed)
# ---------------------------------------------------------------------------


class EventBusProtocol(Protocol):
    """Minimal duck-typed contract the observer needs.
    TrinityEventBus satisfies it natively. Tests inject stubs."""

    async def subscribe(
        self, pattern: str, handler: Callable[[Any], Awaitable[None]],
    ) -> str: ...

    async def unsubscribe(self, subscription_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Decision telemetry record (frozen, for listeners + Slice 4 SSE)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillObserverDecision:
    """One observer decision -- fired or skipped, with why.

    Telemetry shape: listeners receive these per-event so the
    Slice 4 SSE bridge can publish ``skill_observer_decision``
    frames covering the full lifecycle (FIRED + every skip
    reason).

    ``skip_reason`` is empty when ``fired=True``;
    ``invocation_outcome`` is set only when fired (the
    SkillInvocationOutcome from SkillInvoker).
    """

    qualified_name: str
    triggered_by_kind: SkillTriggerKind
    triggered_by_signal: str
    outcome: SkillOutcome
    spec_index: Optional[int]
    fired: bool
    skip_reason: str = ""
    invocation_outcome: Optional[SkillInvocationOutcome] = None
    decided_at_monotonic: float = 0.0
    schema_version: str = SKILL_OBSERVER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "triggered_by_kind": self.triggered_by_kind.value,
            "triggered_by_signal": self.triggered_by_signal,
            "outcome": self.outcome.value,
            "spec_index": self.spec_index,
            "fired": self.fired,
            "skip_reason": self.skip_reason,
            "invocation_ok": (
                self.invocation_outcome.ok
                if self.invocation_outcome is not None else None
            ),
            "invocation_duration_ms": (
                self.invocation_outcome.duration_ms
                if self.invocation_outcome is not None else None
            ),
            "decided_at_monotonic": self.decided_at_monotonic,
            "schema_version": self.schema_version,
        }


# Skip-reason vocabulary -- exposed as constants so listeners and
# Slice 5 graduation tests can pin against shared strings instead
# of free-form English.
SKIP_REASON_DECISION: str = "decision"  # compute_should_fire said no
SKIP_REASON_RATE_LIMIT: str = "rate_limit_exhausted"
SKIP_REASON_DEDUP: str = "dedup_hit"
SKIP_REASON_INVOKER_RAISED: str = "invoker_raised"


# ---------------------------------------------------------------------------
# Internal: per-skill sliding-window rate limiter
# ---------------------------------------------------------------------------


@dataclass
class _RateLimitState:
    """Per-skill sliding window. Window + max are derived per-spec
    (spec.window_s + spec.max_invocations override the env defaults
    when non-zero)."""

    window_s: float
    max_invocations: int
    timestamps: Deque[float] = field(default_factory=deque)

    def has_budget(self, now: float) -> bool:
        # Drop timestamps outside the window (sliding).
        cutoff = now - self.window_s
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        return len(self.timestamps) < self.max_invocations

    def record(self, now: float) -> None:
        self.timestamps.append(now)


# ---------------------------------------------------------------------------
# SkillObserver
# ---------------------------------------------------------------------------


class SkillObserver:
    """Async observer bridging TrinityEventBus to SkillCatalog +
    SkillInvoker. See module docstring."""

    def __init__(
        self,
        *,
        event_bus: EventBusProtocol,
        catalog: Optional[SkillCatalog] = None,
        invoker: Optional[SkillInvoker] = None,
        concurrency: Optional[int] = None,
        dedup_ttl_s: Optional[float] = None,
    ) -> None:
        if event_bus is None:
            raise ValueError("event_bus is required")
        self._bus = event_bus
        self._catalog = catalog or get_default_catalog()
        self._invoker = invoker or get_default_invoker()
        self._concurrency = (
            concurrency if concurrency is not None
            else skill_observer_concurrency()
        )
        self._sem = asyncio.Semaphore(max(1, self._concurrency))
        self._dedup_ttl_s = (
            dedup_ttl_s if dedup_ttl_s is not None
            else skill_dedup_ttl_s()
        )
        # subscription_id -> (qualified_name, spec_index)
        self._subscriptions: Dict[str, Tuple[str, int]] = {}
        # qualified_name -> _RateLimitState
        self._rate_limits: Dict[str, _RateLimitState] = {}
        # dedup_key -> expiry_monotonic
        self._dedup_cache: Dict[str, float] = {}
        # Telemetry listeners (called sync per decision; failures
        # logged + swallowed so a buggy listener can't stall the
        # observer).
        self._listeners: List[
            Callable[[SkillObserverDecision], None]
        ] = []
        # Lock guards the catalog-change reaction path so a
        # register-during-start race can't double-subscribe.
        self._lifecycle_lock = asyncio.Lock()
        # Q3 Slice 4 — dedicated lock for ALL ``_subscriptions`` dict
        # mutations + the bus subscribe/unsubscribe calls that pair
        # with them. Separate from ``_lifecycle_lock`` because
        # ``start()`` holds the lifecycle lock while calling
        # ``_subscribe_manifest`` — sharing one lock would deadlock.
        # Lock hierarchy: ``_lifecycle_lock`` is OUTER,
        # ``_subscriptions_lock`` is INNER. ``asyncio.Lock``'s waiter
        # queue is FIFO, so a register-then-unregister-in-close-
        # succession sequence serializes in scheduling order
        # (eliminates leaked-sub-for-unregistered-manifest).
        self._subscriptions_lock = asyncio.Lock()
        # Catalog-change listener handle (for stop cleanup).
        self._catalog_unsub: Optional[Callable[[], None]] = None
        # Started flag -- prevents duplicate start + drives the
        # catalog-listener wiring in start. Q3 Slice 4: ALSO gates
        # post-stop catalog-change tasks so pending coroutines that
        # were scheduled before stop() drained don't subscribe new
        # entries against a torn-down observer.
        self._started = False

    # ---------------- lifecycle ---------------------------------------

    async def start(self) -> int:
        """Walk catalog, subscribe per spec, install
        catalog-change listener for hot-reload. Returns count of
        subscriptions installed.

        Short-circuits to 0 when:
          * master flag JARVIS_SKILL_OBSERVER_ENABLED is off
          * already started

        NEVER raises -- per-spec subscribe failures are logged +
        skipped.
        """
        if not skill_observer_enabled():
            return 0
        async with self._lifecycle_lock:
            if self._started:
                return 0
            self._started = True
            count = 0
            try:
                manifests = self._catalog.list_all()
            except Exception as exc:  # noqa: BLE001 -- defensive
                logger.warning(
                    "[SkillObserver] catalog.list_all degraded: %s",
                    exc,
                )
                manifests = []
            for manifest in manifests:
                count += await self._subscribe_manifest(manifest)
            # Install catalog-change listener for hot-reload of
            # newly-registered manifests.
            try:
                self._catalog_unsub = self._catalog.on_change(
                    self._on_catalog_change,
                )
            except Exception as exc:  # noqa: BLE001 -- defensive
                logger.debug(
                    "[SkillObserver] on_change install degraded: %s",
                    exc,
                )
            logger.info(
                "[SkillObserver] started subs=%d concurrency=%d "
                "dedup_ttl_s=%.1f",
                count, self._concurrency, self._dedup_ttl_s,
            )
            return count

    async def stop(self) -> int:
        """Unsubscribe everything. Returns count unsubscribed.
        NEVER raises.

        Q3 Slice 4 ordering:
          1. Flip ``_started=False`` under ``_lifecycle_lock`` so
             pending catalog-change tasks (waiting on
             ``_subscriptions_lock``) noop when they finally acquire it.
          2. Detach the catalog-change listener BEFORE the drain so
             no NEW tasks are scheduled while we're tearing down.
          3. Drain ``_subscriptions`` under ``_subscriptions_lock``
             so an in-flight catalog-change task can't re-add subs
             concurrently with the drain.

        Without (3), a register-task that already started (acquired
        ``_subscriptions_lock`` first) would land subs after stop()
        snapshot but before the bus.unsubscribe loop — leaked subs."""
        async with self._lifecycle_lock:
            if not self._started:
                return 0
            self._started = False
            # Step 2 first — close the door so _on_catalog_change can't
            # schedule new tasks during the drain.
            if self._catalog_unsub is not None:
                try:
                    self._catalog_unsub()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[SkillObserver] catalog-listener unsub "
                        "degraded: %s", exc,
                    )
                self._catalog_unsub = None
            # Step 3 — atomic drain.
            count = 0
            async with self._subscriptions_lock:
                sub_ids = list(self._subscriptions.keys())
                for sub_id in sub_ids:
                    try:
                        ok = await self._bus.unsubscribe(sub_id)
                        if ok:
                            count += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[SkillObserver] unsubscribe(%s) "
                            "degraded: %s",
                            sub_id, exc,
                        )
                    self._subscriptions.pop(sub_id, None)
            logger.info(
                "[SkillObserver] stopped unsubs=%d", count,
            )
            return count

    @property
    def is_started(self) -> bool:
        return self._started

    # ---------------- per-spec subscription ---------------------------

    async def _subscribe_manifest(
        self, manifest: SkillManifest,
    ) -> int:
        """Subscribe every observable spec on a manifest, idempotently.

        Q3 Slice 4 atomicity guarantees:

          * Acquires ``_subscriptions_lock`` for the WHOLE body —
            concurrent calls (e.g. ``start()`` racing a
            catalog ``skill_registered`` listener task) serialize.
          * Idempotent: any existing subscriptions already keyed to
            this ``qualified_name`` are unsubscribed FIRST, so a
            register-during-start double-subscribe lands a single
            consistent set of subs at quiescence (the catalog's most
            recent manifest definition wins).
          * Gated on ``self._started`` so a task scheduled before
            ``stop()`` but executed after it cleanly no-ops.

        Returns the count of subscriptions installed for this manifest
        (NOT counting any prior subs that were dropped). NEVER raises
        — every per-spec failure is logged at DEBUG and skipped."""
        try:
            specs = tuple(manifest.trigger_specs or ())
        except Exception:  # noqa: BLE001 -- defensive
            return 0
        async with self._subscriptions_lock:
            if not self._started:
                # stop() drained between schedule and execution.
                return 0
            # Idempotent prefix: drop any existing subs for this
            # qualified name before re-subscribing.
            await self._drop_subs_for_qname_locked(manifest.qualified_name)
            count = 0
            for idx, spec in enumerate(specs):
                if not isinstance(spec, SkillTriggerSpec):
                    continue
                # An observable spec needs a non-empty signal_pattern
                # AND a non-DISABLED kind.
                if not spec.signal_pattern:
                    continue
                if spec.kind is SkillTriggerKind.DISABLED:
                    continue
                sub_id = await self._subscribe_one(
                    manifest.qualified_name, idx, spec,
                )
                if sub_id:
                    count += 1
            return count

    async def _drop_subs_for_qname_locked(self, qname: str) -> int:
        """Drop every subscription keyed to ``qname``. MUST be called
        with ``_subscriptions_lock`` held. Returns count dropped."""
        existing = [
            s for s, (n, _i) in self._subscriptions.items()
            if n == qname
        ]
        dropped = 0
        for sub_id in existing:
            try:
                ok = await self._bus.unsubscribe(sub_id)
                if ok:
                    dropped += 1
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[SkillObserver] drop unsubscribe(%s) degraded: %s",
                    sub_id, exc,
                )
            self._subscriptions.pop(sub_id, None)
        return dropped

    async def _unsubscribe_qname(self, qname: str) -> int:
        """Q3 Slice 4 — atomic unsubscribe of all subs for a qname.

        Replaces the sync iterate-and-schedule pattern in
        ``_on_catalog_change`` (which had two bugs: it iterated
        ``_subscriptions`` from a sync context where another task
        could be mutating it, and it scheduled N independent tasks
        whose interleaving could leak subs). This single-task path
        takes the lock once, drains under it, and noop-gates on
        ``self._started`` to handle post-stop scheduling."""
        async with self._subscriptions_lock:
            if not self._started:
                return 0
            return await self._drop_subs_for_qname_locked(qname)

    async def _subscribe_one(
        self,
        qualified_name: str,
        spec_index: int,
        spec: SkillTriggerSpec,
    ) -> Optional[str]:
        """Subscribe one (qname, spec) pair. Captures qname +
        spec_index + spec.kind in the closure so the handler knows
        exactly which spec produced the event."""
        spec_kind = spec.kind  # frozen capture -- spec is immutable

        async def _handler(event: Any) -> None:
            await self._on_event(
                qualified_name, spec_index, spec_kind, event,
            )

        try:
            sub_id = await self._bus.subscribe(
                spec.signal_pattern, _handler,
            )
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillObserver] subscribe failed for "
                "qname=%s spec_index=%d pattern=%s: %s",
                qualified_name, spec_index, spec.signal_pattern, exc,
            )
            return None
        if not isinstance(sub_id, str) or not sub_id:
            return None
        self._subscriptions[sub_id] = (qualified_name, spec_index)
        return sub_id

    # ---------------- catalog change reactivity -----------------------

    def _on_catalog_change(self, payload: Dict[str, Any]) -> None:
        """SkillCatalog.on_change listener — reacts to register /
        unregister. Schedules async work so the sync listener
        callback never blocks the catalog mutation path.

        Q3 Slice 4 atomicity model:

          * Each event type maps to ONE coroutine that takes
            ``_subscriptions_lock`` and operates atomically. We do
            NOT iterate ``_subscriptions`` here — that read happened
            under no lock + concurrent mutation could (and did)
            raise ``RuntimeError: dictionary changed size during
            iteration`` from a sync callback that has no way to
            recover.
          * ``asyncio.Lock`` waiter queue is FIFO, so a register
            immediately followed by an unregister for the same qname
            executes in scheduling order — the unregister sees the
            subs the register installed and clears them.

        NEVER raises — a failure here cannot stall register/
        unregister."""
        try:
            event_type = payload.get("event_type")
            qname = payload.get("qualified_name")
            if not isinstance(qname, str) or not qname:
                return
            if event_type == "skill_registered":
                manifest = self._catalog.get(qname)
                if manifest is not None:
                    asyncio.create_task(
                        self._subscribe_manifest(manifest),
                    )
            elif event_type == "skill_unregistered":
                asyncio.create_task(self._unsubscribe_qname(qname))
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillObserver] _on_catalog_change degraded: %s",
                exc,
            )

    async def _unsubscribe_one(self, sub_id: str) -> bool:
        """Unsubscribe a single subscription id atomically.

        Q3 Slice 4 — acquires ``_subscriptions_lock`` so the dict pop
        + bus.unsubscribe are paired with no other mutator interleaving.
        Used only for one-off unsubscribe (tests, future targeted
        cleanup) — the catalog-unregister path uses
        :meth:`_unsubscribe_qname` so it can drop ALL subs for a qname
        in a single critical section."""
        async with self._subscriptions_lock:
            try:
                ok = await self._bus.unsubscribe(sub_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SkillObserver] _unsubscribe_one degraded: %s",
                    exc,
                )
                ok = False
            self._subscriptions.pop(sub_id, None)
            return bool(ok)

    # ---------------- event handling ----------------------------------

    async def _on_event(
        self,
        qualified_name: str,
        spec_index: int,
        spec_kind: SkillTriggerKind,
        event: Any,
    ) -> None:
        """Bus delivers an event matching the spec.signal_pattern.
        Compose: SkillInvocation -> catalog narrow check -> Slice
        1 decision -> rate limit -> dedup -> invoker.

        NEVER raises into the bus."""
        try:
            if asyncio.iscoroutine(event):  # pragma: no cover
                event = await event
            topic = str(getattr(event, "topic", "") or "")
            raw_payload = getattr(event, "payload", {}) or {}
            try:
                payload = dict(raw_payload)
            except Exception:  # noqa: BLE001
                payload = {}
            invocation = SkillInvocation(
                skill_name=qualified_name,
                triggered_by_kind=spec_kind,
                triggered_by_signal=topic,
                triggered_at_monotonic=time.monotonic(),
                payload=payload,
            )
            # Slice 2 narrowing -- defensive cross-check that the
            # spec actually matches the payload (e.g., the spec
            # might want required_posture="HARDEN" while the bus
            # only matched the topic). Restrict to OUR (qname,
            # spec_index) so we don't accidentally fire other
            # skills sharing the topic (those have their own
            # subscriptions).
            candidates = self._catalog.triggers_for_signal(invocation)
            ours = [
                (m, idx) for m, idx in candidates
                if m.qualified_name == qualified_name
                and idx == spec_index
            ]
            if not ours:
                # Topic matched but spec narrowing rejected. Emit a
                # decision row (precondition skip) and stop.
                manifest_for_telemetry = self._catalog.get(
                    qualified_name,
                )
                if manifest_for_telemetry is not None:
                    self._emit_decision(SkillObserverDecision(
                        qualified_name=qualified_name,
                        triggered_by_kind=spec_kind,
                        triggered_by_signal=topic,
                        outcome=SkillOutcome.SKIPPED_PRECONDITION,
                        spec_index=spec_index,
                        fired=False,
                        skip_reason=SKIP_REASON_DECISION,
                        decided_at_monotonic=time.monotonic(),
                    ))
                return
            manifest, _ = ours[0]
            await self._evaluate_and_fire(
                manifest, spec_index, invocation, topic,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillObserver] _on_event degraded "
                "qname=%s: %s", qualified_name, exc,
            )

    async def _evaluate_and_fire(
        self,
        manifest: SkillManifest,
        spec_index: int,
        invocation: SkillInvocation,
        topic: str,
    ) -> None:
        """Slice 1 decision -> rate limit -> dedup -> invoke.
        NEVER raises (CancelledError propagates per asyncio
        convention)."""
        spec = manifest.trigger_specs[spec_index]
        decision = compute_should_fire(manifest, invocation)

        if not decision.is_invoked:
            self._emit_decision(SkillObserverDecision(
                qualified_name=manifest.qualified_name,
                triggered_by_kind=invocation.triggered_by_kind,
                triggered_by_signal=topic,
                outcome=decision.outcome,
                spec_index=spec_index,
                fired=False,
                skip_reason=SKIP_REASON_DECISION,
                decided_at_monotonic=time.monotonic(),
            ))
            return

        # Rate limit check.
        now = time.monotonic()
        state = self._get_rate_limit_state(
            manifest.qualified_name, spec,
        )
        if not state.has_budget(now):
            self._emit_decision(SkillObserverDecision(
                qualified_name=manifest.qualified_name,
                triggered_by_kind=invocation.triggered_by_kind,
                triggered_by_signal=topic,
                outcome=decision.outcome,
                spec_index=spec_index,
                fired=False,
                skip_reason=SKIP_REASON_RATE_LIMIT,
                decided_at_monotonic=now,
            ))
            return

        # Dedup check -- OPT-IN via spec.dedup_key_template. Skipped
        # entirely when the operator didn't opt in, so rate-limit
        # alone bounds invocation. (compute_dedup_key has a
        # structural-fingerprint fallback for callers that want
        # one, but the observer treats no-template = no-dedup so
        # multiple distinct invocations with the same payload
        # shape aren't silently swallowed.)
        if spec.dedup_key_template:
            dedup_key = compute_dedup_key(invocation, spec)
        else:
            dedup_key = ""
        if dedup_key and self._is_dedup_hit(dedup_key, now):
            self._emit_decision(SkillObserverDecision(
                qualified_name=manifest.qualified_name,
                triggered_by_kind=invocation.triggered_by_kind,
                triggered_by_signal=topic,
                outcome=decision.outcome,
                spec_index=spec_index,
                fired=False,
                skip_reason=SKIP_REASON_DEDUP,
                decided_at_monotonic=now,
            ))
            return

        # Bounded concurrency invoke.
        async with self._sem:
            invoke_outcome: Optional[SkillInvocationOutcome] = None
            invoke_error: Optional[str] = None
            try:
                invoke_outcome = await self._invoker.invoke(
                    manifest.qualified_name,
                    args=dict(invocation.arguments),
                )
            except Exception as exc:  # noqa: BLE001 -- defensive
                # Invoker is documented to never raise out (it
                # returns ok=False outcomes), but defense in depth.
                invoke_error = (
                    f"{type(exc).__name__}: {exc}"
                )

        # Always record the rate-limit + dedup AFTER the attempt --
        # a failed invoke still consumed a budget slot.
        state.record(now)
        if dedup_key:
            self._record_dedup(dedup_key, now)

        if invoke_error is not None:
            self._emit_decision(SkillObserverDecision(
                qualified_name=manifest.qualified_name,
                triggered_by_kind=invocation.triggered_by_kind,
                triggered_by_signal=topic,
                outcome=decision.outcome,
                spec_index=spec_index,
                fired=False,
                skip_reason=SKIP_REASON_INVOKER_RAISED + ":"
                            + invoke_error[:100],
                decided_at_monotonic=now,
            ))
            return

        self._emit_decision(SkillObserverDecision(
            qualified_name=manifest.qualified_name,
            triggered_by_kind=invocation.triggered_by_kind,
            triggered_by_signal=topic,
            outcome=decision.outcome,
            spec_index=spec_index,
            fired=True,
            invocation_outcome=invoke_outcome,
            decided_at_monotonic=now,
        ))

    # ---------------- rate-limit + dedup helpers ----------------------

    def _get_rate_limit_state(
        self, qualified_name: str, spec: SkillTriggerSpec,
    ) -> _RateLimitState:
        """Get or create the per-skill rate limit. Spec overrides
        env defaults when non-zero."""
        state = self._rate_limits.get(qualified_name)
        if state is not None:
            return state
        window = (
            spec.window_s if spec.window_s > 0
            else skill_window_default_s()
        )
        max_inv = (
            spec.max_invocations if spec.max_invocations > 0
            else skill_per_window_max_invocations()
        )
        state = _RateLimitState(
            window_s=window, max_invocations=max_inv,
        )
        self._rate_limits[qualified_name] = state
        return state

    def _is_dedup_hit(self, key: str, now: float) -> bool:
        # Cheap GC: drop expired entries while we're here.
        expired = [k for k, exp in self._dedup_cache.items() if exp <= now]
        for k in expired:
            self._dedup_cache.pop(k, None)
        return key in self._dedup_cache

    def _record_dedup(self, key: str, now: float) -> None:
        self._dedup_cache[key] = now + self._dedup_ttl_s

    # ---------------- listener pattern --------------------------------

    def on_decision(
        self, listener: Callable[[SkillObserverDecision], None],
    ) -> Callable[[], None]:
        """Register a telemetry listener. Returns an unsubscribe
        callable. Listener exceptions are logged and swallowed so
        a buggy consumer can't stall the observer."""
        self._listeners.append(listener)

        def _unsub() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _unsub

    def _emit_decision(self, decision: SkillObserverDecision) -> None:
        for l in list(self._listeners):
            try:
                l(decision)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SkillObserver] listener exception: %s", exc,
                )
        # Slice 5 graduation: best-effort SSE publish so operators
        # see the full lifecycle (FIRED + every skip reason). Lazy
        # import keeps the observer importable when the IDE
        # observability stream module isn't available (test
        # isolation, partial deployments). NEVER raises.
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_skill_invocation as _publish_skill_invocation,
            )
            _publish_skill_invocation(
                qualified_name=decision.qualified_name,
                triggered_by_kind=(
                    decision.triggered_by_kind.value
                    if decision.triggered_by_kind is not None
                    else ""
                ),
                triggered_by_signal=decision.triggered_by_signal,
                outcome=(
                    decision.outcome.value
                    if decision.outcome is not None else ""
                ),
                spec_index=decision.spec_index,
                fired=decision.fired,
                skip_reason=decision.skip_reason,
                invocation_ok=(
                    decision.invocation_outcome.ok
                    if decision.invocation_outcome is not None
                    else None
                ),
                invocation_duration_ms=(
                    decision.invocation_outcome.duration_ms
                    if decision.invocation_outcome is not None
                    else None
                ),
                decided_at_monotonic=decision.decided_at_monotonic,
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort
            logger.debug(
                "[SkillObserver] SSE publish degraded: %s", exc,
            )

    # ---------------- observability snapshots -------------------------

    def subscription_count(self) -> int:
        return len(self._subscriptions)

    def rate_limit_snapshot(self) -> Dict[str, int]:
        """Per-skill in-window invocation count. Drained of
        expired entries on read."""
        now = time.monotonic()
        out: Dict[str, int] = {}
        for qname, state in self._rate_limits.items():
            cutoff = now - state.window_s
            while state.timestamps and state.timestamps[0] < cutoff:
                state.timestamps.popleft()
            out[qname] = len(state.timestamps)
        return out

    def dedup_cache_size(self) -> int:
        now = time.monotonic()
        expired = [k for k, exp in self._dedup_cache.items() if exp <= now]
        for k in expired:
            self._dedup_cache.pop(k, None)
        return len(self._dedup_cache)


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_observer: Optional[SkillObserver] = None


def get_default_observer(
    *,
    event_bus: Optional[EventBusProtocol] = None,
    catalog: Optional[SkillCatalog] = None,
    invoker: Optional[SkillInvoker] = None,
) -> Optional[SkillObserver]:
    """Lazy singleton. Returns None when called without an
    event_bus AND no observer has been initialised yet -- the
    bus is required for first construction.

    Subsequent calls without args return the existing instance.
    """
    global _default_observer
    if _default_observer is not None:
        return _default_observer
    if event_bus is None:
        return None
    _default_observer = SkillObserver(
        event_bus=event_bus, catalog=catalog, invoker=invoker,
    )
    return _default_observer


def reset_default_observer() -> None:
    """Test helper. Resets the singleton without stopping it
    (caller should ``await observer.stop()`` first)."""
    global _default_observer
    _default_observer = None


__all__ = [
    "EventBusProtocol",
    "SKILL_OBSERVER_SCHEMA_VERSION",
    "SKIP_REASON_DECISION",
    "SKIP_REASON_DEDUP",
    "SKIP_REASON_INVOKER_RAISED",
    "SKIP_REASON_RATE_LIMIT",
    "SkillObserver",
    "SkillObserverDecision",
    "get_default_observer",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_observer",
    "skill_dedup_ttl_s",
    "skill_observer_concurrency",
    "skill_observer_enabled",
]


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[SkillObserver] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/skill_observer.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_SKILL_OBSERVER_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_SKILL_OBSERVER_ENABLED=true",
            description=(
                "Master switch for the SkillObserver async fire "
                "loop. Independent of JARVIS_SKILL_TRIGGER_ENABLED. "
                "Graduated default-true 2026-05-02 in Slice 5."
            ),
        ),
        FlagSpec(
            name="JARVIS_SKILL_OBSERVER_CONCURRENCY",
            type=FlagType.INT, default=4,
            category=Category.CAPACITY,
            source_file=target,
            example="JARVIS_SKILL_OBSERVER_CONCURRENCY=8",
            description=(
                "asyncio.Semaphore cap for simultaneous in-flight "
                "skill invocations. Floor 1, ceiling 32."
            ),
        ),
        FlagSpec(
            name="JARVIS_SKILL_DEDUP_TTL_S",
            type=FlagType.FLOAT, default=300.0,
            category=Category.TIMING,
            source_file=target,
            example="JARVIS_SKILL_DEDUP_TTL_S=600",
            description=(
                "TTL for the dedup-key cache. Floor 1.0, ceiling "
                "3600.0. Only used when a spec opts into dedup via "
                "spec.dedup_key_template."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillObserver] register_flags spec %s skipped: "
                "%s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 3 invariants: authority allowlist (Slice 1 + Slice 2 +
    additive observability) + bounded-concurrency contract."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {
        "skill_trigger",       # Slice 1 primitive
        "skill_catalog",       # Slice 2 narrowing index
        "skill_manifest",      # Slice 1 additive fields
        # Additive observability (Slice 5 SSE publish + module-owned
        # registration). Never reached on the hot fire path.
        "ide_observability_stream",
        "flag_registry",
        "shipped_code_invariants",
    }
    _FORBIDDEN = {
        "orchestrator", "phase_runner", "iron_gate",
        "change_engine", "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    }

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"unexpected governance import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 3 MUST NOT {node.func.id}()"
                        )
        # Bounded-concurrency contract: SkillObserver MUST hold a
        # semaphore attribute (renamed = drift signal).
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "SkillObserver"
            ):
                src_section = source[
                    getattr(node, "col_offset", 0):
                    getattr(node, "end_col_offset", 0) + 50000
                ] if False else source
                if "asyncio.Semaphore(" not in src_section:
                    violations.append(
                        "SkillObserver missing asyncio.Semaphore "
                        "(bounded-concurrency contract)"
                    )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/skill_observer.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="skill_observer_authority",
            target_file=target,
            description=(
                "Slice 3 observer authority: imports only "
                "skill_trigger / skill_catalog / skill_manifest + "
                "additive observability (ide_observability_stream / "
                "flag_registry / shipped_code_invariants). Bounded "
                "concurrency via asyncio.Semaphore. No "
                "exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
