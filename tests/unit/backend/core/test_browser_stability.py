from __future__ import annotations

import asyncio
from dataclasses import dataclass

from backend.core.browser_stability import BrowserStabilityConfig, StabilizedChromeLauncher


@dataclass
class _FakeProcess:
    pid: int = 4242
    returncode: int | None = None


async def test_launch_includes_automation_user_data_dir(monkeypatch):
    cfg = BrowserStabilityConfig()
    launcher = StabilizedChromeLauncher(cfg)
    launcher._chrome_binary = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    captured_cmd: list[str] = []

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        captured_cmd[:] = [str(part) for part in cmd]
        return _FakeProcess(returncode=None)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_cdp_ready(_port, timeout_s=2.0):
        return True

    monkeypatch.setenv("BROWSER_AUTOMATION_USER_DATA_DIR", "/tmp/jarvis-test-browser-profile-1")
    monkeypatch.setattr(launcher, "_find_available_cdp_port", lambda: 9222)
    monkeypatch.setattr(launcher, "_is_cdp_endpoint_ready", _fake_cdp_ready)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    ok = await launcher.launch_stabilized_chrome(url="http://localhost:3000", kill_existing=False)
    assert ok is True
    assert any(part.startswith("--user-data-dir=") for part in captured_cmd), captured_cmd


async def test_exit_zero_requires_cdp_ready(monkeypatch):
    cfg = BrowserStabilityConfig()
    launcher = StabilizedChromeLauncher(cfg)
    launcher._chrome_binary = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    async def _fake_create_subprocess_exec(*_cmd, **_kwargs):
        return _FakeProcess(returncode=0)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_any_chrome_running():
        return True

    async def _fake_cdp_ready(_port, timeout_s=2.0):
        return False

    monkeypatch.setenv("BROWSER_AUTOMATION_USER_DATA_DIR", "/tmp/jarvis-test-browser-profile-2")
    monkeypatch.setattr(launcher, "_find_available_cdp_port", lambda: 9222)
    monkeypatch.setattr(launcher, "_is_chrome_process_running", _fake_any_chrome_running)
    monkeypatch.setattr(launcher, "_is_cdp_endpoint_ready", _fake_cdp_ready)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    ok = await launcher.launch_stabilized_chrome(kill_existing=False)
    assert ok is False

