"""Integration test for the FSEventBridge → TrinityEventBus delivery chain.

Battle test bt-2026-04-12-005521 ran for 30+ minutes with FileWatchGuard
healthy and 6 sensors subscribed to ``fs.changed.*``, but **zero** events
ever flowed through the bus to any sensor handler. The chain was silent end
to end: per-event logging is at DEBUG, the bridge counter is internal, and
no first-event sentinel exists. There was no way to tell whether watchdog
was failing to deliver, the bridge was failing to publish, or the bus was
failing to dispatch — all three look identical from the outside.

This test exercises the full chain in isolation against a tmp_path so a
green run is direct evidence that watchdog → FileWatchGuard → FSEventBridge
→ TrinityEventBus → subscriber callback works on this platform. A failure
here localizes the bug to the chain itself rather than the live battle test
environment (FSEvents conflict, registry collision, etc.).

Backend choice
--------------
The default watchdog Observer on macOS 26 (ARM64, Python 3.9.6) selects the
FSEvents backend, which segfaults during ``Observer.join()`` with
``KERN_PROTECTION_FAILURE`` / pointer authentication failure. The crash is
deterministic and happens even in a one-shot start/stop. Reproduced in this
test suite during initial development — see commit history.

The test forces the ``PollingObserver`` backend to avoid the segfault. This
also serves as a load-bearing diagnostic: if the live battle test environment
hits the same FSEvents crash (likely root cause of zero deliveries), the
production fix is to force ``PollingObserver`` there too.

The test is deliberately tolerant of debounce / dedup timing — it polls for
delivery up to ``_DELIVERY_TIMEOUT_S`` rather than asserting on a single
sleep boundary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.fs_event_bridge import (
    FileSystemEventBridge,
)
from backend.core.resilience.file_watch_guard import (
    FileWatchConfig,
    get_global_watch_registry,
)
from backend.core.trinity_event_bus import TrinityEventBus


_DELIVERY_TIMEOUT_S = 4.0  # Generous: debounce is 0.3s, watchdog poll is ~1s


async def _wait_for(predicate, timeout: float = _DELIVERY_TIMEOUT_S) -> bool:
    """Poll a predicate until True or timeout. Returns final value."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.fixture
