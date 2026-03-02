"""Tests for auth state machine behavioral transitions."""
import asyncio
import os
import pytest
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


def _make_client():
    """Create a GoogleWorkspaceClient with mocked dependencies."""
    from backend.neural_mesh.agents.google_workspace_agent import (
        GoogleWorkspaceClient,
        GoogleWorkspaceConfig,
        AuthState,
    )
    config = GoogleWorkspaceConfig()
    with patch.object(GoogleWorkspaceClient, '__init__', lambda self, cfg: None):
        client = GoogleWorkspaceClient.__new__(GoogleWorkspaceClient)
        client.config = config
        client._auth_state = AuthState.AUTHENTICATED
        client._creds = MagicMock()
        client._last_auth_failure_reason = None
        client._token_health = MagicMock()
        client._token_mtime = None
        client._auth_transition_lock = asyncio.Lock()
        client._refresh_attempts = 0
        client._max_refresh_attempts = 3
        client._auth_probe_count = 0
        client._auth_probe_max = 30
        client._last_auth_probe = 0.0
        client._reauth_notice_cooldown = 0.0
        client._auth_autoheal_total = 0
        client._auth_permanent_fail_total = 0
        client._v2_enabled = True
        return client


class TestRefreshingState:
    """AUTHENTICATED -> REFRESHING -> AUTHENTICATED or DEGRADED_VISUAL."""

    @pytest.mark.asyncio
    async def test_transient_failure_stays_refreshing(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        client._refresh_attempts = 0
        await client._handle_auth_event("transient_failure")
        assert client._auth_state == AuthState.REFRESHING
        assert client._refresh_attempts == 1

    @pytest.mark.asyncio
    async def test_permanent_failure_transitions_to_degraded(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        await client._handle_auth_event("permanent_failure")
        assert client._auth_state == AuthState.DEGRADED_VISUAL

    @pytest.mark.asyncio
    async def test_refresh_success_transitions_to_authenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.REFRESHING
        await client._handle_auth_event("refresh_success")
        assert client._auth_state == AuthState.AUTHENTICATED


class TestDegradedVisualState:
    """DEGRADED_VISUAL behavior for read vs write actions."""

    @pytest.mark.asyncio
    async def test_write_action_transitions_to_guided(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.DEGRADED_VISUAL
        await client._handle_auth_event("write_action")
        assert client._auth_state == AuthState.NEEDS_REAUTH_GUIDED

    @pytest.mark.asyncio
    async def test_api_probe_success_heals_to_authenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.DEGRADED_VISUAL
        await client._handle_auth_event("api_probe_success")
        assert client._auth_state == AuthState.AUTHENTICATED


class TestNeedsReauthGuidedState:

    @pytest.mark.asyncio
    async def test_token_healed_transitions_to_unauthenticated(self):
        client = _make_client()
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        client._auth_state = AuthState.NEEDS_REAUTH_GUIDED
        await client._handle_auth_event("token_healed")
        assert client._auth_state == AuthState.UNAUTHENTICATED


class TestFeatureFlag:

    def test_v2_disabled_falls_back_to_legacy(self):
        client = _make_client()
        client._v2_enabled = False
        assert not client._should_use_visual_fallback("fetch_unread_emails")
