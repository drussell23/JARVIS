# tests/unit/core/test_vm_lifecycle_manager.py
"""Tests for VMLifecycleManager — v298.0 (T1–T20)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# --- helpers -------------------------------------------------------------------

def make_test_config(tmp_path: Path, **overrides) -> "VMLifecycleConfig":  # noqa: F821
    from backend.core.vm_lifecycle_manager import VMLifecycleConfig
    defaults = dict(
        inactivity_threshold_s=0.3,
        idle_grace_s=0.1,
        warming_await_timeout_s=2.0,
        max_uptime_s=None,
        quiet_hours=None,
        quiet_hours_threshold_factor=0.25,
        drain_hard_cap_s=600.0,
        warm_max_strikes=3,
        lease_dir=tmp_path,
        strict_drain=True,
    )
    defaults.update(overrides)
    return VMLifecycleConfig(**defaults)


def make_mock_controller(start_returns=(True, None, None)):
    from backend.core.vm_lifecycle_manager import VMController
    ctrl = MagicMock()
    ctrl.start_vm = AsyncMock(return_value=start_returns)
    ctrl.stop_vm = AsyncMock()
    ctrl.get_vm_host_port = MagicMock(return_value=("127.0.0.1", 8000))
    ctrl.notify_vm_unreachable = MagicMock()
    return ctrl


class _RecordingSink:
    def __init__(self):
        self.events: List = []
    async def emit(self, event) -> None:
        self.events.append(event)


# --- T16: LifecycleLease stale PID overwrite -----------------------------------

def test_lifecycle_lease_stale_pid_overwrite(tmp_path):
    """T16 — stale PID in lease file → overwrite succeeds."""
    from backend.core.vm_lifecycle_manager import LifecycleLease
    lease = LifecycleLease(lease_dir=tmp_path)
    # Write a lease with a dead PID (999999999 is extremely unlikely to exist)
    import json
    lease_file = tmp_path / "vm_lifecycle.lease"
    lease_file.write_text(json.dumps({"pid": 999999999, "session_id": "dead", "acquired_at": 0.0}))
    session_id = lease.acquire()
    assert session_id != "dead"
    assert len(session_id) > 8
    lease.release()


# --- T17: LifecycleLease live PID → DualAuthorityError -------------------------

def test_lifecycle_lease_live_pid_dual_authority(tmp_path):
    """T17 — live PID in lease file → DualAuthorityError raised."""
    import json
    from backend.core.vm_lifecycle_manager import LifecycleLease, DualAuthorityError
    lease_file = tmp_path / "vm_lifecycle.lease"
    # Write our own PID as an "incumbent" (simulate another process)
    other_pid = os.getpid()  # same PID means same process, treated as live
    # Use a different test approach: mock os.kill to simulate a live process
    lease_file.write_text(json.dumps({
        "pid": other_pid,
        "session_id": "incumbent_session",
        "acquired_at": time.time(),
    }))
    lease = LifecycleLease(lease_dir=tmp_path)
    with patch("os.getpid", return_value=other_pid + 1):  # different PID
        with pytest.raises(DualAuthorityError) as exc_info:
            lease.acquire()
    assert exc_info.value.incumbent_session_id == "incumbent_session"


# --- T18: Unregistered caller raises UnregisteredActivitySourceError -----------

@pytest.mark.asyncio
async def test_unregistered_caller_raises(tmp_path):
    """T18 — unknown caller_id with strict_drain=True → raises."""
    from backend.core.vm_lifecycle_manager import (
        VMLifecycleManager, UnregisteredActivitySourceError, VMFsmState,
    )
    config = make_test_config(tmp_path, strict_drain=True)
    ctrl = make_mock_controller()
    sink = _RecordingSink()
    mgr = VMLifecycleManager(config=config, controller=ctrl, telemetry_sink=sink)
    await mgr.start()
    # Warm to READY so record_activity_from doesn't hit COLD guard
    await mgr.ensure_warmed("test")
    assert mgr.state == VMFsmState.READY
    with pytest.raises(UnregisteredActivitySourceError):
        mgr.record_activity_from("totally.unknown.caller")
    await mgr.stop()
