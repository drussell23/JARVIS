from __future__ import annotations

from typing import Any, Callable, Dict


class _CallbackSet:
    def __init__(self):
        self._items = set()

    def add(self, callback: Callable) -> None:
        self._items.add(callback)

    def discard(self, callback: Callable) -> None:
        self._items.discard(callback)

    def __len__(self) -> int:
        return len(self._items)


class _FakeAnalyzer:
    def __init__(self):
        callback_names = [
            "error_detected",
            "content_changed",
            "app_changed",
            "user_needs_help",
            "memory_warning",
            "notification_detected",
            "meeting_detected",
            "security_concern",
            "screen_captured",
        ]
        self.event_callbacks: Dict[str, _CallbackSet] = {
            name: _CallbackSet() for name in callback_names
        }

    def register_callback(self, event_type: str, callback: Callable) -> None:
        self.event_callbacks[event_type].add(callback)

    def unregister_callback(self, event_type: str, callback: Callable) -> None:
        self.event_callbacks[event_type].discard(callback)

    def get_event_stats(self) -> Dict[str, Any]:
        return {"emitted": {}, "suppressed": {}}


async def test_bridge_registers_and_unregisters_all_callbacks():
    from backend.agi_os.jarvis_integration import ScreenAnalyzerBridge

    bridge = ScreenAnalyzerBridge()
    analyzer = _FakeAnalyzer()
    bridge._analyzer = analyzer

    await bridge._register_callbacks()
    stats = bridge.get_stats()

    assert stats["callback_expected_count"] == 9
    assert stats["callback_registered_count"] == 9
    assert stats["callback_missing_types"] == []
    for callback_set in analyzer.event_callbacks.values():
        assert len(callback_set) == 1

    bridge._connected = True
    await bridge.disconnect()

    post_stats = bridge.get_stats()
    assert post_stats["callback_registered_count"] == 0
    for callback_set in analyzer.event_callbacks.values():
        assert len(callback_set) == 0
