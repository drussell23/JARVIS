"""
Tests for VisionIntelligenceHub — wires 5 dormant intelligence modules
to the 60fps SHM capture feed at configurable per-module rates.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from backend.vision.intelligence.vision_intelligence_hub import (
    IntelligenceModuleAdapter,
    VisionIntelligenceHub,
    _StubModule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(h: int = 64, w: int = 64) -> np.ndarray:
    """Return a small random RGB frame for testing."""
    return np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)


class _FakeModule:
    """Minimal fake that records calls without heavy dependencies."""

    def __init__(self):
        self.process_frame_calls: list = []
        self.update_calls: list = []

    def process_frame(self, frame):
        self.process_frame_calls.append(frame)

    def update(self, frame):
        self.update_calls.append(frame)

    def get_context(self):
        return {"fake": True, "calls": len(self.process_frame_calls)}

    def get_state(self):
        return {"state": "active", "calls": len(self.update_calls)}


class _AsyncFakeModule:
    """Fake module with async methods."""

    def __init__(self):
        self.call_count = 0

    async def process_frame(self, frame):
        self.call_count += 1

    async def get_context(self):
        return {"async_fake": True, "calls": self.call_count}


# ---------------------------------------------------------------------------
# _StubModule
# ---------------------------------------------------------------------------

class TestStubModule:
    def test_process_frame_returns_none(self):
        stub = _StubModule()
        assert stub.process_frame(np.zeros((2, 2, 3))) is None

    def test_update_returns_none(self):
        stub = _StubModule()
        assert stub.update(np.zeros((2, 2, 3))) is None

    def test_get_context_returns_stub_status(self):
        stub = _StubModule()
        assert stub.get_context() == {"status": "stub"}

    def test_get_state_returns_stub_status(self):
        stub = _StubModule()
        assert stub.get_state() == {"status": "stub"}


# ---------------------------------------------------------------------------
# IntelligenceModuleAdapter
# ---------------------------------------------------------------------------

class TestIntelligenceModuleAdapter:
    def test_rate_limiting_respects_min_interval(self):
        """Frames arriving faster than min_interval should be skipped."""
        mod = _FakeModule()
        adapter = IntelligenceModuleAdapter(
            name="test",
            module=mod,
            hz=1.0,  # 1 Hz => min_interval = 1.0s
        )

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            # First call should go through
            loop.run_until_complete(adapter.on_frame(frame, 1, time.time()))
            assert len(mod.process_frame_calls) + len(mod.update_calls) == 1

            # Immediate second call should be rate-limited (skipped)
            loop.run_until_complete(adapter.on_frame(frame, 2, time.time()))
            assert len(mod.process_frame_calls) + len(mod.update_calls) == 1
        finally:
            loop.close()

    def test_high_rate_allows_rapid_frames(self):
        """At a very high Hz, frames should not be rate-limited."""
        mod = _FakeModule()
        adapter = IntelligenceModuleAdapter(
            name="test",
            module=mod,
            hz=1000.0,  # 1000 Hz => min_interval = 0.001s
        )

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            for i in range(5):
                loop.run_until_complete(adapter.on_frame(frame, i, time.time()))
            total_calls = len(mod.process_frame_calls) + len(mod.update_calls)
            assert total_calls == 5
        finally:
            loop.close()

    def test_circuit_breaker_disables_after_max_errors(self):
        """After max_errors consecutive failures, the adapter should stop dispatching."""

        class _ErrorModule:
            def process_frame(self, frame):
                raise RuntimeError("boom")

            def get_context(self):
                return {}

            def get_state(self):
                return {}

        mod = _ErrorModule()
        adapter = IntelligenceModuleAdapter(
            name="breaker_test",
            module=mod,
            hz=10000.0,
            max_errors=3,
        )

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            for i in range(5):
                loop.run_until_complete(adapter.on_frame(frame, i, time.time()))

            assert adapter.error_count >= 3
            assert adapter.disabled
        finally:
            loop.close()

    def test_get_context_delegates_to_module(self):
        mod = _FakeModule()
        adapter = IntelligenceModuleAdapter(name="ctx", module=mod, hz=1.0)
        ctx = adapter.get_context()
        assert "fake" in ctx

    def test_get_context_returns_stub_when_disabled(self):
        """A disabled adapter should return an error context."""

        class _ErrorModule:
            def process_frame(self, frame):
                raise RuntimeError("boom")

            def get_context(self):
                return {"real": True}

            def get_state(self):
                return {}

        mod = _ErrorModule()
        adapter = IntelligenceModuleAdapter(
            name="disabled_ctx",
            module=mod,
            hz=10000.0,
            max_errors=1,
        )

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            # Trigger circuit breaker
            loop.run_until_complete(adapter.on_frame(frame, 1, time.time()))
            assert adapter.disabled

            ctx = adapter.get_context()
            assert ctx.get("status") == "disabled"
        finally:
            loop.close()

    def test_prefers_process_frame_over_update(self):
        """When module has process_frame, adapter should use it."""
        mod = _FakeModule()
        adapter = IntelligenceModuleAdapter(name="pf", module=mod, hz=10000.0)

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter.on_frame(frame, 1, time.time()))
            assert len(mod.process_frame_calls) == 1
            assert len(mod.update_calls) == 0
        finally:
            loop.close()

    def test_falls_back_to_update_when_no_process_frame(self):
        """When module only has update(), adapter should use it."""

        class _UpdateOnly:
            def __init__(self):
                self.calls = 0

            def update(self, frame):
                self.calls += 1

            def get_context(self):
                return {}

            def get_state(self):
                return {}

        mod = _UpdateOnly()
        # Remove process_frame if it existed
        adapter = IntelligenceModuleAdapter(name="uo", module=mod, hz=10000.0)

        frame = _make_frame()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(adapter.on_frame(frame, 1, time.time()))
            assert mod.calls == 1
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# VisionIntelligenceHub
# ---------------------------------------------------------------------------

class TestVisionIntelligenceHub:
    def test_hub_registers_all_5_modules(self):
        """Hub should have exactly 5 adapters, one per module."""
        hub = VisionIntelligenceHub()
        assert len(hub._adapters) == 5

    def test_hub_adapter_names(self):
        """All expected module names should be present."""
        hub = VisionIntelligenceHub()
        names = {a.name for a in hub._adapters}
        expected = {
            "activity_recognition",
            "anomaly_detection",
            "goal_inference",
            "predictive_precomputation",
            "intervention_decision",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_frame_dispatch_to_all_modules(self):
        """on_frame should dispatch to all module adapters."""
        hub = VisionIntelligenceHub()

        # Replace adapters with tracked fakes
        fakes = []
        for adapter in hub._adapters:
            fake = _FakeModule()
            adapter._module = fake
            adapter._dispatch_fn = fake.process_frame
            adapter.disabled = False
            adapter.error_count = 0
            adapter._last_dispatch = 0.0  # Reset rate limiter
            fakes.append(fake)

        frame = _make_frame()
        await hub.on_frame(frame, 1, time.time())

        for fake in fakes:
            total = len(fake.process_frame_calls) + len(fake.update_calls)
            assert total >= 1, "Each module should receive at least one frame"

    @pytest.mark.asyncio
    async def test_context_aggregation(self):
        """get_intelligence_context should aggregate context from all modules."""
        hub = VisionIntelligenceHub()
        ctx = await hub.get_intelligence_context()

        assert isinstance(ctx, dict)
        # Should have a key for each module
        assert "activity_recognition" in ctx
        assert "anomaly_detection" in ctx
        assert "goal_inference" in ctx
        assert "predictive_precomputation" in ctx
        assert "intervention_decision" in ctx

    @pytest.mark.asyncio
    async def test_on_frame_tolerates_module_errors(self):
        """If one module raises, others should still receive the frame."""
        hub = VisionIntelligenceHub()

        good_fake = _FakeModule()
        # First adapter: error module
        hub._adapters[0]._module = type(
            "_Err", (), {
                "process_frame": lambda self, f: (_ for _ in ()).throw(RuntimeError("fail")),
                "get_context": lambda self: {},
                "get_state": lambda self: {},
            }
        )()
        hub._adapters[0]._dispatch_fn = hub._adapters[0]._module.process_frame

        # Second adapter: good module
        hub._adapters[1]._module = good_fake
        hub._adapters[1]._dispatch_fn = good_fake.process_frame
        hub._adapters[1]._last_dispatch = 0.0
        hub._adapters[1].disabled = False

        frame = _make_frame()
        # Should not raise
        await hub.on_frame(frame, 1, time.time())

        # Good module should still have received the frame
        assert len(good_fake.process_frame_calls) >= 1

    def test_hub_singleton(self):
        """Hub should be a singleton (same instance each time)."""
        hub1 = VisionIntelligenceHub()
        hub2 = VisionIntelligenceHub()
        assert hub1 is hub2

    @pytest.mark.asyncio
    async def test_stub_modules_dont_break_hub(self):
        """Even with all stub modules, the hub should function."""
        hub = VisionIntelligenceHub()
        # Force all modules to stubs
        for adapter in hub._adapters:
            stub = _StubModule()
            adapter._module = stub
            adapter._dispatch_fn = stub.process_frame
            adapter.disabled = False
            adapter._last_dispatch = 0.0

        frame = _make_frame()
        await hub.on_frame(frame, 1, time.time())

        ctx = await hub.get_intelligence_context()
        assert isinstance(ctx, dict)
        for key in ctx:
            assert ctx[key].get("status") == "stub"


# ---------------------------------------------------------------------------
# FramePipeline subscriber integration
# ---------------------------------------------------------------------------

class TestFramePipelineSubscriber:
    def test_subscribe_adds_callback(self):
        """subscribe() should add a callback to the subscriber list."""
        from backend.vision.realtime.frame_pipeline import FramePipeline

        pipeline = FramePipeline(use_sck=False)
        cb = MagicMock()
        pipeline.subscribe(cb)
        assert cb in pipeline._frame_subscribers

    def test_subscribe_multiple(self):
        """Multiple subscribers should all be registered."""
        from backend.vision.realtime.frame_pipeline import FramePipeline

        pipeline = FramePipeline(use_sck=False)
        cbs = [MagicMock() for _ in range(3)]
        for cb in cbs:
            pipeline.subscribe(cb)
        assert len(pipeline._frame_subscribers) == 3


# ---------------------------------------------------------------------------
# Teardown: reset singleton between test runs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_hub_singleton():
    """Reset the singleton so each test class gets a fresh hub."""
    VisionIntelligenceHub._instance = None
    yield
    VisionIntelligenceHub._instance = None