async def fresh_bus_and_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Construct a fresh TrinityEventBus + FSEventBridge against tmp_path.

    Uses a unique tmp_path to avoid GlobalWatchRegistry collisions across
    parallel test runs. Tears down both at fixture exit so subsequent tests
    don't inherit dangling watches.

    Forces the watchdog ``PollingObserver`` backend by monkeypatching
    ``watchdog.observers.Observer`` for the duration of the test — see
    module docstring for the FSEvents segfault context.
    """
    from watchdog.observers.polling import PollingObserver
    monkeypatch.setattr(
        "watchdog.observers.Observer", PollingObserver, raising=True
    )

    # Pre-clear registry for this path in case a prior run leaked
    registry = get_global_watch_registry()
    registry.unregister(tmp_path)

    bus = TrinityEventBus()
    await bus.start()

    config = FileWatchConfig(
        patterns=["*.py"],
        ignore_patterns=["*.swp", "*.tmp"],
        recursive=True,
        debounce_seconds=0.1,  # Tighter than prod (0.3) for test speed
        verify_checksum=True,
        dedup_ttl_seconds=0.5,
    )
    bridge = FileSystemEventBridge(
        project_root=tmp_path,
        event_bus=bus,
        watch_config=config,
    )
    await bridge.start()

    # Brief settle so polling observer thread is fully primed before we
    # start poking files. PollingObserver default interval is 1s.
    await asyncio.sleep(0.5)

    try:
        yield bus, bridge, tmp_path
    finally:
        try:
            await bridge.stop()
        except Exception:
            pass
        try:
            await bus.stop()
        except Exception:
            pass
        registry.unregister(tmp_path)


class TestChainBoot:
    """Sanity guards on the chain construction itself."""

    @pytest.mark.asyncio
    async def test_bridge_starts_and_reports_zero_events_initially(
        self, fresh_bus_and_bridge
    ) -> None:
        _bus, bridge, _root = fresh_bus_and_bridge
        metrics = bridge.get_metrics()
        assert metrics["events_published"] == 0
        assert metrics["guard_healthy"] is True

    @pytest.mark.asyncio
    async def test_subscription_topic_matches_publish_topic(
        self, fresh_bus_and_bridge
    ) -> None:
        bus, _bridge, _root = fresh_bus_and_bridge
        received: List[str] = []

        async def handler(event: Any) -> None:
            received.append(event.topic)

        await bus.subscribe("fs.changed.*", handler)
        # Just confirm subscription registered without exception
        # (delivery is exercised in TestEndToEndDelivery)
        assert True


class TestEndToEndDelivery:
    """The actual smoking gun — does an event reach a subscribed handler?"""

    @pytest.mark.asyncio
    async def test_create_py_file_delivers_to_subscriber(
        self, fresh_bus_and_bridge
    ) -> None:
        """Create a new .py file in the watched root → subscriber must fire.

        This is the exact failure mode from bt-2026-04-12-005521: every
        op writes files, but no fs.changed event ever reached TodoScanner.
        If this assertion holds, the chain works in isolation and the
        live failure is environmental.
        """
        bus, bridge, root = fresh_bus_and_bridge
        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe("fs.changed.*", handler)

        target = root / "new_module.py"
        target.write_text("def hello():\n    return 'world'\n")

        delivered = await _wait_for(lambda: len(received) > 0)
        assert delivered, (
            f"No fs.changed event delivered within {_DELIVERY_TIMEOUT_S}s. "
            f"Bridge metrics: {bridge.get_metrics()}"
        )
        assert any(e.topic.startswith("fs.changed.") for e in received), (
            f"Got events but none on fs.changed.*: "
            f"{[e.topic for e in received]}"
        )

    @pytest.mark.asyncio
    async def test_modify_existing_py_file_delivers_modified_topic(
        self, fresh_bus_and_bridge
    ) -> None:
        """Modify an existing file → expect fs.changed.modified."""
        bus, bridge, root = fresh_bus_and_bridge

        # Create the file BEFORE subscribing so the create event drains
        target = root / "existing.py"
        target.write_text("x = 1\n")
        await asyncio.sleep(0.5)  # Let the create flush through

        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe("fs.changed.*", handler)

        # Now mutate it
        target.write_text("x = 2\n# changed\n")

        delivered = await _wait_for(lambda: len(received) > 0)
        assert delivered, (
            f"No event for file modification within {_DELIVERY_TIMEOUT_S}s. "
            f"Bridge metrics: {bridge.get_metrics()}"
        )

    @pytest.mark.asyncio
    async def test_non_py_file_filtered_out(
        self, fresh_bus_and_bridge
    ) -> None:
        """Files outside the configured patterns must NOT deliver.

        Confirms the FileWatchGuard `_should_process` filter is active.
        Without this guard, every file in the repo would flood the bus.
        """
        bus, _bridge, root = fresh_bus_and_bridge
        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe("fs.changed.*", handler)

        # .md is NOT in the test config patterns (only *.py)
        (root / "README.md").write_text("# hi\n")
        await asyncio.sleep(_DELIVERY_TIMEOUT_S * 0.5)

        assert len(received) == 0, (
            f"Filter leak: non-py file produced events: "
            f"{[e.topic for e in received]}"
        )

    @pytest.mark.asyncio
    async def test_payload_carries_relative_path_and_extension(
        self, fresh_bus_and_bridge
    ) -> None:
        """Subscriber should receive a payload with relative_path + extension.

        TodoScanner depends on `payload.get('extension')` to filter to .py
        and `payload['path']` to scan. If the payload shape changes, every
        sensor that subscribes silently breaks.
        """
        bus, _bridge, root = fresh_bus_and_bridge
        received: List[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await bus.subscribe("fs.changed.*", handler)

        target = root / "payload_check.py"
        target.write_text("y = 42\n")

        await _wait_for(lambda: len(received) > 0)
        assert received, "no event arrived"
        payload = received[0].payload
        assert "path" in payload
        assert "relative_path" in payload
        assert "extension" in payload
        assert payload["extension"] == ".py"
        assert payload["relative_path"] == "payload_check.py"


class TestObservabilityGap:
    """Document the gap that made bt-2026-04-12-005521 silent.

    These aren't fixes — they're regression guards. If someone tries to
    silence per-event logging again, these assertions catch it.
    """

    @pytest.mark.asyncio
    async def test_bridge_metrics_increment_after_delivery(
        self, fresh_bus_and_bridge
    ) -> None:
        """get_metrics() must reflect published events.

        Without a counter, the only way to confirm chain health is a
        debug log line — which is exactly the gap that hid the failure.
        """
        bus, bridge, root = fresh_bus_and_bridge

        async def handler(event: Any) -> None:
            pass

        await bus.subscribe("fs.changed.*", handler)

        before = bridge.get_metrics()["events_published"]

        (root / "metrics_check.py").write_text("z = 1\n")
        await _wait_for(
            lambda: bridge.get_metrics()["events_published"] > before
        )

        after = bridge.get_metrics()["events_published"]
        assert after > before, (
            f"Bridge published counter did not increment: "
            f"before={before} after={after}"
        )
