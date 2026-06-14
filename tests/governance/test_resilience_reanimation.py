"""Unit tests for the Cybernetic Reanimation layer (Phase C).

These import ONLY the standalone module — never unified_supervisor (which is
sandbox-blocked by split_brain_guard). Bus + registry are fakes.
"""
import asyncio
import pytest

from backend.core.ouroboros.governance.resilience_reanimation import (
    EventActivationDispatcher,
)


class FakeBus:
    def __init__(self):
        self.handlers = []
    def subscribe(self, handler):
        self.handlers.append(handler)
    async def fire(self, event):
        for h in self.handlers:
            r = h(event)
            if asyncio.iscoroutine(r):
                await r


class FakeEvent:
    def __init__(self, type_value):
        self.event_type = type(self).Type(type_value)
    class Type:
        def __init__(self, value): self.value = value


class FakeDescriptor:
    def __init__(self, name, trigger_events):
        self.name = name
        self.activation_contract = type("C", (), {"trigger_events": trigger_events})()


class FakeRegistry:
    def __init__(self, descriptors):
        self._descs = descriptors
        self.activated = []
        self.fail_on = set()
    def iter_event_driven(self):
        return list(self._descs)
    async def activate_service(self, name):
        if name in self.fail_on:
            raise RuntimeError(f"boom:{name}")
        self.activated.append(name)
        return True


@pytest.mark.asyncio
async def test_dispatch_activates_matching_service():
    bus = FakeBus()
    reg = FakeRegistry([FakeDescriptor("grace", ["resource_pressure"])])
    d = EventActivationDispatcher(bus, reg)
    d.start()
    await bus.fire(FakeEvent("resource_pressure"))
    assert reg.activated == ["grace"]


@pytest.mark.asyncio
async def test_non_matching_event_does_not_activate():
    bus = FakeBus()
    reg = FakeRegistry([FakeDescriptor("grace", ["resource_pressure"])])
    EventActivationDispatcher(bus, reg).start()
    await bus.fire(FakeEvent("phase_start"))
    assert reg.activated == []


@pytest.mark.asyncio
async def test_one_failing_activation_does_not_block_others():
    bus = FakeBus()
    reg = FakeRegistry([
        FakeDescriptor("bad", ["resource_pressure"]),
        FakeDescriptor("good", ["resource_pressure"]),
    ])
    reg.fail_on = {"bad"}
    EventActivationDispatcher(bus, reg).start()
    await bus.fire(FakeEvent("resource_pressure"))
    assert reg.activated == ["good"]  # bad failed, good still ran


# ---------------------------------------------------------------------------
# C.2 — PressureSignalEmitter tests
# ---------------------------------------------------------------------------
from backend.core.ouroboros.governance.resilience_reanimation import PressureSignalEmitter


@pytest.mark.asyncio
async def test_emitter_edge_triggers_once_on_crossing():
    emitted = []
    levels = {"mem": [0.5, 0.95, 0.96, 0.4]}   # below, cross, stay, drop
    seq = iter(levels["mem"])
    def sampler(): return {"mem": next(seq)}
    em = PressureSignalEmitter(
        sampler=sampler,
        emit=lambda etype, payload: emitted.append((etype, payload)),
        thresholds={"mem": 0.9},
        signal_event={"mem": "resource_pressure"},
    )
    for _ in range(4):
        await em.tick()
    # crossing 0.5->0.95 emits once; 0.95->0.96 (stay above) no emit; drop resets
    assert [e[0] for e in emitted] == ["resource_pressure"]


@pytest.mark.asyncio
async def test_emitter_reemits_after_drop_then_recross():
    emitted = []
    seq = iter([0.95, 0.4, 0.95])
    em = PressureSignalEmitter(
        sampler=lambda: {"mem": next(seq)},
        emit=lambda etype, payload: emitted.append(etype),
        thresholds={"mem": 0.9},
        signal_event={"mem": "resource_pressure"},
    )
    for _ in range(3):
        await em.tick()
    assert emitted == ["resource_pressure", "resource_pressure"]


@pytest.mark.asyncio
async def test_emitter_failsoft_on_sampler_error():
    def sampler(): raise RuntimeError("probe down")
    em = PressureSignalEmitter(sampler=sampler, emit=lambda *a: None,
                               thresholds={"mem": 0.9}, signal_event={"mem": "resource_pressure"})
    await em.tick()  # must not raise


