"""
Tests for backend.hive.rem_health_scanner

Covers:
- _collect_metrics with mocked psutil returns correct dict
- Healthy system (all < 70%) creates summary thread, 0 calls, no escalation
- Degraded system (RAM 88%) creates warning thread with severity="warning"
- Critical system (RAM 96%, CPU 95%) escalates (should_escalate=True)
- Respects budget (calls_used <= budget)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.hive.rem_health_scanner import HealthScanner
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)


# ============================================================================
# Fixtures
# ============================================================================


def _make_reasoning_msg(thread_id: str) -> MagicMock:
    """Create a mock PersonaReasoningMessage with required fields."""
    msg = MagicMock()
    msg.thread_id = thread_id
    msg.persona = "jarvis"
    msg.role = "body"
    msg.intent = PersonaIntent.OBSERVE
    msg.references = []
    msg.reasoning = "System metrics analysed."
    msg.confidence = 0.85
    msg.model_used = "mock-model"
    msg.token_cost = 100
    msg.type = "persona_reasoning"
    msg.manifesto_principle = None
    msg.validate_verdict = None
    return msg


@pytest.fixture()
def thread_manager() -> ThreadManager:
    """In-memory ThreadManager (no disk persistence)."""
    return ThreadManager(storage_dir=None)


@pytest.fixture()
def persona_engine() -> MagicMock:
    """Mock PersonaEngine whose generate_reasoning returns a MagicMock message."""
    engine = MagicMock()
    engine.generate_reasoning = AsyncMock(
        side_effect=lambda persona, intent, thread: _make_reasoning_msg(
            thread.thread_id
        )
    )
    return engine


@pytest.fixture()
def relay() -> MagicMock:
    """Mock HudRelayAgent."""
    return MagicMock()


@pytest.fixture()
def scanner(
    persona_engine: MagicMock,
    thread_manager: ThreadManager,
    relay: MagicMock,
) -> HealthScanner:
    return HealthScanner(persona_engine, thread_manager, relay)


# ============================================================================
# _collect_metrics
# ============================================================================


class TestCollectMetrics:
    """Verify _collect_metrics translates psutil results correctly."""

    @patch("backend.hive.rem_health_scanner.psutil")
    def test_returns_correct_dict(
        self, mock_psutil: MagicMock, scanner: HealthScanner
    ) -> None:
        mock_psutil.virtual_memory.return_value.percent = 55.3
        mock_psutil.cpu_percent.return_value = 23.7
        mock_psutil.disk_usage.return_value.percent = 62.1

        metrics = scanner._collect_metrics()

        assert metrics == {
            "ram_percent": 55.3,
            "cpu_percent": 23.7,
            "disk_percent": 62.1,
        }

    @patch("backend.hive.rem_health_scanner.psutil", None)
    def test_returns_zeros_without_psutil(self, scanner: HealthScanner) -> None:
        metrics = scanner._collect_metrics()
        assert metrics == {
            "ram_percent": 0.0,
            "cpu_percent": 0.0,
            "disk_percent": 0.0,
        }


# ============================================================================
# _assess
# ============================================================================


class TestAssess:
    """Verify threshold logic in _assess."""

    def test_all_healthy_no_findings(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 50.0, "cpu_percent": 40.0, "disk_percent": 60.0}
        findings = scanner._assess(metrics)
        assert findings == []

    def test_ram_warning(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 88.0, "cpu_percent": 30.0, "disk_percent": 50.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["metric"] == "ram_percent"
        assert findings[0]["severity"] == "warning"
        assert findings[0]["value"] == 88.0

    def test_ram_error(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 96.0, "cpu_percent": 30.0, "disk_percent": 50.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"

    def test_disk_warning(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 30.0, "cpu_percent": 30.0, "disk_percent": 90.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["metric"] == "disk_percent"
        assert findings[0]["severity"] == "warning"

    def test_disk_error(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 30.0, "cpu_percent": 30.0, "disk_percent": 96.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"

    def test_multiple_findings(self, scanner: HealthScanner) -> None:
        metrics = {"ram_percent": 91.0, "cpu_percent": 75.0, "disk_percent": 96.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 3
        severities = {f["metric"]: f["severity"] for f in findings}
        assert severities["ram_percent"] == "error"
        assert severities["cpu_percent"] == "warning"
        assert severities["disk_percent"] == "error"

    def test_boundary_70_is_warning(self, scanner: HealthScanner) -> None:
        """Exactly 70% RAM/CPU should trigger warning (>= 70)."""
        metrics = {"ram_percent": 70.0, "cpu_percent": 69.9, "disk_percent": 50.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["metric"] == "ram_percent"
        assert findings[0]["severity"] == "warning"

    def test_boundary_90_is_warning(self, scanner: HealthScanner) -> None:
        """Exactly 90% RAM/CPU is still warning (not > 90)."""
        metrics = {"ram_percent": 90.0, "cpu_percent": 50.0, "disk_percent": 50.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"

    def test_boundary_90_point_1_is_error(self, scanner: HealthScanner) -> None:
        """90.1% RAM/CPU should be error (> 90)."""
        metrics = {"ram_percent": 90.1, "cpu_percent": 50.0, "disk_percent": 50.0}
        findings = scanner._assess(metrics)
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"


# ============================================================================
# run() — healthy system
# ============================================================================


class TestRunHealthy:
    """Healthy system: all clear summary thread, 0 calls, no escalation."""

    @pytest.mark.asyncio
    @patch("backend.hive.rem_health_scanner.psutil")
    async def test_healthy_system_summary(
        self,
        mock_psutil: MagicMock,
        scanner: HealthScanner,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        mock_psutil.virtual_memory.return_value.percent = 40.0
        mock_psutil.cpu_percent.return_value = 25.0
        mock_psutil.disk_usage.return_value.percent = 50.0

        thread_ids, calls_used, should_escalate, escalation_id = await scanner.run(
            budget=5
        )

        assert len(thread_ids) == 1
        assert calls_used == 0
        assert should_escalate is False
        assert escalation_id is None

        # Verify the summary thread.
        thread = thread_manager.get_thread(thread_ids[0])
        assert thread is not None
        assert thread.title == "System Health: All Clear"
        assert thread.state == ThreadState.OPEN
        assert len(thread.messages) == 1
        assert isinstance(thread.messages[0], AgentLogMessage)
        assert thread.messages[0].severity == "info"
        assert thread.messages[0].payload["ram_percent"] == 40.0

        # Persona engine should NOT have been called.
        persona_engine.generate_reasoning.assert_not_called()


# ============================================================================
# run() — degraded system
# ============================================================================


class TestRunDegraded:
    """Degraded system: warning-level findings create threads with reasoning."""

    @pytest.mark.asyncio
    @patch("backend.hive.rem_health_scanner.psutil")
    async def test_ram_warning_creates_thread(
        self,
        mock_psutil: MagicMock,
        scanner: HealthScanner,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        mock_psutil.virtual_memory.return_value.percent = 88.0
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.disk_usage.return_value.percent = 50.0

        thread_ids, calls_used, should_escalate, escalation_id = await scanner.run(
            budget=5
        )

        assert len(thread_ids) == 1
        assert calls_used == 1
        assert should_escalate is False
        assert escalation_id is None

        thread = thread_manager.get_thread(thread_ids[0])
        assert thread is not None
        assert "ram_percent" in thread.title
        assert "88" in thread.title
        assert thread.state == ThreadState.DEBATING

        # First message: AgentLogMessage with warning severity.
        assert isinstance(thread.messages[0], AgentLogMessage)
        assert thread.messages[0].severity == "warning"

        # Second message: reasoning from persona engine.
        assert len(thread.messages) == 2
        persona_engine.generate_reasoning.assert_called_once()


# ============================================================================
# run() — critical system (escalation)
# ============================================================================


class TestRunCritical:
    """Critical system: error-level findings trigger escalation."""

    @pytest.mark.asyncio
    @patch("backend.hive.rem_health_scanner.psutil")
    async def test_critical_system_escalates(
        self,
        mock_psutil: MagicMock,
        scanner: HealthScanner,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        mock_psutil.virtual_memory.return_value.percent = 96.0
        mock_psutil.cpu_percent.return_value = 95.0
        mock_psutil.disk_usage.return_value.percent = 50.0

        thread_ids, calls_used, should_escalate, escalation_id = await scanner.run(
            budget=5
        )

        # Two error findings: ram_percent + cpu_percent.
        assert len(thread_ids) == 2
        assert calls_used == 2
        assert should_escalate is True
        assert escalation_id is not None
        # escalation_id should be one of the thread IDs.
        assert escalation_id in thread_ids

        # Both threads should be in DEBATING state.
        for tid in thread_ids:
            thread = thread_manager.get_thread(tid)
            assert thread is not None
            assert thread.state == ThreadState.DEBATING


# ============================================================================
# run() — budget enforcement
# ============================================================================


class TestBudgetEnforcement:
    """Verify calls_used never exceeds budget."""

    @pytest.mark.asyncio
    @patch("backend.hive.rem_health_scanner.psutil")
    async def test_respects_budget(
        self,
        mock_psutil: MagicMock,
        scanner: HealthScanner,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        # All three metrics in error/warning — would need 3 calls.
        mock_psutil.virtual_memory.return_value.percent = 96.0
        mock_psutil.cpu_percent.return_value = 95.0
        mock_psutil.disk_usage.return_value.percent = 96.0

        thread_ids, calls_used, should_escalate, escalation_id = await scanner.run(
            budget=1
        )

        assert calls_used == 1
        assert calls_used <= 1
        assert len(thread_ids) == 1

    @pytest.mark.asyncio
    @patch("backend.hive.rem_health_scanner.psutil")
    async def test_zero_budget_no_calls(
        self,
        mock_psutil: MagicMock,
        scanner: HealthScanner,
        persona_engine: MagicMock,
        thread_manager: ThreadManager,
    ) -> None:
        mock_psutil.virtual_memory.return_value.percent = 96.0
        mock_psutil.cpu_percent.return_value = 95.0
        mock_psutil.disk_usage.return_value.percent = 96.0

        thread_ids, calls_used, should_escalate, escalation_id = await scanner.run(
            budget=0
        )

        assert calls_used == 0
        assert len(thread_ids) == 0
        persona_engine.generate_reasoning.assert_not_called()
