"""
Phase 7: Shared integration-test fixtures for JARVIS test suite.

Provides mock servers, pre-populated heartbeat files, and client stubs
so integration tests can exercise multi-component interactions without
real external services.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


# ── Filesystem fixtures (reused from unit conftest pattern) ──────────────

@pytest.fixture
def tmp_jarvis_home(tmp_path: Path) -> Path:
    """Temporary ``~/.jarvis/`` tree with Trinity component directories."""
    jarvis_home = tmp_path / ".jarvis"
    (jarvis_home / "trinity" / "components").mkdir(parents=True)
    (jarvis_home / "cross_repo" / "locks").mkdir(parents=True)
    return jarvis_home


@pytest.fixture
def heartbeat_writer(tmp_jarvis_home: Path):
    """Callable that writes heartbeat JSON into ``tmp_jarvis_home``."""
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


@pytest.fixture
def mock_heartbeat_files(tmp_jarvis_home, heartbeat_writer):
    """Pre-populated heartbeat files for all 3 Trinity components."""
    now = time.time()
    heartbeat_writer("jarvis_body", {"timestamp": now, "status": "healthy"})
    heartbeat_writer("jarvis_prime", {"timestamp": now, "status": "healthy"})
    heartbeat_writer("reactor_core", {"timestamp": now, "status": "healthy"})
    return tmp_jarvis_home


# ── Mock HTTP health server ─────────────────────────────────────────────

@pytest.fixture
async def mock_health_server():
    """Lightweight asyncio HTTP server simulating Trinity health endpoints.

    Yields a controller dict with:
      - ``url``: base URL (http://127.0.0.1:{port})
      - ``set_response(path, status, body)``: configure responses
      - ``set_latency(path, seconds)``: add artificial latency
      - ``request_count``: dict of path → call count
    """
    from aiohttp import web

    routes = web.RouteTableDef()
    responses: Dict[str, Dict[str, Any]] = {}
    latencies: Dict[str, float] = {}
    request_count: Dict[str, int] = {}

    @routes.get("/{path:.*}")
    async def _handle(request):
        path = f"/{request.match_info['path']}"
        request_count[path] = request_count.get(path, 0) + 1

        if path in latencies:
            await asyncio.sleep(latencies[path])

        cfg = responses.get(path, {"status": 200, "body": {"status": "ok"}})
        return web.json_response(cfg["body"], status=cfg["status"])

    app = web.Application()
    app.router.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Extract the dynamically assigned port
    port = site._server.sockets[0].getsockname()[1]

    def set_response(path: str, status: int = 200, body: Any = None):
        responses[path] = {"status": status, "body": body or {"status": "ok"}}

    def set_latency(path: str, seconds: float):
        latencies[path] = seconds

    controller = {
        "url": f"http://127.0.0.1:{port}",
        "set_response": set_response,
        "set_latency": set_latency,
        "request_count": request_count,
        "port": port,
    }

    yield controller

    await runner.cleanup()


# ── Mock Prime Client ───────────────────────────────────────────────────

@pytest.fixture
def mock_prime_client():
    """AsyncMock of JARVISPrimeClient with configurable responses.

    Usage::

        mock_prime_client.generate.return_value = {"text": "Hello"}
    """
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.generate = AsyncMock(return_value={"text": "Mock response"})
    client.health_check = AsyncMock(return_value={"phase": "ready"})
    client.is_ready = AsyncMock(return_value=True)
    return client


# ── Event Bus helpers ───────────────────────────────────────────────────

@pytest.fixture
async def started_event_bus(monkeypatch):
    """Fully started SupervisorEventBus for integration tests.

    Patches create_safe_task and resets singleton on teardown.
    """
    monkeypatch.setenv("JARVIS_EVENT_BUS_QUEUE_SIZE", "50")
    monkeypatch.setenv("JARVIS_EVENT_BUS_ENABLED", "true")

    try:
        import unified_supervisor
        monkeypatch.setattr(
            unified_supervisor, "create_safe_task", asyncio.create_task
        )
    except (ImportError, AttributeError):
        pass

    from unified_supervisor import SupervisorEventBus, get_event_bus

    SupervisorEventBus._instance = None
    import unified_supervisor as _us
    _us._supervisor_event_bus = None

    bus = get_event_bus()
    await bus.start()
    yield bus
    await bus.stop()
    SupervisorEventBus._instance = None
    _us._supervisor_event_bus = None


# ── DLM fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def integration_dlm(tmp_path: Path):
    """File-backed DistributedLockManager for integration tests."""
    lock_dir = tmp_path / "integration_locks"
    lock_dir.mkdir()

    from backend.core.distributed_lock_manager import (
        DistributedLockManager,
        LockConfig,
        LockBackend,
    )

    config = LockConfig(
        lock_dir=lock_dir,
        default_ttl_seconds=5.0,
        default_timeout_seconds=3.0,
        cleanup_interval_seconds=60.0,
        backend=LockBackend.FILE,
    )
    return DistributedLockManager(config=config)
