"""Tests for loading page redirect URL resolution."""
import asyncio
import os
import sys
import importlib
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp


# ---------------------------------------------------------------------------
# Import isolation: loading_server has module-level SQLite init that writes
# to ~/.jarvis/loading_server/progress.db.  Patch ProgressPersistence.__init__
# before the module executes so the import doesn't touch the filesystem.
# ---------------------------------------------------------------------------

def _import_loading_server():
    """Import loading_server with module-level side-effects suppressed."""
    # Patch sqlite3.connect so ProgressPersistence._init_db is a no-op
    with patch("sqlite3.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        # Also patch Path.mkdir so the parent-dir creation doesn't fail
        with patch("pathlib.Path.mkdir"):
            # Remove cached module if it was imported previously without the patch
            sys.modules.pop("loading_server", None)
            mod = importlib.import_module("loading_server")

    return mod


# Run import once at collection time
_ls = _import_loading_server()
_resolve_redirect_url = _ls._resolve_redirect_url


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolveRedirectUrl:
    @pytest.mark.asyncio
    async def test_jarvis_frontend_url_wins(self):
        """JARVIS_FRONTEND_URL takes precedence over everything."""
        os.environ["JARVIS_FRONTEND_URL"] = "http://custom:4000"
        try:
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert url == "http://custom:4000"
        finally:
            os.environ.pop("JARVIS_FRONTEND_URL", None)

    @pytest.mark.asyncio
    async def test_legacy_frontend_url_fallback(self):
        """FRONTEND_URL is checked after JARVIS_FRONTEND_URL."""
        os.environ.pop("JARVIS_FRONTEND_URL", None)
        os.environ["FRONTEND_URL"] = "http://legacy:5000"
        try:
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert url == "http://legacy:5000"
        finally:
            os.environ.pop("FRONTEND_URL", None)

    @pytest.mark.asyncio
    async def test_fallback_to_api_when_no_frontend(self):
        """When no frontend responds, return API-only URL."""
        os.environ.pop("JARVIS_FRONTEND_URL", None)
        os.environ.pop("FRONTEND_URL", None)
        os.environ.pop("JARVIS_FRONTEND_PROBE_URLS", None)

        # Mock aiohttp.ClientSession so no real network calls are made
        with patch.object(_ls.aiohttp, "ClientSession") as mock_cls:
            mock_session = MagicMock()
            # Simulate connection failure on every probe
            mock_get_ctx = MagicMock()
            mock_get_ctx.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("connection refused")
            )
            mock_get_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_get_ctx)

            mock_cls_ctx = MagicMock()
            mock_cls_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cls_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_cls_ctx

            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert "8010" in url

    @pytest.mark.asyncio
    async def test_jarvis_frontend_url_precedence_over_legacy(self):
        """JARVIS_FRONTEND_URL wins over FRONTEND_URL."""
        os.environ["JARVIS_FRONTEND_URL"] = "http://new:4000"
        os.environ["FRONTEND_URL"] = "http://old:5000"
        try:
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert url == "http://new:4000"
        finally:
            os.environ.pop("JARVIS_FRONTEND_URL", None)
            os.environ.pop("FRONTEND_URL", None)
