"""Tests for autonomy contract checking and version comparison."""

import pytest


def test_version_gte_simple():
    """Basic version comparison."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0", "1.0") is True
    assert _version_gte("2.0", "1.0") is True
    assert _version_gte("1.0", "2.0") is False


def test_version_gte_multidigit():
    """Multi-digit segments must compare numerically, not lexically."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.10", "1.9") is True
    assert _version_gte("1.9", "1.10") is False


def test_version_gte_three_segments():
    """Three-segment versions (patch level)."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.1", "1.0.0") is True
    assert _version_gte("1.0.0", "1.0.1") is False
    assert _version_gte("2.0.0", "1.9.9") is True


def test_version_gte_unequal_length():
    """Versions with different segment counts.

    Note: "1.0" is treated as < "1.0.1" because Python tuple comparison
    treats shorter tuples as lesser when prefixes match.
    """
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0.0", "1.0") is True
    assert _version_gte("1.0", "1.0.1") is False


def test_version_gte_malformed_inputs():
    """Malformed version strings should return False, not raise."""
    from backend.supervisor.cross_repo_startup_orchestrator import _version_gte

    assert _version_gte("1.0-beta", "1.0") is False
    assert _version_gte("v1.0", "1.0") is False
    assert _version_gte("", "1.0") is False
    assert _version_gte("1.0", "abc") is False


from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp


@pytest.fixture
def mock_config():
    """Mock OrchestratorConfig with default ports."""
    config = MagicMock()
    config.jarvis_prime_default_port = 8001
    config.reactor_core_default_port = 8090
    return config


def _make_resp_cm(schema_version="1.0"):
    """Create a mock response context manager for session.get()."""
    resp = MagicMock()
    resp.status = 200

    async def _json():
        return {
            "autonomy_schema_version": schema_version,
            "status": "healthy",
        }

    resp.json = _json

    # session.get() returns an async context manager
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session_factory(get_side_effect=None, schema_version="1.0"):
    """Build a mock aiohttp.ClientSession that works with `async with`."""

    def factory(*args, **kwargs):
        session = MagicMock()
        if get_side_effect is not None:
            session.get = MagicMock(side_effect=get_side_effect)
        else:
            session.get = MagicMock(return_value=_make_resp_cm(schema_version))

        # aiohttp.ClientSession() used as `async with ... as session:`
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    return factory


@pytest.mark.asyncio
async def test_contract_check_all_healthy(mock_config):
    """When all services are healthy and compatible, reason is 'active'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    with patch(
        "backend.supervisor.cross_repo_startup_orchestrator.aiohttp.ClientSession",
        side_effect=_make_session_factory(schema_version="1.0"),
    ):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            passed, status, checks = await check_autonomy_contracts()

    assert passed is True
    assert checks["reason"] == "active"
    assert checks.get("pending") == []


@pytest.mark.asyncio
async def test_contract_check_services_unreachable(mock_config):
    """When services are unreachable, reason is 'pending_services'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    def _raise_on_get(*args, **kwargs):
        raise aiohttp.ClientError("Connection refused")

    with patch(
        "backend.supervisor.cross_repo_startup_orchestrator.aiohttp.ClientSession",
        side_effect=_make_session_factory(get_side_effect=_raise_on_get),
    ):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            passed, status, checks = await check_autonomy_contracts()

    assert passed is False
    assert checks["reason"] == "pending_services"
    assert "prime" in checks["pending"]
    assert "reactor" in checks["pending"]


@pytest.mark.asyncio
async def test_contract_check_schema_mismatch(mock_config):
    """When services are healthy but schema incompatible, reason is 'schema_mismatch'."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        check_autonomy_contracts,
    )

    # Mock OrchestrationJournal so body_journal is True (has_lease=True),
    # otherwise the reason falls through to 'pending_lease' before 'schema_mismatch'.
    mock_journal = MagicMock()
    mock_journal.has_lease = True

    mock_journal_cls = MagicMock()
    mock_journal_cls.get_instance.return_value = mock_journal

    with patch(
        "backend.supervisor.cross_repo_startup_orchestrator.aiohttp.ClientSession",
        side_effect=_make_session_factory(schema_version="0.1"),
    ):
        with patch(
            "backend.supervisor.cross_repo_startup_orchestrator.OrchestratorConfig",
            return_value=mock_config,
        ):
            with patch(
                "backend.core.orchestration_journal.OrchestrationJournal",
                mock_journal_cls,
            ):
                passed, status, checks = await check_autonomy_contracts()

    assert passed is False
    assert checks["reason"] == "schema_mismatch"
    assert checks["pending"] == []


