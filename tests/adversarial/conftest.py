"""Shared fixtures for adversarial tests."""
import tempfile
import pytest
from pathlib import Path


@pytest.fixture
def fault_injector():
    from tests.adversarial.fault_injector import FaultInjector
    return FaultInjector()


@pytest.fixture
def mock_clock():
    from tests.adversarial.fault_injector import MockClock
    return MockClock()


@pytest.fixture
def tmp_trace_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def envelope_factory():
    from backend.core.trace_envelope import TraceEnvelopeFactory
    return TraceEnvelopeFactory(
        repo="jarvis", boot_id="test-boot",
        runtime_epoch_id="test-epoch",
        node_id="test-node", producer_version="test-v1",
    )
