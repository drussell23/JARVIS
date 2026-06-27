from __future__ import annotations
import backend.core.ouroboros.battle_test.harness as H


def test_adaptive_interval_inversely_proportional(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "true")
    assert H.rg_poll_interval_for("ok")       == 10.0
    assert H.rg_poll_interval_for("warn")     == 3.0
    assert H.rg_poll_interval_for("high")     == 0.5
    assert H.rg_poll_interval_for("critical") == 0.2
    # unknown level -> OK interval
    assert H.rg_poll_interval_for("???")      == 10.0


def test_backstop_interval_has_fixed_floor(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_BACKSTOP_INTERVAL_S", raising=False)
    assert H.rg_backstop_interval_s() == 1.0


def test_monitor_uses_adaptive_interval(monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    # Stub the level source + capture the sleeps requested.
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: mpg.PressureLevel.HIGH)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise asyncio.CancelledError  # exit after one iteration

    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    h._probe_process_tree_rss_mb = lambda: 1.0
    asyncio.run(_drive_once(h))
    assert sleeps and sleeps[0] == 0.5   # HIGH -> 0.5s


async def _drive_once(h):
    import asyncio
    try:
        await h._monitor_process_memory(warn_mb=1e9, cap_mb=1e9, interval_s=15.0)
    except asyncio.CancelledError:
        pass
