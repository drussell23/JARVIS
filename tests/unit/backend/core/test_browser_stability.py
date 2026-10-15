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


# -- the automation profile is single-holder (Chromium's process singleton) --
# Live evidence (supervisor-20261015-085516.log, 08:56:57): an automation
# Chrome from an earlier boot held the profile on CDP 9222; the launcher picked
# 9223, launched a second Chrome on the same profile, which handed off and
# exited 21 -- logged as "Chrome crashed immediately with code 21".


def _launcher(monkeypatch, profile: str) -> StabilizedChromeLauncher:
    launcher = StabilizedChromeLauncher(BrowserStabilityConfig())
    launcher._chrome_binary = "chrome"
    monkeypatch.setenv("BROWSER_AUTOMATION_USER_DATA_DIR", profile)
    monkeypatch.setattr(launcher, "_find_available_cdp_port", lambda: 9223)

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return launcher


async def test_running_profile_holder_with_cdp_is_adopted_not_relaunched(monkeypatch, tmp_path):
    launcher = _launcher(monkeypatch, str(tmp_path / "p"))
    monkeypatch.setattr(launcher, "_profile_holder_sync", lambda d: (29468, 9222))

    async def _cdp_ready(port, timeout_s=2.0):
        return port == 9222

    async def _no_spawn(*_a, **_k):
        raise AssertionError("must not launch a second Chrome on a held profile")

    monkeypatch.setattr(launcher, "_is_cdp_endpoint_ready", _cdp_ready)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    assert await launcher.launch_stabilized_chrome(kill_existing=False) is True
    assert launcher.get_cdp_port() == 9222
    assert launcher._adopted_pid == 29468


async def test_holder_without_cdp_is_replaced_then_launched(monkeypatch, tmp_path):
    launcher = _launcher(monkeypatch, str(tmp_path / "p"))
    holders = [(777, None)]
    stopped: list[int] = []
    monkeypatch.setattr(launcher, "_profile_holder_sync", lambda d: holders[0] if holders else None)
    monkeypatch.setattr(launcher, "_terminate_pid_sync", lambda pid, g: stopped.append(pid) or True)

    async def _cdp_ready(_port, timeout_s=2.0):
        return True

    async def _spawn(*_a, **_k):
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(launcher, "_is_cdp_endpoint_ready", _cdp_ready)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    assert await launcher.launch_stabilized_chrome(kill_existing=False) is True
    assert stopped == [777]
    assert launcher.get_cdp_port() == 9223


async def test_exit_21_is_a_handoff_not_a_crash(monkeypatch, tmp_path, caplog):
    launcher = _launcher(monkeypatch, str(tmp_path / "p"))
    calls = {"n": 0}

    def _holder(_d):
        # Absent at the pre-launch check, present when the launch hands off.
        calls["n"] += 1
        return None if calls["n"] == 1 else (555, 9222)

    async def _cdp_ready(port, timeout_s=2.0):
        return port == 9222

    async def _spawn(*_a, **_k):
        return _FakeProcess(returncode=21)

    monkeypatch.setattr(launcher, "_profile_holder_sync", _holder)
    monkeypatch.setattr(launcher, "_is_cdp_endpoint_ready", _cdp_ready)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    with caplog.at_level("ERROR"):
        assert await launcher.launch_stabilized_chrome(kill_existing=False) is True
    assert launcher.get_cdp_port() == 9222
    assert not [r for r in caplog.records if "crashed" in r.getMessage()]


def test_profile_holder_judged_structurally(tmp_path, monkeypatch):
    import psutil

    prof = tmp_path / "automation-profile"
    other = tmp_path / "other"

    class _P:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline}

    procs = [
        _P(1, ["chrome.exe", "--restore-last-session"]),                        # user Chrome
        _P(2, ["chrome.exe", "--type=renderer", f"--user-data-dir={prof}"]),    # child process
        _P(3, ["chrome.exe", f"--user-data-dir={other}", "--remote-debugging-port=9300"]),
        _P(4, ["chrome.exe", "--remote-debugging-port=9222", f"--user-data-dir={prof}"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    launcher = StabilizedChromeLauncher(BrowserStabilityConfig())
    assert launcher._profile_holder_sync(str(prof)) == (4, 9222)
