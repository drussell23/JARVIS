"""
Integration tests for the SupervisorEventBus -> CliRenderer pipeline.

Validates end-to-end event flow: events emitted through the bus arrive at
renderers (PlainCliRenderer, JsonCliRenderer) correctly, with proper
verbosity filtering, fault isolation, backpressure, and lifecycle semantics.
"""

from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from typing import List
from unittest.mock import patch

import pytest

from unified_supervisor import (
    JsonCliRenderer,
    PlainCliRenderer,
    SupervisorEvent,
    SupervisorEventBus,
    SupervisorEventSeverity,
    SupervisorEventType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_type: SupervisorEventType = SupervisorEventType.LOG,
    message: str = "test event",
    severity: SupervisorEventSeverity = SupervisorEventSeverity.INFO,
    phase: str = "",
    component: str = "",
    duration_ms: float = 0.0,
    progress_pct: float = -1,
    metadata: tuple = (),
    correlation_id: str = "",
) -> SupervisorEvent:
    return SupervisorEvent(
        event_type=event_type,
        timestamp=time.time(),
        message=message,
        severity=severity,
        phase=phase,
        component=component,
        duration_ms=duration_ms,
        progress_pct=progress_pct,
        metadata=metadata,
        correlation_id=correlation_id,
    )


class CapturingHandler:
    """Simple handler that records every event it receives."""

    def __init__(self):
        self.events: List[SupervisorEvent] = []

    def __call__(self, event: SupervisorEvent) -> None:
        self.events.append(event)


class CapturingPlainRenderer(PlainCliRenderer):
    """PlainCliRenderer subclass that captures output to a list instead of stdout."""

    def __init__(self, verbosity: str = "ops"):
        super().__init__(verbosity=verbosity)
        self.lines: List[str] = []

    def handle_event(self, event: SupervisorEvent) -> None:
        if not self._running or not self.should_display(event):
            return
        try:
            elapsed = (event.timestamp - self._start_time) * 1000
            parts = [
                f"[+{elapsed:>7.0f}ms]",
                f"[{event.event_type.value.upper()}]",
            ]
            if event.phase:
                parts.append(f"[{event.phase}]")
            if event.component:
                parts.append(f"[{event.component}]")
            parts.append(event.message)
            if event.duration_ms > 0:
                parts.append(f"({event.duration_ms:.1f}ms)")
            if event.progress_pct >= 0:
                parts.append(f"[{event.progress_pct:.0f}%]")
            self.lines.append(" ".join(parts))
        except Exception:
            pass


