"""
Phase 7: Shared unit-test fixtures for JARVIS test suite.

Supplements the root ``tests/conftest.py`` with fixtures specific to unit
tests.  All fixtures here are function-scoped (default) to ensure complete
isolation between tests.

Import strategy
───────────────
``unified_supervisor.py`` is a 75K-line monolith.  Its ``create_safe_task``
helper depends on internal state.  To avoid import-time side effects we:

1. Patch ``create_safe_task`` → ``asyncio.create_task`` via monkeypatch before
   any supervisor import.
2. Reset singleton instances in fixture teardown.

Backend modules (``backend.*``) are lighter and can be imported directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _patch_create_safe_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``create_safe_task`` in unified_supervisor to avoid side effects.

    ``create_safe_task`` may reference internals that are not initialised in
    test context.  Replace it with plain ``asyncio.create_task``.
    """
    try:
        import unified_supervisor
        monkeypatch.setattr(
            unified_supervisor, "create_safe_task", asyncio.create_task
        )
    except (ImportError, AttributeError):
        pass


# ── SupervisorEventBus fixtures ──────────────────────────────────────────────

@pytest.fixture
async def event_bus(monkeypatch):
    """Fully started SupervisorEventBus with a small bounded queue.

    Yields the bus, then stops it and resets the singleton on teardown.
    """
    monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "10")
    monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
    _patch_create_safe_task(monkeypatch)

    from unified_supervisor import SupervisorEventBus, get_event_bus

    # Reset singleton
    SupervisorEventBus._instance = None

    import unified_supervisor as _us
    _us._supervisor_event_bus = None

    bus = get_event_bus()
    await bus.start()
    yield bus
    await bus.stop()
    SupervisorEventBus._instance = None
    _us._supervisor_event_bus = None


@pytest.fixture
def event_bus_sync(monkeypatch):
    """SupervisorEventBus that has NOT been started.

    Tests the synchronous delivery path (``_queue is None``).
    """
    monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")
    _patch_create_safe_task(monkeypatch)

    from unified_supervisor import SupervisorEventBus

    SupervisorEventBus._instance = None

    import unified_supervisor as _us
    _us._supervisor_event_bus = None

    bus = SupervisorEventBus()
    yield bus
    SupervisorEventBus._instance = None
    _us._supervisor_event_bus = None


# ── Filesystem fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def tmp_jarvis_home(tmp_path: Path) -> Path:
    """Temporary ``~/.jarvis/`` tree with Trinity component directories.

    Returns the *home* directory (equivalent to ``~/.jarvis/``).
    """
    jarvis_home = tmp_path / ".jarvis"
    (jarvis_home / "trinity" / "components").mkdir(parents=True)
    (jarvis_home / "cross_repo" / "locks").mkdir(parents=True)
    return jarvis_home


@pytest.fixture
def heartbeat_writer(tmp_jarvis_home: Path):
    """Callable that writes heartbeat JSON into ``tmp_jarvis_home``.

    Usage::

        heartbeat_writer("jarvis-body", {"status": "healthy"})
    """
    def _write(component: str, data: Optional[Dict[str, Any]] = None) -> Path:
        payload = {
            "component": component,
            "timestamp": time.time(),
            "status": "healthy",
            **(data or {}),
        }
        hb_dir = tmp_jarvis_home / "trinity" / "components"
        hb_file = hb_dir / f"{component}.heartbeat.json"
        hb_file.write_text(json.dumps(payload))
        return hb_file

    return _write


# ── DLM fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def dlm(tmp_path: Path):
    """File-backed DistributedLockManager with 2 s TTL in a temp directory.

    Returns the initialised (but NOT started) DLM instance.
    """
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()

    from backend.core.distributed_lock_manager import (
        DistributedLockManager,
        LockConfig,
        LockBackend,
    )

    config = LockConfig(
        lock_dir=lock_dir,
        default_ttl_seconds=2.0,
        default_timeout_seconds=1.0,
        cleanup_interval_seconds=60.0,
        backend=LockBackend.FILE,
    )
    return DistributedLockManager(config=config)


# ── Environment helpers ──────────────────────────────────────────────────────

@pytest.fixture
def mock_env(monkeypatch):
    """Context manager to batch-set environment variables.

    Usage::

        with mock_env(FOO="bar", BAZ="qux"):
            ...
    """
    @contextmanager
    def _ctx(**kwargs: str):
        for key, val in kwargs.items():
            monkeypatch.setenv(key, val)
        yield
        # monkeypatch automatically restores on test teardown

    return _ctx
