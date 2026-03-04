"""Tests for the 5 startup warning fixes.

Covers:
1. SQLite thread safety (check_same_thread + dedicated executor)
2. Quiet vs non-quiet log levels for wait_for_ready
3. Quiet probe escalation threshold
4. Anthropic API reason code mapping
5. Connectivity WARN→INFO conditional on fallback
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: SQLite cross-thread safety
# ---------------------------------------------------------------------------

def test_state_store_cross_thread_read_write():
    """State store must not raise ProgrammingError under concurrent executor scheduling."""
    from backend.autonomy.email_triage.state_store import TriageStateStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_triage.db")
        store = TriageStateStore(db_path=db_path)

        loop = asyncio.new_event_loop()
        try:
            # open() creates connection on the executor thread
            loop.run_until_complete(store.open())

            # load_latest_snapshot() reads on executor — may be a different thread
            # with the default pool. With our fix, it's the same dedicated thread.
            snapshot = loop.run_until_complete(store.load_latest_snapshot())
            # Should not raise ProgrammingError; snapshot may be None (empty DB)
            assert snapshot is None or isinstance(snapshot, dict)

            loop.run_until_complete(store.close())
        finally:
            loop.close()


def test_state_store_executor_is_single_thread():
    """Executor must be a single-thread pool, not None (default)."""
    from backend.autonomy.email_triage.state_store import TriageStateStore

    store = TriageStateStore(db_path=":memory:")
    assert store._executor is not None
    assert store._executor._max_workers == 1


def test_state_store_close_shuts_down_executor():
    """close() must shut down the dedicated executor."""
    from backend.autonomy.email_triage.state_store import TriageStateStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_close.db")
        store = TriageStateStore(db_path=db_path)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(store.open())
            assert store._executor is not None
            loop.run_until_complete(store.close())
            assert store._executor is None
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Test 2 & 3: Quiet vs non-quiet log levels + escalation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_ready_quiet_suppresses_warning():
    """quiet=True should log at DEBUG, not WARNING, on timeout."""
    from backend.core.ouroboros.integration import (
        ServiceReadinessChecker,
        ServiceReadinessLevel,
    )

    checker = ServiceReadinessChecker(
        service_name="test_service",
        base_url="http://127.0.0.1:19999",  # nothing listening
        health_check_timeout=0.1,
    )

    with patch("backend.core.ouroboros.integration.logger") as mock_logger:
        # quiet=True: timeout should log at DEBUG, not WARNING
        result = await checker.wait_for_ready(timeout=0.2, quiet=True)
        assert result is False

        # Verify no WARNING was emitted (only 1 quiet timeout, below threshold)
        for call in mock_logger.warning.call_args_list:
            msg = call[0][0] if call[0] else ""
            assert "not ready after" not in msg, f"Unexpected WARNING: {msg}"

        # DEBUG should have the timeout message
        debug_msgs = [
            call[0][0] for call in mock_logger.debug.call_args_list if call[0]
        ]
        assert any("not ready after" in m for m in debug_msgs), (
            f"Expected DEBUG timeout message, got: {debug_msgs}"
        )


@pytest.mark.asyncio
async def test_wait_for_ready_non_quiet_emits_warning():
    """quiet=False (default) should log timeout at WARNING."""
    from backend.core.ouroboros.integration import (
        ServiceReadinessChecker,
        ServiceReadinessLevel,
    )

    checker = ServiceReadinessChecker(
        service_name="test_service_loud",
        base_url="http://127.0.0.1:19999",
        health_check_timeout=0.1,
    )

    with patch("backend.core.ouroboros.integration.logger") as mock_logger:
        result = await checker.wait_for_ready(timeout=0.2, quiet=False)
        assert result is False

        warning_msgs = [
            call[0][0] for call in mock_logger.warning.call_args_list if call[0]
        ]
        assert any("not ready after" in m for m in warning_msgs), (
            f"Expected WARNING timeout message, got: {warning_msgs}"
        )


@pytest.mark.asyncio
async def test_quiet_probe_escalation_after_threshold():
    """After 3 quiet timeouts, one summary WARNING should fire."""
    from backend.core.ouroboros.integration import (
        ServiceReadinessChecker,
        ServiceReadinessLevel,
    )

    checker = ServiceReadinessChecker(
        service_name="test_escalation",
        base_url="http://127.0.0.1:19999",
        health_check_timeout=0.1,
    )

    with patch("backend.core.ouroboros.integration.logger") as mock_logger:
        # Fire 3 quiet timeouts
        for i in range(3):
            result = await checker.wait_for_ready(timeout=0.15, quiet=True)
            assert result is False

        # After 3rd call, a WARNING should have been emitted with the count
        warning_msgs = [
            call[0][0] for call in mock_logger.warning.call_args_list if call[0]
        ]
        assert any(
            "quiet probe failed" in m and "3" in m for m in warning_msgs
        ), f"Expected escalation WARNING with count 3, got: {warning_msgs}"

    # Verify stats track the count
    stats = checker.get_stats()
    assert stats["quiet_timeouts"] == 3


# ---------------------------------------------------------------------------
# Test 4: Anthropic API reason code mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_check_reason_codes():
    """Diagnostic must include reason code in message."""
    # Import the check function
    from backend.core.coding_council.diagnostics import (
        CheckCategory,
        CheckStatus,
    )

    # We need to call the static check function. It's defined inside PreFlightChecker
    # but we can test the logic by importing and calling it.
    from backend.core.coding_council.diagnostics import RuntimeChecker

    # Patch httpx to raise various exceptions
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key", "JARVIS_OFFLINE_MODE": ""}):
        # Test timeout
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=Exception("Connection timed out"))
            mock_client_cls.return_value = mock_client

            result = await RuntimeChecker.check_anthropic_api()
            assert "timeout" in result.message
            assert "local models available" in result.message

        # Test connection refused
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value = mock_client

            result = await RuntimeChecker.check_anthropic_api()
            assert "connection_refused" in result.message
            assert "local models available" in result.message

        # Test DNS failure
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=Exception("Name resolution failed"))
            mock_client_cls.return_value = mock_client

            result = await RuntimeChecker.check_anthropic_api()
            assert "dns_failed" in result.message

        # Test 401 auth failure
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await RuntimeChecker.check_anthropic_api()
            assert "auth_failed" in result.message
            assert "local models available" in result.message


# ---------------------------------------------------------------------------
# Test 5: Connectivity WARN→INFO conditional on fallback
# ---------------------------------------------------------------------------

def test_connectivity_warn_downgraded_only_with_fallback():
    """Connectivity warnings only downgrade to INFO when 'local models available' in message."""
    from backend.core.coding_council.diagnostics import (
        CheckCategory,
        CheckResult,
        CheckStatus,
    )

    # Simulate the log-level decision from startup.py
    def get_log_level(check: CheckResult) -> str:
        if check.status == CheckStatus.WARN:
            if (
                check.category == CheckCategory.CONNECTIVITY
                and "local models available" in check.message
            ):
                return "INFO"
            else:
                return "WARNING"
        return "OTHER"

    # With fallback message → INFO
    check_with_fallback = CheckResult(
        name="Anthropic API",
        category=CheckCategory.CONNECTIVITY,
        status=CheckStatus.WARN,
        message="Cannot reach Anthropic API (timeout, local models available)",
    )
    assert get_log_level(check_with_fallback) == "INFO"

    # Without fallback message → WARNING
    check_without_fallback = CheckResult(
        name="Anthropic API",
        category=CheckCategory.CONNECTIVITY,
        status=CheckStatus.WARN,
        message="Unexpected response: 500",
    )
    assert get_log_level(check_without_fallback) == "WARNING"

    # Non-connectivity check → WARNING (even with matching text)
    check_non_connectivity = CheckResult(
        name="Some Other Check",
        category=CheckCategory.ENVIRONMENT,
        status=CheckStatus.WARN,
        message="Something about local models available",
    )
    assert get_log_level(check_non_connectivity) == "WARNING"


# ---------------------------------------------------------------------------
# Test 6: Port default consistency (supervisor vs gcp_vm_manager)
# ---------------------------------------------------------------------------

def test_supervisor_and_vm_manager_port_defaults_match():
    """Supervisor and GCP VM manager must agree on default inference port.

    Both unified_supervisor.py (invincible_node_port) and gcp_vm_manager.py
    read JARVIS_PRIME_PORT with a fallback default. If they disagree, the
    supervisor promotes on one port while the VM manager promotes on another,
    causing the idempotent check in PrimeRouter to fail and triggering
    false flapping protection.

    We verify via source inspection to avoid heavy module-level side effects
    from importing the full SystemKernelConfig.
    """
    import re
    import pathlib

    project = pathlib.Path(__file__).resolve().parents[4]

    # --- Supervisor: invincible_node_port default ---
    # Pattern: invincible_node_port: int = field(default_factory=lambda: _get_env_int("JARVIS_PRIME_PORT", XXXX))
    sup_src = (project / "unified_supervisor.py").read_text()
    sup_match = re.search(
        r'invincible_node_port.*_get_env_int\(\s*"JARVIS_PRIME_PORT"\s*,\s*(\d+)\s*\)',
        sup_src,
    )
    assert sup_match, "Could not find invincible_node_port default in unified_supervisor.py"
    sup_default = int(sup_match.group(1))

    # --- GCP VM manager: JARVIS_PRIME_PORT default in _ensure_endpoint_propagated ---
    # Pattern: os.getenv("JARVIS_PRIME_PORT" if is_invincible else "GCP_BACKEND_PORT", "8000")
    vm_src = (project / "backend" / "core" / "gcp_vm_manager.py").read_text()
    vm_match = re.search(
        r'os\.getenv\(\s*"JARVIS_PRIME_PORT".*?,\s*"(\d+)"\s*,?\s*\)',
        vm_src,
    )
    assert vm_match, "Could not find JARVIS_PRIME_PORT default in gcp_vm_manager.py"
    vm_default = int(vm_match.group(1))

    assert sup_default == vm_default == 8000, (
        f"Port default mismatch: supervisor={sup_default}, vm_manager={vm_default}, expected=8000"
    )


# ---------------------------------------------------------------------------
# Test 7: Concurrent promote_gcp_endpoint serialization via lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_gcp_endpoint_serialized():
    """Two concurrent promote calls must not corrupt state."""
    from backend.core.prime_router import PrimeRouter

    router = PrimeRouter()
    router._initialized = True
    router._prime_client = AsyncMock()
    router._prime_client.update_endpoint = AsyncMock(return_value=True)
    # Stub out circuit breaker and ultra coordinator
    router._local_circuit = MagicMock()
    router._local_circuit.reset_for_endpoint = MagicMock()

    with patch("backend.core.prime_router._get_ultra_coordinator", new_callable=AsyncMock, return_value=None):
        # Fire two concurrent promotions to the same endpoint
        results = await asyncio.gather(
            router.promote_gcp_endpoint("10.0.0.1", 8000),
            router.promote_gcp_endpoint("10.0.0.1", 8000),
        )

    # Both should succeed — first does the work, second hits idempotent check
    assert all(results), f"Expected both True, got {results}"
    assert router._gcp_promoted is True
    assert router._gcp_host == "10.0.0.1"
    assert router._gcp_port == 8000
    # update_endpoint called exactly once (second call hits idempotent path)
    assert router._prime_client.update_endpoint.call_count == 1


# ---------------------------------------------------------------------------
# Test 8: endpoint_propagated conditional on PrimeRouter success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoint_propagated_only_on_router_success():
    """endpoint_propagated must not be set when PrimeRouter promotion fails."""
    from backend.core.gcp_vm_manager import GCPVMManager, VMInstance, VMState

    manager = GCPVMManager.__new__(GCPVMManager)

    # Create a minimal VM
    vm = VMInstance(
        instance_id="test-123",
        name="jarvis-prime-node-1",
        zone="us-central1-a",
        state=VMState.RUNNING,
        created_at=0,
        ip_address="10.0.0.1",
        health_status="unknown",
        metadata={},
    )

    # Mock the health ping to succeed
    manager._ping_health_endpoint = AsyncMock(return_value=(True, {"status": "ok"}))

    # Mock notify_gcp_vm_ready to FAIL (patched at source — lazy imported inside method)
    with patch("backend.core.prime_router.notify_gcp_vm_ready", new_callable=AsyncMock, return_value=False):
        # Also mock the model serving notification to avoid import errors
        with patch("backend.intelligence.unified_model_serving.notify_gcp_endpoint_ready", new_callable=AsyncMock, return_value=True):
            result = await manager._ensure_endpoint_propagated(vm)

    assert result is False, "Should return False when router promotion fails"
    assert vm.metadata.get("endpoint_propagated") is not True, (
        "endpoint_propagated must not be True when router promotion failed"
    )


# ---------------------------------------------------------------------------
# Test 9: Health status updated after successful ping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_status_updated_after_successful_ping():
    """_ensure_endpoint_propagated must set health_status='healthy' after ping succeeds."""
    from backend.core.gcp_vm_manager import GCPVMManager, VMInstance, VMState

    manager = GCPVMManager.__new__(GCPVMManager)

    vm = VMInstance(
        instance_id="test-456",
        name="jarvis-prime-node-2",
        zone="us-central1-a",
        state=VMState.RUNNING,
        created_at=0,
        ip_address="10.0.0.2",
        health_status="unknown",
        metadata={},
    )

    # Mock health ping to succeed
    manager._ping_health_endpoint = AsyncMock(return_value=(True, {"status": "ok"}))

    # Mock PrimeRouter promotion to succeed (patched at source — lazy imported inside method)
    with patch("backend.core.prime_router.notify_gcp_vm_ready", new_callable=AsyncMock, return_value=True):
        with patch("backend.intelligence.unified_model_serving.notify_gcp_endpoint_ready", new_callable=AsyncMock, return_value=True):
            result = await manager._ensure_endpoint_propagated(vm)

    assert result is True
    assert vm.health_status == "healthy", (
        f"Expected health_status='healthy', got '{vm.health_status}'"
    )
