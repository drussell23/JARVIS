from __future__ import annotations

import logging

import pytest

import backend.autonomy.action_executor as action_executor_mod
from backend.autonomy.action_executor import ActionExecutor
from backend.autonomy.autonomous_decision_engine import (
    ActionCategory,
    ActionPriority,
    AutonomousAction,
)


def _make_action(*, target: str = "security", params: dict | None = None) -> AutonomousAction:
    return AutonomousAction(
        action_type="security_alert",
        target=target,
        params=params or {},
        priority=ActionPriority.CRITICAL,
        confidence=0.95,
        category=ActionCategory.SECURITY,
        reasoning="Security signal",
    )


def _make_executor(monkeypatch: pytest.MonkeyPatch) -> ActionExecutor:
    class _StubMacOSController:
        async def hide_application(self, _app: str) -> None:
            return None

        async def take_screenshot(self, _name: str) -> str:
            return "stub.png"

    monkeypatch.setattr(action_executor_mod, "MacOSController", _StubMacOSController)
    return ActionExecutor()


def test_resolve_security_context_defaults_to_system_general(monkeypatch: pytest.MonkeyPatch):
    executor = _make_executor(monkeypatch)
    action = _make_action(target="unknown", params={"concern_type": "general"})

    app, concern_type, context_resolved = executor._resolve_security_alert_context(action)
    assert app == "system"
    assert concern_type == "general"
    assert context_resolved is False


def test_resolve_security_context_uses_alias_fields(monkeypatch: pytest.MonkeyPatch):
    executor = _make_executor(monkeypatch)
    action = _make_action(
        target="security",
        params={"source_app": "Discord", "alert_type": "suspicious_login"},
    )

    app, concern_type, context_resolved = executor._resolve_security_alert_context(action)
    assert app == "Discord"
    assert concern_type == "suspicious_login"
    assert context_resolved is True


@pytest.mark.asyncio
async def test_handle_security_alert_general_unknown_is_not_warning(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _make_executor(monkeypatch)
    action = _make_action(target="unknown", params={"concern_type": "general"})

    with caplog.at_level(logging.INFO):
        result = await executor._handle_security_alert(action)

    assert result["success"] is True
    assert result["app"] == "system"
    assert result["concern_type"] == "general"
    assert result["context_resolved"] is False
    assert "Security alert: general in unknown" not in caplog.text


@pytest.mark.asyncio
async def test_handle_security_alert_unresolved_high_risk_is_info(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    executor = _make_executor(monkeypatch)
    action = _make_action(
        target="unknown",
        params={"concern_type": "suspicious_login"},
    )

    with caplog.at_level(logging.INFO):
        result = await executor._handle_security_alert(action)

    assert result["success"] is True
    assert result["context_resolved"] is False
    assert "Security alert: suspicious_login in unknown" not in caplog.text
