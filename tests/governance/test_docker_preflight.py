from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock


@pytest.mark.asyncio
async def test_preflight_reports_daemon_state():
    assert await lock.docker_preflight(probe=lambda: True) is True
    assert await lock.docker_preflight(probe=lambda: False) is False


@pytest.mark.asyncio
async def test_preflight_warns_when_gate_armed_and_absent(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(lock, "lock_enabled", lambda: True)
    with caplog.at_level(logging.WARNING):
        result = await lock.docker_preflight(probe=lambda: False)
    assert result is False
    assert any(
        "Docker daemon ABSENT" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_preflight_no_warn_when_gate_off(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(lock, "lock_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        result = await lock.docker_preflight(probe=lambda: False)
    assert result is False
    assert not any("Docker daemon ABSENT" in r.getMessage() for r in caplog.records)
