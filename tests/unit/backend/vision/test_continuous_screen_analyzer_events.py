from __future__ import annotations

from collections import defaultdict
from unittest.mock import AsyncMock

from PIL import Image


class _FakeVisionHandler:
    def __init__(self) -> None:
        self._captures = 0

    async def capture_screen(self):
        self._captures += 1
        color_value = min(255, 40 * self._captures)
        return Image.new("RGB", (64, 64), color=(color_value, 20, 10))


async def test_capture_pipeline_emits_app_content_and_capture_events():
    from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer

    analyzer = MemoryAwareScreenAnalyzer(_FakeVisionHandler(), update_interval=0.1)
    counters = defaultdict(int)

    async def _on_screen_captured(_):
        counters["screen_captured"] += 1

    async def _on_app_changed(_):
        counters["app_changed"] += 1

    async def _on_content_changed(_):
        counters["content_changed"] += 1

    analyzer.register_callback("screen_captured", _on_screen_captured)
    analyzer.register_callback("app_changed", _on_app_changed)
    analyzer.register_callback("content_changed", _on_content_changed)

    analyzer._quick_screen_analysis = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"current_app": "Visual Studio Code", "timestamp": 1.0},
            {"current_app": "Safari", "timestamp": 2.0},
        ]
    )
    analyzer._full_screen_analysis = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "success": True,
                "description": "Coding in VS Code with no alerts.",
                "timestamp": 1.0,
                "raw_data": {"text": "Coding in VS Code"},
            },
            {
                "success": True,
                "description": "Safari tab with updated dashboard content.",
                "timestamp": 2.0,
                "raw_data": {"text": "Dashboard updated in Safari"},
            },
        ]
    )

    await analyzer._capture_and_analyze()
    await analyzer._capture_and_analyze()

    assert counters["screen_captured"] == 2
    assert counters["app_changed"] == 1
    assert counters["content_changed"] >= 1


async def test_capture_diff_fallback_emits_content_change_without_full_analysis():
    from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer

    analyzer = MemoryAwareScreenAnalyzer(_FakeVisionHandler(), update_interval=0.1)
    counters = defaultdict(int)

    async def _on_content_changed(_):
        counters["content_changed"] += 1

    analyzer.register_callback("content_changed", _on_content_changed)
    analyzer._quick_screen_analysis = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"current_app": "Safari", "timestamp": 1.0},
            {"current_app": "Safari", "timestamp": 2.0},
        ]
    )
    analyzer._full_screen_analysis = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "description": "Safari dashboard baseline.",
            "timestamp": 1.0,
            "raw_data": {"text": "baseline"},
        }
    )
    analyzer.config["capture_change_threshold"] = 0.01
    full_analysis_decisions = iter([True, False])
    analyzer._needs_full_analysis = lambda _quick: next(full_analysis_decisions)  # type: ignore[method-assign]

    await analyzer._capture_and_analyze()
    await analyzer._capture_and_analyze()

    assert counters["content_changed"] >= 2


async def test_semantic_event_detection_emits_extended_callbacks():
    from backend.vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer

    analyzer = MemoryAwareScreenAnalyzer(_FakeVisionHandler(), update_interval=0.1)
    counters = defaultdict(int)

    def _make_counter(name: str):
        async def _callback(_):
            counters[name] += 1
        return _callback

    callbacks = {
        "notification": _make_counter("notification"),
        "meeting": _make_counter("meeting"),
        "security": _make_counter("security"),
        "help": _make_counter("help"),
    }
    analyzer.register_callback("notification_detected", callbacks["notification"])
    analyzer.register_callback("meeting_detected", callbacks["meeting"])
    analyzer.register_callback("security_concern", callbacks["security"])
    analyzer.register_callback("user_needs_help", callbacks["help"])

    await analyzer._process_screen_events(
        {
            "description": (
                "You have a new message notification. "
                "Meeting in 15 minutes on zoom. "
                "Password required for security verification. "
                "The app is not responding and appears stuck loading."
            ),
            "raw_data": {
                "text": "Message + meeting + security + stuck",
                "visual_elements": ["notification badge", "security prompt"],
            },
        }
    )

    assert counters["notification"] == 1
    assert counters["meeting"] == 1
    assert counters["security"] == 1
    assert counters["help"] == 1
