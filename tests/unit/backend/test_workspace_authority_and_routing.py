"""Tests for workspace routing registry and supervisor authority gating."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


def _make_processor():
    """Create a minimal UnifiedCommandProcessor for authority/routing tests."""
    from backend.api.unified_command_processor import UnifiedCommandProcessor

    with patch.object(UnifiedCommandProcessor, "__init__", lambda self: None):
        proc = UnifiedCommandProcessor.__new__(UnifiedCommandProcessor)
        proc._workspace_agent_singleton = None
        proc._workspace_agent_singleton_lock = asyncio.Lock()
        proc._v242_metrics = {
            "workspace_requests": 0,
            "workspace_standalone_denials": 0,
            "workspace_action_map_misses": 0,
        }
        proc._get_neural_mesh_coordinator = AsyncMock(return_value=None)
        return proc


def test_workspace_routing_registry_is_canonical():
    """Fast-path allowlist must be a subset of the canonical routing registry."""
    from backend.api.unified_command_processor import (
        _WORKSPACE_FASTPATH_INTENTS,
        _WORKSPACE_INTENT_ACTION_MAP,
        _map_workspace_intent_to_action,
    )

    assert _WORKSPACE_FASTPATH_INTENTS <= set(_WORKSPACE_INTENT_ACTION_MAP.keys())
    assert _map_workspace_intent_to_action("daily_briefing") == "workspace_summary"
    assert _map_workspace_intent_to_action("read_email") == "fetch_unread_emails"
    assert _map_workspace_intent_to_action("unknown_intent") == "handle_workspace_query"


@pytest.mark.asyncio
async def test_workspace_action_fails_closed_before_supervisor_ready(tmp_path, monkeypatch):
    """Standalone workspace agent fallback must be blocked until supervisor is ready."""
    proc = _make_processor()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_SUPERVISED", "1")
    monkeypatch.delenv("JARVIS_STARTUP_COMPLETE", raising=False)
    monkeypatch.delenv("JARVIS_WORKSPACE_ALLOW_STANDALONE", raising=False)

    readiness_dir = tmp_path / ".jarvis" / "kernel"
    readiness_dir.mkdir(parents=True, exist_ok=True)
    (readiness_dir / "readiness_state.json").write_text(
        json.dumps({"tier": "starting"}),
        encoding="utf-8",
    )

    response = SimpleNamespace(
        source="unit_test",
        suggested_actions=["fetch_unread_emails"],
    )

    result = await proc._handle_workspace_action("check my email", response)

    assert result["error_code"] == "workspace_authority_unavailable"
    assert proc._v242_metrics["workspace_standalone_denials"] == 1


def test_explicit_workspace_standalone_flag_overrides_supervisor_gate(tmp_path, monkeypatch):
    """Explicit standalone mode is the only supported bypass."""
    proc = _make_processor()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_WORKSPACE_ALLOW_STANDALONE", "true")
    monkeypatch.delenv("JARVIS_SUPERVISED", raising=False)
    monkeypatch.delenv("JARVIS_STARTUP_COMPLETE", raising=False)

    allowed, reason = proc._can_use_standalone_workspace_agent()

    assert allowed is True
    assert reason == "explicit_standalone_mode"