class CapturingJsonRenderer(JsonCliRenderer):
    """JsonCliRenderer subclass that captures output to a list instead of stdout."""

    def __init__(self, verbosity: str = "ops"):
        super().__init__(verbosity=verbosity)
        self.outputs: List[str] = []

    def handle_event(self, event: SupervisorEvent) -> None:
        if not self._running or not self.should_display(event):
            return
        try:
            self.outputs.append(
                json.dumps(event.to_json_dict(), separators=(",", ":"))
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestEventBusRendererPipeline:
    """End-to-end pipeline: events emitted through bus arrive at renderers."""

    async def test_plain_renderer_receives_events(self, started_event_bus):
        """Subscribe PlainCliRenderer to bus, emit event, verify it received and formatted it."""
        renderer = CapturingPlainRenderer(verbosity="debug")
        renderer.start()
        started_event_bus.subscribe(renderer.handle_event)

        try:
            event = _make_event(
                event_type=SupervisorEventType.COMPONENT_STATUS,
                message="Backend is healthy",
                phase="startup",
                component="jarvis-body",
            )
            started_event_bus.emit(event)

            # Wait for async delivery
            for _ in range(50):
                if renderer.lines:
                    break
                await asyncio.sleep(0.05)

            assert len(renderer.lines) == 1
            line = renderer.lines[0]
            assert "[COMPONENT_STATUS]" in line
            assert "[startup]" in line
            assert "[jarvis-body]" in line
            assert "Backend is healthy" in line
        finally:
            started_event_bus.unsubscribe(renderer.handle_event)
            renderer.stop()

    async def test_json_renderer_outputs_valid_json(self, started_event_bus):
        """Subscribe JsonCliRenderer, emit event, verify valid JSON output per event."""
        renderer = CapturingJsonRenderer(verbosity="debug")
        renderer.start()
        started_event_bus.subscribe(renderer.handle_event)

        try:
            event = _make_event(
                event_type=SupervisorEventType.HEALTH_CHECK,
                message="All systems nominal",
                severity=SupervisorEventSeverity.SUCCESS,
                component="jarvis-prime",
                metadata=(("latency_ms", 42), ("status", "ok")),
            )
            started_event_bus.emit(event)

            for _ in range(50):
                if renderer.outputs:
                    break
                await asyncio.sleep(0.05)

            assert len(renderer.outputs) == 1
            parsed = json.loads(renderer.outputs[0])
            assert parsed["event_type"] == "health_check"
            assert parsed["message"] == "All systems nominal"
            assert parsed["severity"] == "success"
            assert parsed["component"] == "jarvis-prime"
            assert parsed["metadata"]["latency_ms"] == 42
            assert parsed["metadata"]["status"] == "ok"
        finally:
            started_event_bus.unsubscribe(renderer.handle_event)
            renderer.stop()

    async def test_multiple_renderers_both_receive(self, started_event_bus):
        """Subscribe both Plain and JSON renderers, emit event, verify both receive same event."""
        plain = CapturingPlainRenderer(verbosity="debug")
        json_r = CapturingJsonRenderer(verbosity="debug")
        plain.start()
        json_r.start()
        started_event_bus.subscribe(plain.handle_event)
        started_event_bus.subscribe(json_r.handle_event)

        try:
            event = _make_event(
                event_type=SupervisorEventType.METRIC,
                message="CPU usage: 45%",
                component="system",
            )
            started_event_bus.emit(event)

            for _ in range(50):
                if plain.lines and json_r.outputs:
                    break
                await asyncio.sleep(0.05)

            assert len(plain.lines) == 1
            assert "CPU usage: 45%" in plain.lines[0]

            assert len(json_r.outputs) == 1
            parsed = json.loads(json_r.outputs[0])
            assert parsed["message"] == "CPU usage: 45%"
        finally:
            started_event_bus.unsubscribe(plain.handle_event)
            started_event_bus.unsubscribe(json_r.handle_event)
            plain.stop()
            json_r.stop()

    async def test_crashing_renderer_doesnt_break_bus(self, started_event_bus):
        """One renderer raises exception, bus and other renderers still work."""
        def crashing_handler(event: SupervisorEvent) -> None:
            raise RuntimeError("Renderer exploded!")

        healthy_recorder = CapturingHandler()

        started_event_bus.subscribe(crashing_handler)
        started_event_bus.subscribe(healthy_recorder)

        try:
            event = _make_event(message="Should survive crash")
            started_event_bus.emit(event)

            for _ in range(50):
                if healthy_recorder.events:
                    break
                await asyncio.sleep(0.05)

            assert len(healthy_recorder.events) == 1
            assert healthy_recorder.events[0].message == "Should survive crash"
        finally:
            started_event_bus.unsubscribe(crashing_handler)
            started_event_bus.unsubscribe(healthy_recorder)

    async def test_verbosity_filtering_end_to_end(self, started_event_bus):
        """Set renderer to 'summary' mode, emit debug-level event, verify it is filtered out."""
        renderer = CapturingPlainRenderer(verbosity="summary")
        renderer.start()
        started_event_bus.subscribe(renderer.handle_event)

        try:
            # DEBUG severity event should be filtered by summary mode
            debug_event = _make_event(
                event_type=SupervisorEventType.LOG,
                message="Debug noise",
                severity=SupervisorEventSeverity.DEBUG,
            )
            started_event_bus.emit(debug_event)

            # INFO-level LOG event should also be filtered by summary mode
            # (summary only shows phase transitions, completion, critical, errors)
            info_event = _make_event(
                event_type=SupervisorEventType.LOG,
                message="Info noise",
                severity=SupervisorEventSeverity.INFO,
            )
            started_event_bus.emit(info_event)

            # PHASE_START should pass through summary filter
            phase_event = _make_event(
                event_type=SupervisorEventType.PHASE_START,
                message="Phase 1 starting",
                phase="preflight",
            )
            started_event_bus.emit(phase_event)

            # Wait for all events to be processed
            for _ in range(50):
                if renderer.lines:
                    break
                await asyncio.sleep(0.05)

            # Give extra time to ensure no straggling events arrive
            await asyncio.sleep(0.15)

            # Only the PHASE_START event should have been rendered
            assert len(renderer.lines) == 1
            assert "Phase 1 starting" in renderer.lines[0]
        finally:
            started_event_bus.unsubscribe(renderer.handle_event)
            renderer.stop()

    async def test_phase_timeline_from_events(self, started_event_bus):
        """Emit PHASE_START and PHASE_END events, verify they flow through correctly."""
        recorder = CapturingHandler()
        started_event_bus.subscribe(recorder)

        try:
            cid = "test-phase-corr-001"
            start_event = _make_event(
                event_type=SupervisorEventType.PHASE_START,
                message="Backend startup begins",
                phase="backend",
                correlation_id=cid,
            )
            end_event = _make_event(
                event_type=SupervisorEventType.PHASE_END,
                message="Backend startup complete",
                phase="backend",
                duration_ms=1234.5,
                correlation_id=cid,
            )

            started_event_bus.emit(start_event)
            started_event_bus.emit(end_event)

            for _ in range(50):
                if len(recorder.events) >= 2:
                    break
                await asyncio.sleep(0.05)

            assert len(recorder.events) == 2
            assert recorder.events[0].event_type == SupervisorEventType.PHASE_START
            assert recorder.events[0].phase == "backend"
            assert recorder.events[0].correlation_id == cid
            assert recorder.events[1].event_type == SupervisorEventType.PHASE_END
            assert recorder.events[1].duration_ms == 1234.5
            assert recorder.events[1].correlation_id == cid
        finally:
            started_event_bus.unsubscribe(recorder)

    async def test_high_throughput_all_received(self, started_event_bus):
        """Emit 100 events rapidly, verify all received by renderer."""
        recorder = CapturingHandler()
        started_event_bus.subscribe(recorder)

        try:
            count = 100
            for i in range(count):
                started_event_bus.emit(
                    _make_event(message=f"event-{i}")
                )

            # Wait for delivery - queue size is 50, so some may be dropped.
            # But the bus processes faster than emit, so most should arrive.
            # Give ample time for the consumer to drain.
            for _ in range(200):
                if len(recorder.events) >= count:
                    break
                await asyncio.sleep(0.02)

            # With queue size 50 and rapid emission, some events may be
            # dropped (oldest-first). Verify we received a significant number.
            # The bus drops oldest on overflow, so the LAST events should arrive.
            received = len(recorder.events)
            assert received >= 40, (
                f"Expected at least 40 of {count} events, got {received}"
            )

            # Verify ordering is preserved for received events
            messages = [e.message for e in recorder.events]
            for i in range(1, len(messages)):
                prev_idx = int(messages[i - 1].split("-")[1])
                curr_idx = int(messages[i].split("-")[1])
                assert curr_idx > prev_idx, (
                    f"Event ordering broken: {messages[i-1]} came before {messages[i]}"
                )
        finally:
            started_event_bus.unsubscribe(recorder)

    async def test_backpressure_under_load(self, started_event_bus):
        """Fill bus queue, verify oldest dropped and renderer still works."""
        # Create a slow handler that blocks delivery to cause backpressure
        slow_recorder = CapturingHandler()

        # Block the consumer by subscribing a handler that sleeps
        async def slow_handler(event: SupervisorEvent) -> None:
            await asyncio.sleep(10)  # Very slow - will stall consumer

        started_event_bus.subscribe(slow_handler)
        started_event_bus.subscribe(slow_recorder)

        try:
            # Give consumer a moment to start waiting on slow_handler
            await asyncio.sleep(0.05)

            # Flood with more events than queue size (50)
            for i in range(70):
                started_event_bus.emit(_make_event(message=f"flood-{i}"))

            # The queue should be full, oldest should have been dropped
            assert started_event_bus.dropped_count > 0, (
                "Expected some drops with 70 events into a 50-element queue"
            )
        finally:
            started_event_bus.unsubscribe(slow_handler)
            started_event_bus.unsubscribe(slow_recorder)

    async def test_bus_stop_delivers_remaining(self, monkeypatch):
        """Emit events, call stop(), verify queued events still delivered."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "50")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        try:
            import unified_supervisor
            monkeypatch.setattr(
                unified_supervisor, "create_safe_task", asyncio.create_task
            )
        except (ImportError, AttributeError):
            pass

        from unified_supervisor import SupervisorEventBus as SEB, get_event_bus

        SEB._instance = None
        import unified_supervisor as _us
        _us._supervisor_event_bus = None

        bus = get_event_bus()
        recorder = CapturingHandler()
        bus.subscribe(recorder)

        await bus.start()

        try:
            # Emit several events
            for i in range(5):
                bus.emit(_make_event(message=f"pre-stop-{i}"))

            # Give the consumer a moment to pick some up
            await asyncio.sleep(0.2)

            # Stop the bus - should drain remaining queue
            await bus.stop()

            # All 5 events should have been delivered (either during normal
            # processing or during the drain-on-stop phase)
            assert len(recorder.events) == 5, (
                f"Expected 5 events after stop(), got {len(recorder.events)}"
            )
        finally:
            bus.unsubscribe(recorder)
            SEB._instance = None
            _us._supervisor_event_bus = None

    async def test_full_lifecycle(self, monkeypatch):
        """start bus -> subscribe renderer -> emit events -> stop bus -> verify all received."""
        monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "50")
        monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

        try:
            import unified_supervisor
            monkeypatch.setattr(
                unified_supervisor, "create_safe_task", asyncio.create_task
            )
        except (ImportError, AttributeError):
            pass

        from unified_supervisor import SupervisorEventBus as SEB, get_event_bus

        SEB._instance = None
        import unified_supervisor as _us
        _us._supervisor_event_bus = None

        bus = get_event_bus()

        # Phase 1: Start bus
        await bus.start()
        assert bus._started is True

        # Phase 2: Subscribe renderers
        plain = CapturingPlainRenderer(verbosity="debug")
        json_r = CapturingJsonRenderer(verbosity="debug")
        plain.start()
        json_r.start()
        bus.subscribe(plain.handle_event)
        bus.subscribe(json_r.handle_event)
        assert bus.handler_count == 2

        try:
            # Phase 3: Emit a variety of events
            events_to_emit = [
                _make_event(
                    event_type=SupervisorEventType.PHASE_START,
                    message="Preflight begins",
                    phase="preflight",
                    correlation_id="lifecycle-001",
                ),
                _make_event(
                    event_type=SupervisorEventType.COMPONENT_STATUS,
                    message="Backend starting",
                    component="jarvis-body",
                    severity=SupervisorEventSeverity.INFO,
                ),
                _make_event(
                    event_type=SupervisorEventType.PHASE_END,
                    message="Preflight complete",
                    phase="preflight",
                    duration_ms=500.0,
                    correlation_id="lifecycle-001",
                ),
                _make_event(
                    event_type=SupervisorEventType.STARTUP_COMPLETE,
                    message="All systems go",
                    severity=SupervisorEventSeverity.SUCCESS,
                ),
            ]

            for ev in events_to_emit:
                bus.emit(ev)

            # Wait for delivery
            for _ in range(50):
                if len(plain.lines) >= 4 and len(json_r.outputs) >= 4:
                    break
                await asyncio.sleep(0.05)

            # Phase 4: Stop bus (drains remaining)
            await bus.stop()

            # Phase 5: Verify all events received by both renderers
            assert len(plain.lines) == 4, (
                f"Plain renderer expected 4 lines, got {len(plain.lines)}"
            )
            assert len(json_r.outputs) == 4, (
                f"JSON renderer expected 4 outputs, got {len(json_r.outputs)}"
            )

            # Verify plain text content
            assert "Preflight begins" in plain.lines[0]
            assert "[PHASE_START]" in plain.lines[0]
            assert "Backend starting" in plain.lines[1]
            assert "Preflight complete" in plain.lines[2]
            assert "(500.0ms)" in plain.lines[2]
            assert "All systems go" in plain.lines[3]

            # Verify JSON content
            for output in json_r.outputs:
                parsed = json.loads(output)
                assert "event_type" in parsed
                assert "message" in parsed
                assert "severity" in parsed

            # Verify specific JSON entries
            first_json = json.loads(json_r.outputs[0])
            assert first_json["event_type"] == "phase_start"
            assert first_json["phase"] == "preflight"

            last_json = json.loads(json_r.outputs[3])
            assert last_json["event_type"] == "startup_complete"
            assert last_json["severity"] == "success"
        finally:
            bus.unsubscribe(plain.handle_event)
            bus.unsubscribe(json_r.handle_event)
            plain.stop()
            json_r.stop()
            SEB._instance = None
            _us._supervisor_event_bus = None