# ---------------------------------------------------------------------------
# C.3 — organ adapters + ReanimationLayer tests
# ---------------------------------------------------------------------------
from backend.core.ouroboros.governance.resilience_reanimation import (
    GracefulDegradationAdapter,
    LoadSheddingAdapter,
    AutoScalingAdapter,
    AnomalyDetectorAdapter,
    ProcessHealthPredictorAdapter,
    SelfHealingAdapter,
    CircuitBreakerAdapter,
    ReanimationLayer,
)


class MockOrgan:
    """Records every method call as (name, args, kwargs)."""

    def __init__(self, **returns):
        self.calls = []
        self._returns = returns

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))
        return self._returns.get(name)

    # sync surfaces
    def record_load(self, load):
        return self._rec("record_load", load)

    def record_metrics(self, *a, **k):
        return self._rec("record_metrics", *a, **k)

    # async surfaces — return whatever was configured
    async def _check_resources(self):
        return self._rec("_check_resources")

    async def evaluate(self):
        return self._rec("evaluate")

    async def record_observation(self, *a, **k):
        return self._rec("record_observation", *a, **k)

    async def check_and_remediate(self, *a, **k):
        return self._rec("check_and_remediate", *a, **k)

    def record_failure(self, *a, **k):
        return self._rec("record_failure", *a, **k)

    def record_success(self, *a, **k):
        return self._rec("record_success", *a, **k)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.mark.asyncio
async def test_graceful_degradation_adapter_calls_check_resources():
    organ = MockOrgan()
    ad = GracefulDegradationAdapter(organ)
    await ad.on_event({"signal": "mem", "level": 0.95})
    assert organ.names() == ["_check_resources"]


@pytest.mark.asyncio
async def test_load_shedding_adapter_records_load_from_payload():
    organ = MockOrgan()
    ad = LoadSheddingAdapter(organ)
    await ad.on_event({"signal": "cpu", "level": 0.92})
    assert organ.calls == [("record_load", (0.92,), {})]


@pytest.mark.asyncio
async def test_autoscaling_adapter_records_then_evaluates():
    organ = MockOrgan(evaluate="decision")
    ad = AutoScalingAdapter(organ)
    await ad.on_event({"signal": "cpu", "level": 0.88, "memory": 0.7})
    names = organ.names()
    assert names == ["record_metrics", "evaluate"]
    # record_metrics gets cpu+mem fractions scaled to percent
    rec = organ.calls[0]
    assert rec[2].get("cpu_percent") == pytest.approx(88.0)
    assert rec[2].get("memory_percent") == pytest.approx(70.0)


@pytest.mark.asyncio
async def test_anomaly_adapter_records_observation():
    organ = MockOrgan()
    ad = AnomalyDetectorAdapter(organ)
    await ad.on_event({"category": "latency", "features": {"p99": 1200.0}})
    assert organ.calls == [
        ("record_observation", ("latency", {"p99": 1200.0}), {})
    ]


@pytest.mark.asyncio
async def test_process_health_predictor_adapter_records_metrics():
    organ = MockOrgan(record_metrics={"health_score": 0.4, "failure_probability": 0.6})
    ad = ProcessHealthPredictorAdapter(organ)
    await ad.on_event({"component": "worker-1", "metrics": {"cpu": 95.0}})
    assert organ.calls[0] == ("record_metrics", ("worker-1", {"cpu": 95.0}), {})


@pytest.mark.asyncio
async def test_self_healing_adapter_remediates_from_payload():
    organ = MockOrgan()
    ad = SelfHealingAdapter(organ)
    await ad.on_event({
        "component": "worker-1",
        "health_score": 0.2,
        "failure_probability": 0.8,
    })
    assert organ.calls == [
        ("check_and_remediate", ("worker-1", 0.2, 0.8), {})
    ]


@pytest.mark.asyncio
async def test_circuit_breaker_adapter_records_failure_on_degraded():
    organ = MockOrgan()
    ad = CircuitBreakerAdapter(organ)
    await ad.on_event({"component": "worker-1", "degraded": True})
    assert organ.names() == ["record_failure"]


@pytest.mark.asyncio
async def test_circuit_breaker_adapter_records_success_when_not_degraded():
    organ = MockOrgan()
    ad = CircuitBreakerAdapter(organ)
    await ad.on_event({"component": "worker-1", "degraded": False})
    assert organ.names() == ["record_success"]


