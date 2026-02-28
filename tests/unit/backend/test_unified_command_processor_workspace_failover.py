from __future__ import annotations

import time
from types import SimpleNamespace


async def test_workspace_command_failsover_when_jprime_unreachable(monkeypatch):
    from backend.api.unified_command_processor import UnifiedCommandProcessor

    processor = UnifiedCommandProcessor()

    async def _no_reflex(_command_text):
        return None

    async def _no_jprime(_command_text, deadline=None, source_context=None):
        return None

    async def _fake_workspace_action(_command_text, _response, deadline=None):
        return {
            "success": True,
            "response": "You have 2 unread emails.",
            "command_type": "WORKSPACE",
            "source": "workspace_failover",
        }

    class _Detector:
        async def detect(self, _query: str):
            return SimpleNamespace(
                is_workspace_command=True,
                intent=SimpleNamespace(value="check_email"),
                confidence=0.95,
                entities={},
            )

    monkeypatch.setattr(processor, "_check_reflex_manifest", _no_reflex)
    monkeypatch.setattr(processor, "_call_jprime", _no_jprime)
    monkeypatch.setattr(processor, "_handle_workspace_action", _fake_workspace_action)

    import backend.core.workspace_routing_intelligence as wri

    monkeypatch.setattr(wri, "get_workspace_detector", lambda: _Detector())

    result = await processor._execute_command_pipeline("check my email")

    assert result["success"] is True
    assert result["command_type"] == "WORKSPACE"
    assert result["source"] == "workspace_failover"


async def test_workspace_command_skips_jprime_when_budget_too_low(monkeypatch):
    from backend.api.unified_command_processor import UnifiedCommandProcessor

    processor = UnifiedCommandProcessor()
    calls = {"jprime": 0}

    async def _no_reflex(_command_text):
        return None

    async def _should_not_call_jprime(_command_text, deadline=None, source_context=None):
        calls["jprime"] += 1
        return None

    async def _fake_workspace_action(_command_text, _response, deadline=None):
        return {
            "success": True,
            "response": "You have 3 unread emails.",
            "command_type": "WORKSPACE",
            "source": "workspace_failover",
        }

    class _Detector:
        async def detect(self, _query: str):
            return SimpleNamespace(
                is_workspace_command=True,
                intent=SimpleNamespace(value="check_email"),
                confidence=0.97,
                entities={},
            )

    monkeypatch.setattr(processor, "_check_reflex_manifest", _no_reflex)
    monkeypatch.setattr(processor, "_call_jprime", _should_not_call_jprime)
    monkeypatch.setattr(processor, "_handle_workspace_action", _fake_workspace_action)
    monkeypatch.setenv("JARVIS_JPRIME_MIN_BUDGET_SECONDS", "2.5")

    import backend.core.workspace_routing_intelligence as wri

    monkeypatch.setattr(wri, "get_workspace_detector", lambda: _Detector())

    deadline = time.monotonic() + 0.1
    result = await processor._execute_command_pipeline("check my email", deadline=deadline)

    assert result["success"] is True
    assert result["command_type"] == "WORKSPACE"
    assert calls["jprime"] == 0

