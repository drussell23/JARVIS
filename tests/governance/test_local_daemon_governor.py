# tests/governance/test_local_daemon_governor.py
from __future__ import annotations
import pytest


def test_governor_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED", raising=False)
    from backend.core.ouroboros.governance.local_daemon_governor import daemon_governor_enabled
    assert daemon_governor_enabled() is False
    monkeypatch.setenv("JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED", "true")
    assert daemon_governor_enabled() is True


def _mk(monkeypatch, *, enabled=True, local_on=True, healthy_seq, runner_rc=0):
    """Build a governor with injected health sequence + recording runner."""
    monkeypatch.setenv("JARVIS_LOCAL_DAEMON_GOVERNOR_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true" if local_on else "false")
    from backend.core.ouroboros.governance.local_daemon_governor import LocalDaemonGovernor
    calls = {"runner": [], "flush": 0}
    seq = list(healthy_seq)

    async def _health():
        return seq.pop(0) if seq else (seq[-1] if seq else False)

    async def _flush():
        calls["flush"] += 1

    def _runner(cmd):
        calls["runner"].append(list(cmd))
        return runner_rc

    gov = LocalDaemonGovernor(health_probe=_health, flush=_flush, runner=_runner,
                              start_timeout_s=0.5, poll_interval_s=0.01)
    return gov, calls


@pytest.mark.asyncio
async def test_start_noop_when_disabled(monkeypatch):
    gov, calls = _mk(monkeypatch, enabled=False, healthy_seq=[False])
    started = await gov.start_if_enabled()
    assert started is False
    assert calls["runner"] == []          # no host mutation when OFF


@pytest.mark.asyncio
async def test_start_when_already_healthy_does_not_start_or_own(monkeypatch):
    gov, calls = _mk(monkeypatch, healthy_seq=[True])
    started = await gov.start_if_enabled()
    assert started is True
    assert calls["runner"] == []          # already up -> we did NOT start it
    assert gov.owns_daemon() is False     # not owned -> we won't stop it later


@pytest.mark.asyncio
async def test_jit_start_boots_and_owns_when_down(monkeypatch):
    # health: down on first probe, healthy after the brew start + one poll
    gov, calls = _mk(monkeypatch, healthy_seq=[False, True])
    started = await gov.start_if_enabled()
    assert started is True
    assert any("start" in c and "ollama" in c for c in calls["runner"])  # brew services start ollama
    assert gov.owns_daemon() is True       # we booted it -> we own it


@pytest.mark.asyncio
async def test_stop_flushes_and_stops_only_when_owned(monkeypatch):
    gov, calls = _mk(monkeypatch, healthy_seq=[False, True])
    await gov.start_if_enabled()           # owns it now
    calls["runner"].clear()
    await gov.stop_if_idle()
    assert calls["flush"] >= 1             # weights flushed (keep_alive:0)
    assert any("stop" in c and "ollama" in c for c in calls["runner"])  # owned -> stopped


@pytest.mark.asyncio
async def test_stop_does_not_kill_operator_started_daemon(monkeypatch):
    gov, calls = _mk(monkeypatch, healthy_seq=[True])
    await gov.start_if_enabled()           # already healthy -> NOT owned
    calls["runner"].clear()
    await gov.stop_if_idle()
    assert calls["flush"] >= 1             # still flushes weights (frees RAM)
    assert calls["runner"] == []          # NOT owned -> never stops the operator's process


@pytest.mark.asyncio
async def test_stop_noop_when_disabled(monkeypatch):
    gov, calls = _mk(monkeypatch, enabled=False, healthy_seq=[True])
    await gov.stop_if_idle()
    assert calls["runner"] == [] and calls["flush"] == 0
