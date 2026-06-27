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


def test_death_rattle_writes_allocation_free_dump(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._open_autopsy_fd()
    assert getattr(h, "_autopsy_fd", None) is not None
    h._fire_death_rattle()
    body = (tmp_path / "pre_oom_autopsy.log").read_text()
    assert "PRE-OOM DEATH RATTLE" in body
    assert "END DEATH RATTLE" in body
    assert ("File" in body or "Thread" in body)  # faulthandler stack present


def test_death_rattle_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", raising=False)
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._open_autopsy_fd()           # still pre-opens (cheap, boot-time)
    h._fire_death_rattle()         # but writes nothing when off
    p = tmp_path / "pre_oom_autopsy.log"
    assert (not p.exists()) or p.read_text() == ""


def test_cap_fire_dumps_before_oracle_checkpoint(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._stop_reason = "unknown"
    h._started_at = 0.0
    h._process_memory_event = asyncio.Event()
    order = []
    h._open_autopsy_fd()
    orig = h._fire_death_rattle
    h._fire_death_rattle = lambda: (order.append("rattle"), orig())[1]

    async def fake_ckpt():
        order.append("oracle")

    h._checkpoint_oracle_best_effort = fake_ckpt
    asyncio.run(h._fire_process_memory_cap(99999.0, 1.0))
    assert order[0] == "rattle"            # rattle BEFORE oracle checkpoint
    assert "oracle" in order


def test_redline_trips_on_critical_pressure_not_just_free_pct(monkeypatch, tmp_path):
    """FIX 3: Redline fires when gate.pressure() == CRITICAL even with high free_pct."""
    import asyncio
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "true")

    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._stop_reason = "unknown"
    h._started_at = 0.0
    h._process_memory_event = asyncio.Event()
    h._open_autopsy_fd()

    # Stub gate: pressure() = CRITICAL, probe returns healthy free_pct=60
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: mpg.PressureLevel.CRITICAL)
    monkeypatch.setattr(mpg.MemoryPressureGate, "probe",
                        lambda self: mpg.MemoryProbe(
                            free_pct=60.0, total_bytes=1, available_bytes=1,
                            source="test"))

    cap_fired = []

    async def fake_fire(rss_mb, cap_mb):
        cap_fired.append((rss_mb, cap_mb))

    h._fire_process_memory_cap = fake_fire
    h._probe_process_tree_rss_mb = lambda: 100.0

    # fake_sleep returns normally — lets the loop body run (where the
    # redline check lives). The monitor then calls fake_fire + returns
    # naturally without needing CancelledError.
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    # Disable adaptive polling so first sleep is the fixed interval
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", raising=False)

    async def _drive():
        await h._monitor_process_memory(warn_mb=1e9, cap_mb=1e9, interval_s=0.001)

    asyncio.run(_drive())
    assert cap_fired, "cap fire not invoked on CRITICAL pressure with high free_pct"
    assert h._stop_reason == "resource_governor_redline"


def test_redline_trips_on_disk_critical_with_disk_label(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "1")
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    # gate reports CRITICAL and a low disk free%; RAM probe is healthy
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: mpg.PressureLevel.CRITICAL)
    monkeypatch.setattr(mpg.MemoryPressureGate, "probe",
                        lambda self: mpg.MemoryProbe(free_pct=60.0, total_bytes=1,
                                                     available_bytes=1, source="test"))
    monkeypatch.setattr(mpg.MemoryPressureGate, "_disk_dim",
                        lambda self: (mpg.PressureLevel.CRITICAL, 3.0, None))
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._stop_reason = "unknown"
    h._started_at = 0.0
    h._process_memory_event = asyncio.Event()
    h._open_autopsy_fd()
    fired = {"cap": False}

    async def fake_cap(rss, cap):
        fired["cap"] = True
    h._fire_process_memory_cap = fake_cap

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    h._probe_process_tree_rss_mb = lambda: 1.0
    try:
        asyncio.run(h._monitor_process_memory(1e9, 1e9, 15.0))
    except asyncio.CancelledError:
        pass
    assert fired["cap"] is True
    assert h._stop_reason == "resource_governor_disk_redline"