@pytest.mark.asyncio
async def test_adapter_failsoft_swallows_organ_error():
    class Boom:
        def record_load(self, load):
            raise RuntimeError("organ down")
    ad = LoadSheddingAdapter(Boom())
    # must not raise
    await ad.on_event({"signal": "cpu", "level": 0.99})


# ---- ReanimationLayer ------------------------------------------------------


class _Meta:
    def __init__(self, metadata):
        self.metadata = metadata


class FakeEventWithMeta:
    def __init__(self, type_value, metadata=None):
        self.event_type = type("T", (), {"value": type_value})()
        self.metadata = metadata or {}


class RecordingRegistry:
    def __init__(self):
        self.registered = []   # ServiceDescriptor-like

    def register(self, desc):
        self.registered.append(desc)

    def iter_event_driven(self):
        return list(self.registered)

    async def activate_service(self, name):
        return True


def _seven_mock_organs():
    return {
        "graceful_degradation": MockOrgan(),
        "load_shedding": MockOrgan(),
        "auto_scaling": MockOrgan(evaluate="d"),
        "anomaly_detector": MockOrgan(),
        "health_predictor": MockOrgan(record_metrics={"health_score": 0.5}),
        "self_healing": MockOrgan(),
        "circuit_breaker": MockOrgan(),
    }


@pytest.mark.asyncio
async def test_layer_wire_subscribes_and_registers_seven_contracts():
    bus = FakeBus()
    reg = RecordingRegistry()
    organs = _seven_mock_organs()
    layer = ReanimationLayer(bus, reg, organs)
    layer.wire()
    # 7 descriptors registered, each carrying an activation_contract w/ triggers
    assert len(reg.registered) == 7
    for d in reg.registered:
        assert getattr(d, "activation_contract", None) is not None
        assert getattr(d.activation_contract, "trigger_events", None)
    # bus has at least one subscriber (the adapter fan-out handler)
    assert bus.handlers


@pytest.mark.asyncio
async def test_layer_resource_pressure_drives_pressure_tier_adapters():
    bus = FakeBus()
    reg = RecordingRegistry()
    organs = _seven_mock_organs()
    layer = ReanimationLayer(bus, reg, organs)
    layer.wire()
    await bus.fire(FakeEventWithMeta("resource_pressure",
                                     {"signal": "cpu", "level": 0.9, "memory": 0.8}))
    assert "_check_resources" in organs["graceful_degradation"].names()
    assert "record_load" in organs["load_shedding"].names()
    assert "evaluate" in organs["auto_scaling"].names()
    # anomaly organ should NOT fire on a pressure event
    assert organs["anomaly_detector"].names() == []


@pytest.mark.asyncio
async def test_layer_per_organ_flag_off_skips_that_organ():
    bus = FakeBus()
    reg = RecordingRegistry()
    organs = _seven_mock_organs()
    layer = ReanimationLayer(
        bus, reg, organs,
        enabled_flags={"load_shedding": False},
    )
    layer.wire()
    # disabled organ not registered
    names = [d.name for d in reg.registered]
    assert "load_shedding" not in names
    assert len(reg.registered) == 6
    # and it never fires
    await bus.fire(FakeEventWithMeta("resource_pressure",
                                     {"signal": "cpu", "level": 0.9}))
    assert organs["load_shedding"].names() == []
    assert "_check_resources" in organs["graceful_degradation"].names()


@pytest.mark.asyncio
async def test_layer_anomaly_event_drives_anomaly_adapter():
    bus = FakeBus()
    reg = RecordingRegistry()
    organs = _seven_mock_organs()
    ReanimationLayer(bus, reg, organs).wire()
    await bus.fire(FakeEventWithMeta(
        "anomaly_detected", {"category": "latency", "features": {"p99": 999.0}}))
    assert "record_observation" in organs["anomaly_detector"].names()


@pytest.mark.asyncio
async def test_layer_component_degraded_drives_heal_tier():
    bus = FakeBus()
    reg = RecordingRegistry()
    organs = _seven_mock_organs()
    ReanimationLayer(bus, reg, organs).wire()
    await bus.fire(FakeEventWithMeta(
        "component_degraded",
        {"component": "w1", "health_score": 0.2, "failure_probability": 0.8,
         "degraded": True}))
    assert "record_metrics" in organs["health_predictor"].names()
    assert "check_and_remediate" in organs["self_healing"].names()
    assert "record_failure" in organs["circuit_breaker"].names()
