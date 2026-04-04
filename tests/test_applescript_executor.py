"""Tests for AppleScriptExecutor -- deterministic macOS actions."""
import pytest
from unittest.mock import patch, MagicMock

from backend.hud.applescript_executor import AppleScriptExecutor


@pytest.fixture
def executor():
    return AppleScriptExecutor()


class TestAppDiscovery:
    def test_discovers_exact_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app", "Google Chrome.app"]):
            result = executor.discover_app("Safari")
        assert result == "Safari"

    def test_discovers_fuzzy_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app", "Google Chrome.app"]):
            result = executor.discover_app("chrome")
        assert result == "Google Chrome"

    def test_returns_original_on_no_match(self, executor):
        with patch("os.listdir", return_value=["Safari.app"]):
            result = executor.discover_app("NonExistentApp")
        assert result == "NonExistentApp"


class TestURLInference:
    def test_full_url_passthrough(self, executor):
        assert executor.infer_url("https://linkedin.com") == "https://linkedin.com"

    def test_known_site_inference(self, executor):
        url = executor.infer_url("LinkedIn")
        assert "linkedin.com" in url

    def test_search_query(self, executor):
        url = executor.infer_url("search Google for AI engineers")
        assert "google.com/search" in url
        assert "ai" in url.lower()


class TestExecution:
    @pytest.mark.asyncio
    async def test_open_app_calls_subprocess(self, executor):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await executor.open_app("Safari")
        assert result.success is True
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_url_calls_subprocess(self, executor):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await executor.open_url("https://linkedin.com")
        assert result.success is True
