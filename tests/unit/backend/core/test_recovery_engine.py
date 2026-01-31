"""
Tests for RecoveryEngine - Error classification and recovery strategy selection.

Tests cover:
- All enum values
- ErrorClassifier.classify() for each exception type
- RecoveryEngine.handle_failure() scenarios
- Exponential backoff delay calculation
- reset_attempts()
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass

# Import components we're testing (will fail until implemented)
from backend.core.recovery_engine import (
    ErrorClass,
    RecoveryPhase,
    RecoveryStrategy,
    ErrorClassification,
    ErrorClassifier,
    RecoveryAction,
    RecoveryEngine,
)
from backend.core.component_registry import (
    ComponentRegistry,
    ComponentDefinition,
    ComponentState,
    Criticality,
    ProcessType,
    FallbackStrategy,
)


class TestErrorClassEnum:
    """Test ErrorClass enum values."""

    def test_transient_network_value(self):
        assert ErrorClass.TRANSIENT_NETWORK.value == "transient_network"

    def test_needs_fallback_value(self):
        assert ErrorClass.NEEDS_FALLBACK.value == "needs_fallback"

    def test_missing_resource_value(self):
        assert ErrorClass.MISSING_RESOURCE.value == "missing_resource"

    def test_resource_exhaustion_value(self):
        assert ErrorClass.RESOURCE_EXHAUSTION.value == "resource_exhaustion"

    def test_all_enum_members(self):
        """Verify all expected enum members exist."""
        members = [e.name for e in ErrorClass]
        assert "TRANSIENT_NETWORK" in members
        assert "NEEDS_FALLBACK" in members
        assert "MISSING_RESOURCE" in members
        assert "RESOURCE_EXHAUSTION" in members


class TestRecoveryPhaseEnum:
    """Test RecoveryPhase enum values."""

    def test_startup_value(self):
        assert RecoveryPhase.STARTUP.value == "startup"

    def test_runtime_value(self):
        assert RecoveryPhase.RUNTIME.value == "runtime"

    def test_all_enum_members(self):
        members = [e.name for e in RecoveryPhase]
        assert "STARTUP" in members
        assert "RUNTIME" in members


class TestRecoveryStrategyEnum:
    """Test RecoveryStrategy enum values."""

    def test_full_restart_value(self):
        assert RecoveryStrategy.FULL_RESTART.value == "full_restart"

    def test_fallback_mode_value(self):
        assert RecoveryStrategy.FALLBACK_MODE.value == "fallback_mode"

    def test_disable_and_continue_value(self):
        assert RecoveryStrategy.DISABLE_AND_CONTINUE.value == "disable"

    def test_escalate_to_user_value(self):
        assert RecoveryStrategy.ESCALATE_TO_USER.value == "escalate"

    def test_all_enum_members(self):
        members = [e.name for e in RecoveryStrategy]
        assert "FULL_RESTART" in members
        assert "FALLBACK_MODE" in members
        assert "DISABLE_AND_CONTINUE" in members
        assert "ESCALATE_TO_USER" in members


class TestErrorClassification:
    """Test ErrorClassification dataclass."""

    def test_create_classification(self):
        classification = ErrorClassification(
            error_class=ErrorClass.TRANSIENT_NETWORK,
            suggested_strategy=RecoveryStrategy.FULL_RESTART,
            is_retryable=True,
            needs_fallback=False,
        )
        assert classification.error_class == ErrorClass.TRANSIENT_NETWORK
        assert classification.suggested_strategy == RecoveryStrategy.FULL_RESTART
        assert classification.is_retryable is True
        assert classification.needs_fallback is False

    def test_needs_fallback_classification(self):
        classification = ErrorClassification(
            error_class=ErrorClass.NEEDS_FALLBACK,
            suggested_strategy=RecoveryStrategy.FALLBACK_MODE,
            is_retryable=False,
            needs_fallback=True,
        )
        assert classification.needs_fallback is True
        assert classification.is_retryable is False


class TestRecoveryAction:
    """Test RecoveryAction dataclass."""

    def test_create_simple_action(self):
        action = RecoveryAction(strategy=RecoveryStrategy.FULL_RESTART)
        assert action.strategy == RecoveryStrategy.FULL_RESTART
        assert action.delay == 0.0
        assert action.fallback_targets == {}
        assert action.message is None

    def test_create_action_with_delay(self):
        action = RecoveryAction(
            strategy=RecoveryStrategy.FULL_RESTART,
            delay=5.0,
        )
        assert action.delay == 5.0

    def test_create_action_with_fallback_targets(self):
        action = RecoveryAction(
            strategy=RecoveryStrategy.FALLBACK_MODE,
            fallback_targets={"tts": "cloud_tts"},
        )
        assert action.fallback_targets == {"tts": "cloud_tts"}

    def test_create_action_with_message(self):
        action = RecoveryAction(
            strategy=RecoveryStrategy.ESCALATE_TO_USER,
            message="Critical component failed, manual intervention required",
        )
        assert action.message == "Critical component failed, manual intervention required"


class TestErrorClassifier:
    """Test ErrorClassifier.classify() method."""

    @pytest.fixture
    def classifier(self):
        return ErrorClassifier()

    def test_classify_connection_refused_error(self, classifier):
        error = ConnectionRefusedError("Connection refused")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.TRANSIENT_NETWORK
        assert classification.is_retryable is True

    def test_classify_timeout_error(self, classifier):
        error = TimeoutError("Connection timed out")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.TRANSIENT_NETWORK
        assert classification.is_retryable is True

    def test_classify_file_not_found_error(self, classifier):
        error = FileNotFoundError("File not found")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.MISSING_RESOURCE
        assert classification.is_retryable is False

    def test_classify_memory_error(self, classifier):
        error = MemoryError("Out of memory")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.RESOURCE_EXHAUSTION
        assert classification.is_retryable is False

    def test_classify_cloud_offload_required(self, classifier):
        """Test classification via error message matching."""
        error = RuntimeError("CloudOffloadRequired: Local resources insufficient")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.NEEDS_FALLBACK
        assert classification.needs_fallback is True

    def test_classify_gpu_not_available(self, classifier):
        """Test classification via error message matching."""
        error = RuntimeError("GPUNotAvailable: No GPU found")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.NEEDS_FALLBACK
        assert classification.needs_fallback is True

    def test_classify_unknown_error(self, classifier):
        """Unknown errors should default to transient network (retryable)."""
        error = RuntimeError("Some unknown error")
        classification = classifier.classify(error)
        # Unknown errors should be treated as potentially transient
        assert classification.is_retryable is True

    def test_classify_oserror_connection(self, classifier):
        """Test OSError with connection-related errno."""
        error = OSError(111, "Connection refused")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.TRANSIENT_NETWORK

    def test_classify_oserror_disk_full(self, classifier):
        """Test OSError with disk space errno."""
        error = OSError(28, "No space left on device")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.RESOURCE_EXHAUSTION


class TestRecoveryEngine:
    """Test RecoveryEngine.handle_failure() method."""

    @pytest.fixture
    def registry(self):
        registry = ComponentRegistry()
        registry._reset_for_testing()
        return registry

    @pytest.fixture
    def classifier(self):
        return ErrorClassifier()

    @pytest.fixture
    def engine(self, registry, classifier):
        return RecoveryEngine(registry=registry, error_classifier=classifier)

    def _create_component(
        self,
        registry: ComponentRegistry,
        name: str = "test_component",
        criticality: Criticality = Criticality.OPTIONAL,
        retry_max_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
        fallback_for_capabilities: dict = None,
    ) -> ComponentDefinition:
        """Helper to create and register a component."""
        definition = ComponentDefinition(
            name=name,
            criticality=criticality,
            process_type=ProcessType.IN_PROCESS,
            retry_max_attempts=retry_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            fallback_for_capabilities=fallback_for_capabilities or {},
        )
        registry.register(definition)
        return definition

    @pytest.mark.asyncio
    async def test_transient_error_with_retries_available(self, registry, engine):
        """Transient error with retries available should return FULL_RESTART with delay."""
        self._create_component(registry, "test_component", retry_max_attempts=3)

        error = ConnectionRefusedError("Connection refused")
        action = await engine.handle_failure(
            component="test_component",
            error=error,
            phase=RecoveryPhase.STARTUP,
        )

        assert action.strategy == RecoveryStrategy.FULL_RESTART
        assert action.delay > 0  # Should have exponential backoff delay

    @pytest.mark.asyncio
    async def test_transient_error_retries_exhausted_optional(self, registry, engine):
        """Optional component with exhausted retries should DISABLE_AND_CONTINUE."""
        self._create_component(
            registry,
            "optional_component",
            criticality=Criticality.OPTIONAL,
            retry_max_attempts=2,
        )

        error = TimeoutError("Timeout")
        # Exhaust retries
        await engine.handle_failure("optional_component", error, RecoveryPhase.STARTUP)
        await engine.handle_failure("optional_component", error, RecoveryPhase.STARTUP)
        action = await engine.handle_failure("optional_component", error, RecoveryPhase.STARTUP)

        assert action.strategy == RecoveryStrategy.DISABLE_AND_CONTINUE

    @pytest.mark.asyncio
    async def test_transient_error_retries_exhausted_required(self, registry, engine):
        """Required component with exhausted retries should ESCALATE_TO_USER."""
        self._create_component(
            registry,
            "required_component",
            criticality=Criticality.REQUIRED,
            retry_max_attempts=2,
        )

        error = ConnectionRefusedError("Connection refused")
        # Exhaust retries
        await engine.handle_failure("required_component", error, RecoveryPhase.STARTUP)
        await engine.handle_failure("required_component", error, RecoveryPhase.STARTUP)
        action = await engine.handle_failure("required_component", error, RecoveryPhase.STARTUP)

        assert action.strategy == RecoveryStrategy.ESCALATE_TO_USER
        assert action.message is not None

    @pytest.mark.asyncio
    async def test_needs_fallback_with_fallback_available(self, registry, engine):
        """Error needing fallback with fallback available should use FALLBACK_MODE."""
        self._create_component(
            registry,
            "local_tts",
            fallback_for_capabilities={"tts": "cloud_tts"},
        )

        error = RuntimeError("GPUNotAvailable: No GPU found")
        action = await engine.handle_failure("local_tts", error, RecoveryPhase.STARTUP)

        assert action.strategy == RecoveryStrategy.FALLBACK_MODE
        assert action.fallback_targets == {"tts": "cloud_tts"}

    @pytest.mark.asyncio
    async def test_needs_fallback_without_fallback(self, registry, engine):
        """Error needing fallback without fallback configured should retry or disable."""
        self._create_component(
            registry,
            "no_fallback_component",
            criticality=Criticality.OPTIONAL,
            fallback_for_capabilities={},
        )

        error = RuntimeError("GPUNotAvailable: No GPU found")
        # Exhaust retries since no fallback
        await engine.handle_failure("no_fallback_component", error, RecoveryPhase.STARTUP)
        await engine.handle_failure("no_fallback_component", error, RecoveryPhase.STARTUP)
        await engine.handle_failure("no_fallback_component", error, RecoveryPhase.STARTUP)
        action = await engine.handle_failure("no_fallback_component", error, RecoveryPhase.STARTUP)

        assert action.strategy == RecoveryStrategy.DISABLE_AND_CONTINUE

    @pytest.mark.asyncio
    async def test_missing_resource_not_retryable(self, registry, engine):
        """Missing resource error should not be retried, go straight to disable/escalate."""
        self._create_component(
            registry,
            "file_component",
            criticality=Criticality.OPTIONAL,
            retry_max_attempts=3,
        )

        error = FileNotFoundError("Config file not found")
        action = await engine.handle_failure("file_component", error, RecoveryPhase.STARTUP)

        # Non-retryable error should skip retries
        assert action.strategy == RecoveryStrategy.DISABLE_AND_CONTINUE

    @pytest.mark.asyncio
    async def test_missing_resource_required_component(self, registry, engine):
        """Required component with missing resource should escalate immediately."""
        self._create_component(
            registry,
            "required_file_component",
            criticality=Criticality.REQUIRED,
        )

        error = FileNotFoundError("Required config file not found")
        action = await engine.handle_failure(
            "required_file_component", error, RecoveryPhase.STARTUP
        )

        assert action.strategy == RecoveryStrategy.ESCALATE_TO_USER

    @pytest.mark.asyncio
    async def test_exponential_backoff_delay(self, registry, engine):
        """Test that delay increases exponentially with attempts."""
        self._create_component(
            registry,
            "backoff_component",
            retry_max_attempts=5,
            retry_delay_seconds=2.0,
        )

        error = TimeoutError("Timeout")
        delays = []

        # Collect delays for multiple attempts
        for _ in range(3):
            action = await engine.handle_failure(
                "backoff_component", error, RecoveryPhase.STARTUP
            )
            if action.strategy == RecoveryStrategy.FULL_RESTART:
                delays.append(action.delay)

        # Verify exponential increase: delay = retry_delay * (1.5 ** attempt_count)
        # Attempt 0: 2.0 * 1.5^0 = 2.0
        # Attempt 1: 2.0 * 1.5^1 = 3.0
        # Attempt 2: 2.0 * 1.5^2 = 4.5
        assert len(delays) >= 2
        assert delays[1] > delays[0]
        if len(delays) >= 3:
            assert delays[2] > delays[1]
            # Check exponential relationship
            ratio_1 = delays[1] / delays[0]
            ratio_2 = delays[2] / delays[1]
            assert abs(ratio_1 - 1.5) < 0.1
            assert abs(ratio_2 - 1.5) < 0.1

    @pytest.mark.asyncio
    async def test_reset_attempts(self, registry, engine):
        """Test that reset_attempts() clears the attempt counter."""
        self._create_component(
            registry,
            "reset_component",
            retry_max_attempts=2,
        )

        error = TimeoutError("Timeout")

        # Make attempts
        await engine.handle_failure("reset_component", error, RecoveryPhase.STARTUP)
        await engine.handle_failure("reset_component", error, RecoveryPhase.STARTUP)

        # Reset
        engine.reset_attempts("reset_component")

        # Should be able to retry again
        action = await engine.handle_failure("reset_component", error, RecoveryPhase.STARTUP)
        assert action.strategy == RecoveryStrategy.FULL_RESTART

    @pytest.mark.asyncio
    async def test_runtime_phase_vs_startup_phase(self, registry, engine):
        """Test behavior differences between phases."""
        self._create_component(
            registry,
            "runtime_component",
            criticality=Criticality.DEGRADED_OK,
        )

        error = ConnectionRefusedError("Connection refused")

        # Runtime failures might have different behavior
        action = await engine.handle_failure(
            "runtime_component", error, RecoveryPhase.RUNTIME
        )

        assert action.strategy in [
            RecoveryStrategy.FULL_RESTART,
            RecoveryStrategy.DISABLE_AND_CONTINUE,
        ]

    @pytest.mark.asyncio
    async def test_degraded_ok_component_behavior(self, registry, engine):
        """Test DEGRADED_OK components continue without escalation."""
        self._create_component(
            registry,
            "degraded_ok_component",
            criticality=Criticality.DEGRADED_OK,
            retry_max_attempts=1,
        )

        error = TimeoutError("Timeout")

        # Exhaust retries
        await engine.handle_failure("degraded_ok_component", error, RecoveryPhase.STARTUP)
        action = await engine.handle_failure("degraded_ok_component", error, RecoveryPhase.STARTUP)

        # DEGRADED_OK should continue, not escalate
        assert action.strategy == RecoveryStrategy.DISABLE_AND_CONTINUE

    @pytest.mark.asyncio
    async def test_component_not_found(self, registry, engine):
        """Test handling of unknown component."""
        error = TimeoutError("Timeout")

        with pytest.raises(KeyError):
            await engine.handle_failure("unknown_component", error, RecoveryPhase.STARTUP)


class TestErrorClassifierEdgeCases:
    """Test edge cases in error classification."""

    @pytest.fixture
    def classifier(self):
        return ErrorClassifier()

    def test_classify_nested_exception(self, classifier):
        """Test classification of nested/wrapped exceptions."""
        inner = ConnectionRefusedError("Inner error")
        outer = RuntimeError("Wrapper")
        outer.__cause__ = inner

        # Should still classify based on outer exception type
        classification = classifier.classify(outer)
        assert classification is not None

    def test_classify_exception_with_errno(self, classifier):
        """Test OSError with specific errno values."""
        # ECONNREFUSED = 111
        error = OSError(111, "Connection refused")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.TRANSIENT_NETWORK

    def test_classify_custom_exception_with_pattern(self, classifier):
        """Test custom exceptions matching patterns in message."""
        error = Exception("CloudOffloadRequired: GPU memory insufficient")
        classification = classifier.classify(error)
        assert classification.error_class == ErrorClass.NEEDS_FALLBACK
