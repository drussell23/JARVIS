"""Tests for the unified telemetry contract v1."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch
from backend.core.telemetry_contract import (
    TelemetryEnvelope,
    EventRegistry,
    SequenceCounter,
    ENVELOPE_VERSION,
    TelemetryBus,
    get_telemetry_bus,
)


class TestTelemetryEnvelope:
    def test_create_envelope(self):
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="jprime_lifecycle_controller",
            trace_id="t1",
            span_id="s1",
            partition_key="lifecycle",
            payload={"from_state": "UNKNOWN", "to_state": "PROBING"},
        )
        assert env.envelope_version == ENVELOPE_VERSION
        assert env.event_schema == "lifecycle.transition@1.0.0"
        assert env.source == "jprime_lifecycle_controller"
        assert env.trace_id == "t1"
        assert env.span_id == "s1"
        assert env.partition_key == "lifecycle"
        assert env.payload["from_state"] == "UNKNOWN"
        assert env.event_id
        assert env.emitted_at > 0
        assert env.severity == "info"

    def test_idempotency_key_deterministic(self):
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="lifecycle",
            payload={},
        )
        expected = f"lifecycle.transition@1.0.0:t1:{env.sequence}"
        assert env.idempotency_key == expected

    def test_envelope_is_frozen(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            payload={},
        )
        with pytest.raises(AttributeError):
            env.trace_id = "modified"

    def test_to_dict(self):
        env = TelemetryEnvelope.create(
            event_schema="fault.raised@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="recovery",
            severity="error",
            payload={"fault_class": "connection_refused"},
        )
        d = env.to_dict()
        assert d["envelope_version"] == ENVELOPE_VERSION
        assert d["event_schema"] == "fault.raised@1.0.0"
        assert d["severity"] == "error"
        assert d["payload"]["fault_class"] == "connection_refused"

    def test_causal_parent_id_optional(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            causal_parent_id="parent-s1",
            payload={},
        )
        assert env.causal_parent_id == "parent-s1"

    def test_op_id_optional(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            op_id="op-abc-jarvis",
            payload={},
        )
        assert env.op_id == "op-abc-jarvis"

    def test_defaults_none_for_optional_fields(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            payload={},
        )
        assert env.causal_parent_id is None
        assert env.op_id is None


class TestSequenceCounter:
    def test_monotonic_per_partition(self):
        counter = SequenceCounter()
        assert counter.next("lifecycle") == 1
        assert counter.next("lifecycle") == 2
        assert counter.next("reasoning") == 1
        assert counter.next("lifecycle") == 3

    def test_independent_partitions(self):
        counter = SequenceCounter()
        counter.next("a")
        counter.next("a")
        counter.next("b")
        assert counter.next("a") == 3
        assert counter.next("b") == 2


class TestEventRegistry:
    def test_register_and_validate(self):
        registry = EventRegistry()
        registry.register("lifecycle.transition@1.0.0")
        assert registry.is_registered("lifecycle.transition@1.0.0") is True

    def test_unknown_schema_not_registered(self):
        registry = EventRegistry()
        assert registry.is_registered("unknown.event@1.0.0") is False

    def test_parse_schema(self):
        name, version = EventRegistry.parse_schema("lifecycle.transition@1.0.0")
        assert name == "lifecycle.transition"
        assert version == "1.0.0"

    def test_parse_invalid_schema(self):
        with pytest.raises(ValueError):
            EventRegistry.parse_schema("no_version")

    def test_major_version_compatible(self):
        registry = EventRegistry()
        registry.register("lifecycle.transition@1.0.0")
        assert registry.is_compatible("lifecycle.transition@1.0.0") is True
        assert registry.is_compatible("lifecycle.transition@1.1.0") is True
        assert registry.is_compatible("lifecycle.transition@1.99.0") is True
        assert registry.is_compatible("lifecycle.transition@2.0.0") is False

    def test_default_v1_events_registered(self):
        registry = EventRegistry.with_v1_defaults()
        assert registry.is_registered("lifecycle.transition@1.0.0")
        assert registry.is_registered("lifecycle.health@1.0.0")
        assert registry.is_registered("reasoning.activation@1.0.0")
        assert registry.is_registered("reasoning.decision@1.0.0")
        assert registry.is_registered("scheduler.graph_state@1.0.0")
        assert registry.is_registered("scheduler.unit_state@1.0.0")
        assert registry.is_registered("recovery.attempt@1.0.0")
        assert registry.is_registered("fault.raised@1.0.0")
        assert registry.is_registered("fault.resolved@1.0.0")

    def test_v1_has_exactly_9_events(self):
        registry = EventRegistry.with_v1_defaults()
        assert len(registry._schemas) == 9


class TestTelemetryBus:
    @pytest.mark.asyncio
    async def test_emit_and_subscribe(self):
        bus = TelemetryBus(max_queue=100)
        received = []
        async def handler(env):
            received.append(env)
        bus.subscribe("lifecycle.*", handler)
        await bus.start()
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={"test": True},
        )
        bus.emit(env)
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(received) == 1
        assert received[0].event_id == env.event_id

    @pytest.mark.asyncio
    async def test_pattern_matching(self):
        bus = TelemetryBus(max_queue=100)
        lifecycle_events = []
        reasoning_events = []
        async def lh(env): lifecycle_events.append(env)
        async def rh(env): reasoning_events.append(env)
        bus.subscribe("lifecycle.*", lh)
        bus.subscribe("reasoning.*", rh)
        await bus.start()
        bus.emit(TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        ))
        bus.emit(TelemetryEnvelope.create(
            event_schema="reasoning.decision@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="reasoning", payload={},
        ))
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(lifecycle_events) == 1
        assert len(reasoning_events) == 1

    @pytest.mark.asyncio
    async def test_dedup_by_idempotency_key(self):
        bus = TelemetryBus(max_queue=100, dedup_window_s=5.0)
        received = []
        async def handler(env): received.append(env)
        bus.subscribe("*", handler)
        await bus.start()
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env)
        bus.emit(env)  # Duplicate
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_backpressure_drops_non_critical(self):
        bus = TelemetryBus(max_queue=2)
        bus.subscribe("*", AsyncMock())
        # Don't start consumer — let queue fill
        env1 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        env2 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env1)
        bus.emit(env2)
        # Queue full — next non-critical should drop
        env3 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t3", span_id="s3",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env3)
        assert bus.dropped_count > 0

    @pytest.mark.asyncio
    async def test_dead_letter_on_consumer_error(self):
        bus = TelemetryBus(max_queue=100)
        async def failing_handler(env):
            raise ValueError("consumer exploded")
        bus.subscribe("lifecycle.*", failing_handler)
        await bus.start()
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env)
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(bus.dead_letter) == 1
        assert bus.dead_letter[0]["error"] == "consumer exploded"

    @pytest.mark.asyncio
    async def test_wildcard_subscribe(self):
        bus = TelemetryBus(max_queue=100)
        all_events = []
        async def catch_all(env): all_events.append(env)
        bus.subscribe("*", catch_all)
        await bus.start()
        bus.emit(TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        ))
        bus.emit(TelemetryEnvelope.create(
            event_schema="fault.raised@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="recovery", payload={},
        ))
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(all_events) == 2

    @pytest.mark.asyncio
    async def test_get_metrics(self):
        bus = TelemetryBus(max_queue=100)
        m = bus.get_metrics()
        assert "emitted" in m
        assert "delivered" in m
        assert "dropped" in m
        assert "deduped" in m
        assert "dead_letter" in m
        assert "queue_size" in m


class TestChainTelemetryEnvelope:
    @pytest.mark.asyncio
    async def test_proactive_detection_emits_envelope(self):
        bus = TelemetryBus(max_queue=100)
        received = []
        async def handler(env):
            received.append(env)
        bus.subscribe("reasoning.*", handler)

        with patch("backend.core.reasoning_chain_orchestrator.get_telemetry_bus", return_value=bus):
            await bus.start()
            from backend.core.reasoning_chain_orchestrator import ChainTelemetry
            ct = ChainTelemetry()
            event = await ct.emit_proactive_detection(
                trace_id="t1", command="start my day", is_proactive=True,
                confidence=0.92, signals=["workflow_trigger"], latency_ms=15.0,
            )
            await asyncio.sleep(0.1)
            await bus.stop()

        # Original dict still returned
        assert event["event"] == "proactive_detection"
        # Envelope emitted to bus
        assert len(received) == 1
        assert received[0].event_schema == "reasoning.decision@1.0.0"
        assert received[0].trace_id == "t1"
        assert received[0].source == "reasoning_chain"
        assert received[0].partition_key == "reasoning"


class TestTelemetryBusSingleton:
    def test_singleton(self):
        import backend.core.telemetry_contract as mod
        mod._bus_instance = None
        b1 = get_telemetry_bus()
        b2 = get_telemetry_bus()
        assert b1 is b2
        mod._bus_instance = None
