import importlib
from pathlib import Path

import pytest

from backend.api.vision_command_handler import (
    VisionCommandHandler,
    VisionDescribeResult,
)
from backend.api.vision_websocket import VisionManager
from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer


@pytest.mark.asyncio
async def test_describe_screen_returns_backward_compatible_result(monkeypatch):
    handler = VisionCommandHandler()

    async def fake_analyze_screen(command_text):
        return {
            "handled": True,
            "response": f"analysis:{command_text}",
            "metadata": {"source": "test"},
        }

    monkeypatch.setattr(handler, "analyze_screen", fake_analyze_screen)

    result = await handler.describe_screen({"query": "status check"})

    assert isinstance(result, VisionDescribeResult)
    assert isinstance(result, dict)
    assert result.success is True
    assert result["success"] is True
    assert result.description == "analysis:status check"
    assert result.data["handled"] is True
    assert result.data["query"] == "status check"
    assert result.error is None


@pytest.mark.asyncio
async def test_describe_screen_handles_errors_without_raising(monkeypatch):
    handler = VisionCommandHandler()

    async def fake_analyze_screen(_command_text):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler, "analyze_screen", fake_analyze_screen)

    result = await handler.describe_screen({"query": "trigger error"})

    assert result.success is False
    assert result.error == "boom"
    assert "error" in result.description.lower()


def test_continuous_analyzer_validates_handler_contract():
    class InvalidHandler:
        async def capture_screen(self):
            return None

    with pytest.raises(TypeError, match="describe_screen"):
        MemoryAwareScreenAnalyzer(InvalidHandler())


def test_vision_websocket_aliases_share_singleton(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    backend_root = repo_root / "backend"

    monkeypatch.syspath_prepend(str(repo_root))
    monkeypatch.syspath_prepend(str(backend_root))

    backend_module = importlib.import_module("backend.api.vision_websocket")
    api_module = importlib.import_module("api.vision_websocket")

    assert backend_module is api_module
    assert backend_module.vision_manager is api_module.vision_manager


def test_vision_manager_rejects_analyzer_classes():
    manager = VisionManager()

    class AnalyzerContract:
        async def capture_screen(self, multi_space=False, space_number=None):
            return None

    published = manager.set_vision_analyzer(AnalyzerContract)

    assert published is False
    assert manager.get_vision_analyzer() is None


@pytest.mark.asyncio
async def test_handler_rebinds_invalid_vision_manager_analyzer(monkeypatch):
    import backend.api.vision_websocket as vision_websocket

    manager = vision_websocket.VisionManager()

    class InvalidAnalyzerContract:
        async def capture_screen(self, multi_space=False, space_number=None):
            return None

    class WorkingAnalyzer:
        async def capture_screen(self, multi_space=False, space_number=None):
            return {"ok": True, "multi_space": multi_space, "space_number": space_number}

    manager.vision_analyzer = InvalidAnalyzerContract
    monkeypatch.setattr(vision_websocket, "vision_manager", manager)

    handler = VisionCommandHandler()
    handler.vision_analyzer = WorkingAnalyzer()

    await handler._ensure_vision_manager()

    assert manager.get_vision_analyzer() is handler.vision_analyzer
