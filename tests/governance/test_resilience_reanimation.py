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
