import pytest
from unittest.mock import MagicMock, patch

from backend.neural_mesh.registry.agent_registry import AgentRegistry, AgentCapabilityIndex
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
from backend.neural_mesh.data_models import CapabilityManifest


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset AgentRegistry and AgentCapabilityIndex singletons before each test."""
    AgentRegistry._instance = None
    AgentCapabilityIndex._instance = None
    yield
    AgentRegistry._instance = None
    AgentCapabilityIndex._instance = None


@pytest.fixture()
def registry():
    return AgentRegistry()


def test_gap_emitted_on_universal_fallback(registry):
    """When resolve_capability falls through to computer_use with no fallback, a gap event is emitted."""
    emitted = []

    mock_bus = MagicMock()
    mock_bus.emit.side_effect = lambda e: emitted.append(e)

    with patch(
        "backend.neural_mesh.registry.agent_registry.get_gap_signal_bus",
        return_value=mock_bus,
    ):
        result = registry.resolve_capability(
            goal="do something completely unknown xyz",
            target_app="nonexistent_app_xyz",
            task_type="nonexistent_task_xyz",
        )

    assert result[0] == "computer_use"
    assert len(emitted) == 1
    evt = emitted[0]
    assert isinstance(evt, CapabilityGapEvent)
    assert evt.source == "primary_fallback"
    assert evt.task_type == "nonexistent_task_xyz"


def test_signature_unchanged(registry):
    """resolve_capability must keep its original 3-argument signature."""
    import inspect
    sig = inspect.signature(registry.resolve_capability)
    params = list(sig.parameters.keys())
    assert "goal" in params
    assert "target_app" in params
    # session_id and command_id must NOT be added as parameters
    assert "session_id" not in params
    assert "command_id" not in params


def test_no_gap_when_capability_found(registry):
    """When a manifest matches the request, no gap event is emitted."""
    # Seed a manifest so 'chrome' / 'browser_navigation' resolves normally
    index = AgentCapabilityIndex()
    index._manifests = {
        "chrome_agent": CapabilityManifest(
            agent_name="chrome_agent",
            agent_type="browser",
            capabilities={"browser_navigation"},
            supported_apps=["chrome", "Google Chrome"],
            supported_task_types=["browser_navigation"],
        )
    }

    emitted = []

    mock_bus = MagicMock()
    mock_bus.emit.side_effect = lambda e: emitted.append(e)

    with patch(
        "backend.neural_mesh.registry.agent_registry.get_gap_signal_bus",
        return_value=mock_bus,
    ):
        registry.resolve_capability(
            goal="open google chrome",
            target_app="chrome",
            task_type="browser_navigation",
        )

    assert len(emitted) == 0


def test_das_canary_key_generation():
    """das_canary_key = sha256(session_id:normalized_command) — verify formula."""
    import hashlib
    import re

    def _normalize_command(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    session_id = "test-session-abc"
    command_text = "  Open My Email  "
    # Pre-computed oracle: sha256("test-session-abc:open my email")
    expected = "6310ac6e73d38d72990dd5cd62dcd59a117917da963fd9a0c01fd21b45d1714a"

    result = hashlib.sha256(
        f"{session_id}:{_normalize_command(command_text)}".encode()
    ).hexdigest()
    assert result == expected
    assert len(result) == 64
