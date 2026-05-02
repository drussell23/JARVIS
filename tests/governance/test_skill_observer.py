"""SkillObserver Slice 3 -- regression spine.

Pins the async observer that bridges TrinityEventBus signals to
SkillCatalog narrowing -> Slice 1 decision -> SkillInvoker
dispatch -> rate limit + dedup.

Coverage:
  * Sub-flag asymmetric env semantics (default false until Slice 5)
  * Concurrency + dedup-TTL env knob clamping
  * start() walks catalog + subscribes per spec; stop() cleanup
  * Master-flag-off short-circuit returns 0 subscriptions
  * Specs without signal_pattern OR with kind=DISABLED skip
    subscribe (no-op)
  * Catalog hot-reload: register-after-start auto-subscribes;
    unregister-after-start cleans up
  * Event handling end-to-end: bus delivers -> compute_should_fire
    INVOKED -> invoker dispatched -> decision telemetry recorded
  * Spec narrowing: bus topic matches but spec.required_posture
    rejects -> SKIPPED_PRECONDITION
  * Decision NO -> not invoked, telemetry recorded
  * Rate limit: enforces sliding window (Nth+1 fire skipped)
  * Rate limit overrides via spec.window_s + spec.max_invocations
  * Dedup: same key within TTL -> skipped; after TTL -> fires again
  * Concurrency cap: simultaneous events bounded by semaphore
  * Telemetry listener pattern (subscribe + unsubscribe)
  * Listener exception doesn't stall observer
  * Invoker exception (defense-in-depth) -> decision recorded
    with INVOKER_RAISED skip reason
  * asyncio.CancelledError propagates per asyncio convention
  * subscription_count + rate_limit_snapshot + dedup_cache_size
    observability helpers
  * Singleton get/reset
  * NEVER raises into the bus -- defensive across the handler
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillInvocationOutcome,
    SkillInvoker,
    SkillSource,
    reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
)
from backend.core.ouroboros.governance.skill_observer import (
    SKILL_OBSERVER_SCHEMA_VERSION,
    SKIP_REASON_DECISION,
    SKIP_REASON_DEDUP,
    SKIP_REASON_INVOKER_RAISED,
    SKIP_REASON_RATE_LIMIT,
    SkillObserver,
    SkillObserverDecision,
    get_default_observer,
    reset_default_observer,
    skill_dedup_ttl_s,
    skill_observer_concurrency,
    skill_observer_enabled,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillOutcome,
    SkillTriggerKind,
)


# ---------------------------------------------------------------------------
# Fixtures: stub event bus + invoker
# ---------------------------------------------------------------------------


@dataclass
class _StubEvent:
    topic: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class _StubBus:
    """Minimal duck-typed event bus implementing the
    EventBusProtocol contract."""

    def __init__(self) -> None:
        self._subs: Dict[str, "tuple[str, Callable]"] = {}
        self._counter = 0
        self.subscribe_failures = 0  # incremented when a sub fails
        self._lock = asyncio.Lock()

    async def subscribe(
        self, pattern: str,
        handler: Callable[[Any], Awaitable[None]],
    ) -> str:
        async with self._lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            self._subs[sub_id] = (pattern, handler)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        async with self._lock:
            return self._subs.pop(subscription_id, None) is not None

    async def deliver(self, topic: str, payload: Dict[str, Any]) -> int:
        """Test helper -- mimics the bus delivering an event to all
        matching subscribers. Returns count fired."""
        # snapshot to avoid mutation during iteration
        async with self._lock:
            entries = list(self._subs.items())
        count = 0
        for _, (pattern, handler) in entries:
            if self._topic_matches(pattern, topic):
                event = _StubEvent(topic=topic, payload=dict(payload))
                await handler(event)
                count += 1
        return count

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        # Tiny matcher: "*" wildcard + literal segments. Mirrors
        # TrinityEventBus's pattern shape closely enough for tests.
        if pattern == topic or pattern == "*":
            return True
        if pattern.endswith(".*") and topic.startswith(
            pattern[:-2] + ".",
        ):
            return True
        return False

    @property
    def active_subscriptions(self) -> int:
        return len(self._subs)


class _CountingInvoker:
    """Stand-in for SkillInvoker -- records invoke() calls + can
    be configured to raise."""

    def __init__(
        self, *, ok: bool = True, raises: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        self.calls: List[tuple] = []
        self._ok = ok
        self._raises = raises
        self._delay_s = delay_s

    async def invoke(
        self, qualified_name: str, *,
        args: Optional[Dict[str, Any]] = None,
        output_preview_chars: int = 400,
    ) -> SkillInvocationOutcome:
        self.calls.append((qualified_name, dict(args or {})))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises:
            raise RuntimeError("invoker boom")
        return SkillInvocationOutcome(
            qualified_name=qualified_name,
            ok=self._ok, duration_ms=1.5,
            result_preview="ok",
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_manifest(
    *, name: str, reach: str = "any", trigger_specs=None,
) -> SkillManifest:
    return SkillManifest.from_mapping({
        "name": name,
        "description": "d", "trigger": "t",
        "entrypoint": "mod.x:f",
        "reach": reach,
        "trigger_specs": list(trigger_specs or ()),
    })


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_SKILL_TRIGGER_ENABLED",
        "JARVIS_SKILL_OBSERVER_ENABLED",
        "JARVIS_SKILL_OBSERVER_CONCURRENCY",
        "JARVIS_SKILL_DEDUP_TTL_S",
        "JARVIS_SKILL_PER_WINDOW_MAX",
        "JARVIS_SKILL_WINDOW_S",
    ):
        monkeypatch.delenv(var, raising=False)
    # Default ON for trigger primitive so compute_should_fire
    # doesn't short-circuit; observer flag explicitly toggled per test.
    monkeypatch.setenv("JARVIS_SKILL_TRIGGER_ENABLED", "true")
    yield
    reset_default_catalog()
    reset_default_invoker()
    reset_default_observer()


@pytest.fixture
def catalog():
    return SkillCatalog()


@pytest.fixture
def bus():
    return _StubBus()


@pytest.fixture
def invoker():
    return _CountingInvoker()


# ---------------------------------------------------------------------------
# Sub-flag semantics
# ---------------------------------------------------------------------------


class TestObserverFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_OBSERVER_ENABLED", raising=False,
        )
        assert skill_observer_enabled() is True

    def test_empty_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "")
        assert skill_observer_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "ON"])
    def test_truthy_enables(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", raw)
        assert skill_observer_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", raw)
        assert skill_observer_enabled() is False


class TestEnvKnobs:
    def test_concurrency_default(self):
        assert skill_observer_concurrency() == 4

    def test_concurrency_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_CONCURRENCY", "0")
        assert skill_observer_concurrency() == 1

    def test_concurrency_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_CONCURRENCY", "9999")
        assert skill_observer_concurrency() == 32

    def test_concurrency_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_OBSERVER_CONCURRENCY", "abc",
        )
        assert skill_observer_concurrency() == 4

    def test_dedup_ttl_default(self):
        assert skill_dedup_ttl_s() == 300.0

    def test_dedup_ttl_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_DEDUP_TTL_S", "0")
        assert skill_dedup_ttl_s() == 1.0

    def test_dedup_ttl_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_DEDUP_TTL_S", "100000")
        assert skill_dedup_ttl_s() == 3600.0


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_no_bus_raises(self):
        with pytest.raises(ValueError, match="event_bus is required"):
            SkillObserver(event_bus=None)  # type: ignore[arg-type]

    def test_schema_version(self):
        assert (
            SKILL_OBSERVER_SCHEMA_VERSION == "skill_observer.v1"
        )

    def test_default_skip_reason_constants(self):
        assert SKIP_REASON_DECISION == "decision"
        assert SKIP_REASON_RATE_LIMIT == "rate_limit_exhausted"
        assert SKIP_REASON_DEDUP == "dedup_hit"
        assert SKIP_REASON_INVOKER_RAISED == "invoker_raised"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_short_circuits_when_disabled(
        self, monkeypatch, catalog, bus, invoker,
    ):
        # Master observer flag explicitly OFF (post-graduation
        # default is true; operator escape hatch is "false").
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "false")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "sensor_fired",
                 "signal_pattern": "sensor.fired.test"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.start()
        assert n == 0
        assert obs.subscription_count() == 0
        assert bus.active_subscriptions == 0
        assert obs.is_started is False

    @pytest.mark.asyncio
    async def test_start_subscribes_per_observable_spec(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "sensor_fired",
                 "signal_pattern": "sensor.fired.a"},
                {"kind": "drift_detected",
                 "signal_pattern": "coherence.drift_detected"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.start()
        assert n == 2
        assert obs.subscription_count() == 2
        assert bus.active_subscriptions == 2

    @pytest.mark.asyncio
    async def test_start_skips_specs_without_signal_pattern(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                # No signal_pattern -- explicit invocation only
                {"kind": "explicit_invocation"},
                # Empty signal_pattern
                {"kind": "sensor_fired"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.start()
        assert n == 0

    @pytest.mark.asyncio
    async def test_start_skips_disabled_kind_specs(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "disabled",
                 "signal_pattern": "anything"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.start()
        assert n == 0

    @pytest.mark.asyncio
    async def test_double_start_no_double_subscribe(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "sensor_fired",
                 "signal_pattern": "sensor.fired.a"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n1 = await obs.start()
        n2 = await obs.start()
        assert n1 == 1
        assert n2 == 0  # already started
        assert bus.active_subscriptions == 1

    @pytest.mark.asyncio
    async def test_stop_unsubscribes_all(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "sensor_fired",
                 "signal_pattern": "sensor.fired.a"},
                {"kind": "drift_detected",
                 "signal_pattern": "coherence.drift_detected"},
            ]),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        n = await obs.stop()
        assert n == 2
        assert bus.active_subscriptions == 0
        assert obs.subscription_count() == 0
        assert obs.is_started is False

    @pytest.mark.asyncio
    async def test_stop_when_never_started(
        self, catalog, bus, invoker,
    ):
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.stop()
        assert n == 0


# ---------------------------------------------------------------------------
# Event handling end-to-end
# ---------------------------------------------------------------------------


class TestEventHandling:
    @pytest.mark.asyncio
    async def test_matching_event_invokes(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        await bus.deliver("sensor.fired.test", {"sensor_name": "test"})
        assert len(invoker.calls) == 1
        assert invoker.calls[0][0] == "a"
        assert len(decisions) == 1
        assert decisions[0].fired is True
        assert decisions[0].outcome is SkillOutcome.INVOKED

    @pytest.mark.asyncio
    async def test_topic_match_but_payload_narrowing_rejects(
        self, monkeypatch, catalog, bus, invoker,
    ):
        """Topic matched (sensor.fired.*); spec wants
        required_sensor_name="test_failure" but event payload says
        sensor_name="voice_command". Catalog narrowing rejects
        -> SKIPPED_PRECONDITION."""
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.*",
                    "required_sensor_name": "test_failure",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        await bus.deliver(
            "sensor.fired.voice", {"sensor_name": "voice_command"},
        )
        # Invoker NOT called -- narrowing rejected.
        assert invoker.calls == []
        assert len(decisions) == 1
        assert decisions[0].fired is False
        assert decisions[0].outcome is (
            SkillOutcome.SKIPPED_PRECONDITION
        )

    @pytest.mark.asyncio
    async def test_decision_no_for_reach_excluding_autonomous(
        self, monkeypatch, catalog, bus, invoker,
    ):
        """Skill reach=OPERATOR_PLUS_MODEL excludes AUTONOMOUS.
        compute_should_fire returns SKIPPED_DISABLED -> no
        invoke."""
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="operator_plus_model",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "test"},
        )
        assert invoker.calls == []
        assert len(decisions) == 1
        assert decisions[0].fired is False
        assert decisions[0].outcome is SkillOutcome.SKIPPED_DISABLED


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_max_invocations_enforced(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                    "max_invocations": 2,
                    "window_s": 100.0,
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        for _ in range(5):
            await bus.deliver(
                "sensor.fired.test", {"sensor_name": "x"},
            )
        # 2 fired, 3 rate-limited
        fired = [d for d in decisions if d.fired]
        skipped = [d for d in decisions if not d.fired]
        assert len(fired) == 2
        assert len(skipped) == 3
        for d in skipped:
            assert d.skip_reason == SKIP_REASON_RATE_LIMIT
        assert len(invoker.calls) == 2

    @pytest.mark.asyncio
    async def test_window_slides(
        self, monkeypatch, catalog, bus, invoker,
    ):
        """After the window elapses, budget refreshes."""
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                    "max_invocations": 1,
                    "window_s": 0.05,  # 50ms
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "x"},
        )
        # Immediate second event -> rate-limited
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "x"},
        )
        assert len(invoker.calls) == 1
        # Sleep past window
        await asyncio.sleep(0.07)
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "x"},
        )
        assert len(invoker.calls) == 2


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    @pytest.mark.asyncio
    async def test_duplicate_within_ttl_skipped(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "drift_detected",
                    "signal_pattern": "coherence.drift_detected",
                    "dedup_key_template": "{drift_kind}",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
            dedup_ttl_s=10.0,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        # Same drift_kind delivered twice
        for _ in range(2):
            await bus.deliver(
                "coherence.drift_detected",
                {"drift_kind": "RECURRENCE_DRIFT"},
            )
        fired = [d for d in decisions if d.fired]
        skipped = [d for d in decisions if not d.fired]
        assert len(fired) == 1
        assert len(skipped) == 1
        assert skipped[0].skip_reason == SKIP_REASON_DEDUP

    @pytest.mark.asyncio
    async def test_different_keys_both_fire(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "drift_detected",
                    "signal_pattern": "coherence.drift_detected",
                    "dedup_key_template": "{drift_kind}",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        await bus.deliver(
            "coherence.drift_detected",
            {"drift_kind": "RECURRENCE_DRIFT"},
        )
        await bus.deliver(
            "coherence.drift_detected",
            {"drift_kind": "POSTURE_LOCKED"},
        )
        assert len(invoker.calls) == 2


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrent_invocations(
        self, monkeypatch, catalog, bus,
    ):
        """Slow invoker + concurrency=1 -> serial execution."""
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        slow_invoker = _CountingInvoker(delay_s=0.05)
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                    "max_invocations": 100,
                    "window_s": 100.0,
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=slow_invoker,
            concurrency=1,
        )
        await obs.start()
        # Fire 3 events concurrently
        t0 = time.monotonic()
        await asyncio.gather(*(
            bus.deliver(
                "sensor.fired.test",
                {"sensor_name": f"s{i}", "uniq": i},
            )
            for i in range(3)
        ))
        elapsed = time.monotonic() - t0
        # 3 serial 50ms invokes >= 0.15s
        assert len(slow_invoker.calls) == 3
        assert elapsed >= 0.13


# ---------------------------------------------------------------------------
# Catalog hot-reload
# ---------------------------------------------------------------------------


class TestHotReload:
    @pytest.mark.asyncio
    async def test_register_after_start_subscribes(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        assert bus.active_subscriptions == 0
        # Register after start -- listener fires + creates a task
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        # Yield to event loop so the create_task runs
        await asyncio.sleep(0.01)
        assert bus.active_subscriptions == 1
        # Deliver an event -> invokes
        await bus.deliver("sensor.fired.test", {"sensor_name": "t"})
        assert len(invoker.calls) == 1

    @pytest.mark.asyncio
    async def test_unregister_after_start_unsubscribes(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        assert bus.active_subscriptions == 1
        catalog.unregister("a")
        await asyncio.sleep(0.01)
        assert bus.active_subscriptions == 0


# ---------------------------------------------------------------------------
# Invoker exception (defense in depth)
# ---------------------------------------------------------------------------


class TestInvokerException:
    @pytest.mark.asyncio
    async def test_invoker_raise_recorded_as_skip_reason(
        self, monkeypatch, catalog, bus,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        bad_invoker = _CountingInvoker(raises=True)
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=bad_invoker,
        )
        decisions = []
        obs.on_decision(lambda d: decisions.append(d))
        await obs.start()
        # Should NOT raise into the bus.
        await bus.deliver("sensor.fired.test", {"sensor_name": "x"})
        assert len(decisions) == 1
        assert decisions[0].fired is False
        assert decisions[0].skip_reason.startswith(
            SKIP_REASON_INVOKER_RAISED,
        )


# ---------------------------------------------------------------------------
# Listener exception
# ---------------------------------------------------------------------------


class TestListenerException:
    @pytest.mark.asyncio
    async def test_buggy_listener_does_not_stall_observer(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        good_calls: List[SkillObserverDecision] = []

        def _bad(_d):
            raise RuntimeError("listener boom")

        def _good(d):
            good_calls.append(d)

        obs.on_decision(_bad)
        obs.on_decision(_good)
        await obs.start()
        await bus.deliver("sensor.fired.test", {"sensor_name": "x"})
        # Despite the bad listener raising, the good listener still
        # received the event.
        assert len(good_calls) == 1
        # And the invoker was still dispatched.
        assert len(invoker.calls) == 1


# ---------------------------------------------------------------------------
# Cancellation propagation
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_during_invoke_propagates(
        self, monkeypatch, catalog, bus,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")

        class _CancellingInvoker:
            calls: List = []

            async def invoke(self, qname, *, args=None,
                             output_preview_chars=400):
                raise asyncio.CancelledError()

        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog,
            invoker=_CancellingInvoker(),
        )
        await obs.start()
        # Cancellation must propagate -- swallowed cancellation
        # would be a bug. Bus.deliver awaits handler.
        with pytest.raises(asyncio.CancelledError):
            await bus.deliver("sensor.fired.test", {"x": 1})


# ---------------------------------------------------------------------------
# Observability snapshots
# ---------------------------------------------------------------------------


class TestObservabilitySnapshots:
    @pytest.mark.asyncio
    async def test_snapshot_helpers(
        self, monkeypatch, catalog, bus, invoker,
    ):
        monkeypatch.setenv("JARVIS_SKILL_OBSERVER_ENABLED", "true")
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                    "dedup_key_template": "{sensor_name}",
                    "max_invocations": 100,
                    "window_s": 100.0,
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        assert obs.subscription_count() == 1
        await bus.deliver("sensor.fired.test", {"sensor_name": "t1"})
        await bus.deliver("sensor.fired.test", {"sensor_name": "t2"})
        snap = obs.rate_limit_snapshot()
        assert snap.get("a") == 2
        assert obs.dedup_cache_size() == 2


# ---------------------------------------------------------------------------
# SkillObserverDecision shape + to_dict
# ---------------------------------------------------------------------------


class TestDecisionDataclass:
    def test_to_dict_minimal(self):
        d = SkillObserverDecision(
            qualified_name="a",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
            triggered_by_signal="sensor.fired.test",
            outcome=SkillOutcome.INVOKED,
            spec_index=0,
            fired=True,
        )
        out = d.to_dict()
        assert out["qualified_name"] == "a"
        assert out["fired"] is True
        assert out["triggered_by_kind"] == "sensor_fired"
        assert out["outcome"] == "invoked"
        assert out["invocation_ok"] is None  # no outcome attached

    def test_to_dict_with_invocation_outcome(self):
        d = SkillObserverDecision(
            qualified_name="a",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
            triggered_by_signal="sensor.fired.test",
            outcome=SkillOutcome.INVOKED,
            spec_index=0,
            fired=True,
            invocation_outcome=SkillInvocationOutcome(
                qualified_name="a", ok=True, duration_ms=12.5,
            ),
        )
        out = d.to_dict()
        assert out["invocation_ok"] is True
        assert out["invocation_duration_ms"] == 12.5


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_default_no_bus_returns_none(self):
        reset_default_observer()
        assert get_default_observer() is None

    def test_get_default_with_bus_initialises(self, bus):
        reset_default_observer()
        obs = get_default_observer(event_bus=bus)
        assert obs is not None
        # Subsequent calls return same instance
        assert get_default_observer() is obs
        assert get_default_observer(event_bus=bus) is obs