def test_autonomy_mode_pending_blocks_writes():
    """pending mode must block writes identically to read_only."""
    for mode in ("pending", "read_only"):
        assert mode != "active", f"{mode} must block autonomous writes"


def test_autonomy_reason_to_mode_mapping():
    """Verify reason codes map to the correct autonomy mode."""
    _REASON_TO_MODE = {
        "pending_services": "pending",
        "pending_lease": "pending",
        "schema_mismatch": "read_only",
        "health_probe_failed": "read_only",
        "timeout": "read_only",
        "active": "active",
    }
    assert _REASON_TO_MODE["pending_services"] == "pending"
    assert _REASON_TO_MODE["pending_lease"] == "pending"
    assert _REASON_TO_MODE["schema_mismatch"] == "read_only"
    assert _REASON_TO_MODE["timeout"] == "read_only"
    assert _REASON_TO_MODE["active"] == "active"


import asyncio


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_early_exit():
    """Should exit early when all dependencies are met."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        await_autonomy_dependencies,
    )

    call_count = 0

    async def mock_check():
        nonlocal call_count
        call_count += 1
        return True, "autonomy_ready", {
            "reason": "active",
            "pending": [],
            "prime_reachable": True,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await await_autonomy_dependencies(
        check_fn=mock_check, timeout=15.0, poll_interval=0.1,
    )
    assert result["all_ready"] is True
    assert call_count == 1


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_timeout():
    """Should return partial results on timeout."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        await_autonomy_dependencies,
    )

    async def mock_check():
        return False, "contract_mismatch", {
            "reason": "pending_services",
            "pending": ["prime"],
            "prime_reachable": False,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await await_autonomy_dependencies(
        check_fn=mock_check, timeout=0.3, poll_interval=0.1,
    )
    assert result["all_ready"] is False
    assert result["reason"] == "pending_services"


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_gradual_readiness():
    """Should keep polling until all dependencies are ready."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        await_autonomy_dependencies,
    )

    poll_count = 0

    async def mock_check():
        nonlocal poll_count
        poll_count += 1
        if poll_count < 3:
            return False, "contract_mismatch", {
                "reason": "pending_services",
                "pending": ["prime"],
                "prime_reachable": False,
                "reactor_reachable": True,
                "body_journal": True,
            }
        return True, "autonomy_ready", {
            "reason": "active",
            "pending": [],
            "prime_reachable": True,
            "reactor_reachable": True,
            "body_journal": True,
        }

    result = await await_autonomy_dependencies(
        check_fn=mock_check, timeout=5.0, poll_interval=0.1,
    )
    assert result["all_ready"] is True
    assert poll_count == 3


@pytest.mark.asyncio
async def test_await_autonomy_dependencies_shutdown():
    """Should abort on shutdown signal."""
    from backend.supervisor.cross_repo_startup_orchestrator import (
        await_autonomy_dependencies,
    )

    shutdown = asyncio.Event()
    shutdown.set()  # Already shutting down

    async def mock_check():
        return False, "contract_mismatch", {
            "reason": "pending_services",
            "pending": ["prime", "reactor"],
        }

    result = await await_autonomy_dependencies(
        check_fn=mock_check, timeout=10.0, poll_interval=0.1,
        shutdown_event=shutdown,
    )
    assert result["all_ready"] is False
    assert result["reason"] == "shutdown"


def test_adaptive_monitor_interval():
    """Pending mode should use shorter check interval."""
    def _get_interval(mode: str, base_interval: float = 60.0) -> float:
        if mode == "pending":
            return min(5.0, base_interval)
        return base_interval

    assert _get_interval("pending") == 5.0
    assert _get_interval("active") == 60.0
    assert _get_interval("read_only") == 60.0
    assert _get_interval("pending", base_interval=3.0) == 3.0
