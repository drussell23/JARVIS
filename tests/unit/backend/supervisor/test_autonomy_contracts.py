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
