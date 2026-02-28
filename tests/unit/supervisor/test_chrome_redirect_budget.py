from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest


def _make_manager(us):
    mgr = us.IntelligentChromeIncognitoManager.__new__(us.IntelligentChromeIncognitoManager)
    mgr._logger = MagicMock()
    mgr._lock = asyncio.Lock()
    mgr._operation_count = 0
    mgr._error_count = 0
    mgr._last_operation_time = datetime.now()
    return mgr


@pytest.mark.asyncio
async def test_run_osascript_timeout_kills_child(monkeypatch):
    import unified_supervisor as us

    mgr = _make_manager(us)

    class _FakeProcess:
        def __init__(self):
            self.returncode = None
            self.killed = False
            self.waited = False

        async def communicate(self):
            await asyncio.sleep(1.0)
            return b"", b""

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9
            return self.returncode

    fake_proc = _FakeProcess()

    async def _fake_create(*_args, **_kwargs):
        return fake_proc

    monkeypatch.setattr(us.asyncio, "create_subprocess_exec", _fake_create)

    ok, out, err = await mgr._run_osascript(
        'return "ok"',
        timeout=0.01,
        op_name="unit_timeout",
    )

    assert ok is False
    assert out == ""
    assert "unit_timeout:timeout" in err
    assert fake_proc.killed is True
    assert fake_proc.waited is True


@pytest.mark.asyncio
async def test_redirect_existing_incognito_with_budget_reports_no_window(monkeypatch):
    import unified_supervisor as us

    mgr = _make_manager(us)

    async def _no_window(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mgr, "_quick_find_any_incognito_window", _no_window)

    result = await mgr.redirect_existing_incognito_with_budget(
        "http://localhost:3000",
        budget_seconds=1.0,
    )

    assert result["success"] is False
    assert result["action"] == "skipped"
    assert result["error"] == "no_existing_incognito_window"
