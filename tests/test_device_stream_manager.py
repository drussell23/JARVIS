# [Ouroboros] Modified by Ouroboros (op=op-01a12176-) at 2026-10-09 16:21 UTC
# Reason: `backend/api/device_stream_manager.py` has no corresponding test module. CREATE `tests/test_device_stream_manager.py` co

from __future__ import annotations
import asyncio
import json
import time
from collections import deque
from typing import AsyncIterator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.device_stream_manager import (
    DeviceRegistration,
    DeviceStreamManager,
    RehydrationBuffer,
    get_device_manager,
    reset_device_manager,
)

class _FakeEventStream:
    def __init__(self, frames: List[str]) -> None:
        self._frames = frames
        self._idx = 0

    async def __aiter__(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame


async def test_import_smoke() -> None:
    # The module imports cleanly and exposes expected public API.
    assert DeviceStreamManager is not None
    assert RehydrationBuffer is not None
    assert DeviceRegistration is not None
    assert get_device_manager is not None
    assert reset_device_manager is not None




async def test_device_registration() -> None:
    now = time.monotonic()
    reg = DeviceRegistration("test-device", now)
    assert reg.device_id == "test-device"
    assert reg.connected_at == now
    assert reg.last_beat == now
    assert reg.alive is True


async def test_device_stream_manager_lifecycle() -> None:
    manager = DeviceStreamManager()

    # Register a device
    reg = manager.register("device-1")
    assert reg.device_id == "device-1"
    assert manager.is_alive("device-1") is True
    assert manager.heartbeat_expired("device-1") is False
    assert len(manager.active_devices) == 1

    # Re-register (should increment reconnects)
    reg2 = manager.register("device-1")
    assert reg2.device_id == "device-1"
    assert manager.is_alive("device-1") is True
    assert manager.heartbeat_expired("device-1") is False

    # Beat the device
    manager.beat("device-1")
    assert manager.heartbeat_expired("device-1") is False

    # Deregister
    manager.deregister("device-1")
    assert manager.is_alive("device-1") is False
    assert len(manager.active_devices) == 0

    # Deregister non-existent (should not raise)
    manager.deregister("non-existent")






async def test_device_stream_manager_device_stream_replay() -> None:
    manager = DeviceStreamManager()
    inner_stream = _FakeEventStream([
        "id:1\nevent:foo\ndata:{\"a\":1}\n\n",
        "id:2\nevent:bar\ndata:{\"b\":2}\n\n"
    ])

    # Add frames to rehydration buffer first
    manager._rehydration.append("id:1\nevent:foo\ndata:{\"a\":1}\n\n")
    manager._rehydration.append("id:2\nevent:bar\ndata:{\"b\":2}\n\n")

    # Mock cold start frames to be empty
    cold_start_frames = AsyncMock(return_value=[])

    stream = manager.device_stream(
        "device-1", inner_stream,
        last_event_id=0,
        cold_start_frames=cold_start_frames
    )

    # Consume the stream
    frames = []
    async for frame in stream:
        frames.append(frame)

    assert len(frames) == 2  # Should replay from buffer and then get inner frames






