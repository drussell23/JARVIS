"""What JARVIS says while it boots is true when it says it.

Boot of 2026-10-14 08:11, as spoken: "Cloud intelligence warming up" and
"Cloud intelligence fully synchronized" (no cloud lane), "All systems fully
operational" and "JARVIS is ready" at about half way, "A.G.I. operating
system online" as it started, three greetings, and "All systems
operational... Boot took 110 seconds" while the backend reported degraded
and the boot had taken ~129 s.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from backend.core import startup_announcement_coordinator as sac
from backend.core.supervisor import lifecycle_narrator as ln

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_coordinator():
    asyncio.run(sac.get_startup_coordinator().reset())
    yield
    asyncio.run(sac.get_startup_coordinator().reset())


# -- one startup announcement per process -----------------------------------------
def test_a_supervisor_boot_silences_every_other_announcer():
    async def scenario():
        assert await sac.should_announce_startup("unified_supervisor", sac.AnnouncementPriority.CRITICAL)
        return [await sac.may_announce(name, prio) for name, prio in (
            ("agi_os_coordinator", sac.AnnouncementPriority.NORMAL),
            ("intelligent_startup_announcer", sac.AnnouncementPriority.NORMAL),
            ("ghost_hands", sac.AnnouncementPriority.NORMAL),
            ("jarvis_voice_api", sac.AnnouncementPriority.HIGH),
            ("anything", sac.AnnouncementPriority.CRITICAL),   # equal priority cannot override
        )]
    assert asyncio.run(scenario()) == [False] * 5


def test_without_a_supervisor_the_first_announcer_speaks():
    async def scenario():
        return await sac.may_announce("agi_os_coordinator"), await sac.may_announce("ghost_hands")
    assert asyncio.run(scenario()) == (True, False)


def test_both_import_names_are_one_coordinator(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "backend"))
    sys.modules.pop("core.startup_announcement_coordinator", None)
    import core.startup_announcement_coordinator as bare  # noqa: E402
    assert bare is sac and bare.get_startup_coordinator() is sac.get_startup_coordinator()


# -- the narrator says what was measured -------------------------------------------
def _narrator():
    n = ln.LifecycleVoiceNarrator.__new__(ln.LifecycleVoiceNarrator)
    n._startup_complete_narrated = False
    n._owner = "Derek"
    n.spoken = []
    n.enqueue = lambda text, *_a, **_k: n.spoken.append(text)
    return n


def test_all_systems_only_when_every_component_measured_healthy():
    n = _narrator()
    n.narrate_startup_complete(124.6, [])
    assert "Boot took 125 seconds" in n.spoken[0]
    assert any(p.format(name="Derek") in n.spoken[0] for p in ln._STARTUP_COMPLETE)


def test_problems_are_named_not_hidden():
    n = _narrator()
    n.narrate_startup_complete(124.6, ["JARVIS is degraded", "Reactor-Core is offline"])
    said = n.spoken[0]
    assert "JARVIS is degraded and Reactor-Core is offline" in said
    assert "operational" not in said and "fully online" not in said


def test_no_measurement_no_claim_about_all_systems():
    n = _narrator()
    n.narrate_startup_complete(124.6, None)
    assert "all systems" not in n.spoken[0].lower()


def test_zone_lines_only_say_a_zone_is_starting():
    claims = ("online", "ready", "awake", "engaged", "operational")
    for zone, lines in ln._BOOT_ZONE_MESSAGES.items():
        for line in lines:
            assert not any(word in line.lower() for word in claims), (zone, line)


# -- tier narration ----------------------------------------------------------------
def _kernel(readings, outcomes):
    import unified_supervisor as us
    k = types.SimpleNamespace(logger=types.SimpleNamespace(debug=lambda *_: None))

    async def measure(self):
        return readings
    k._measure_component_health = types.MethodType(measure, k)
    k._spoken_component = us.JarvisSystemKernel._spoken_component
    k._health_issues = types.MethodType(us.JarvisSystemKernel._health_issues, k)
    describe = types.MethodType(us.JarvisSystemKernel._describe_tier_change, k)
    readiness = types.SimpleNamespace(tier_outcomes=lambda _tier: outcomes)
    from backend.core.progressive_readiness import ReadinessTier
    return lambda tier: asyncio.run(describe(readiness, tier, ReadinessTier)), ReadinessTier


def _reading(name, display, status):
    from backend.core.supervisor.elite_dashboard import RepoHealth
    return RepoHealth(name, display, status)


def test_tiers_say_nothing_about_a_cloud_and_report_what_was_measured():
    say, T = _kernel([_reading("jarvis", "JARVIS (Body)", "DEGRADED"),
                      _reading("jprime", "J-Prime (Mind)", "ONLINE"),
                      _reading("reactor", "Reactor-Core (Nerves)", "OFFLINE")],
                     {"backend": "resolved", "trinity": "resolved"})
    assert "cloud" not in say(T.ACTIVE_LOCAL).lower()
    assert say(T.ACTIVE_FULL) == "J-Prime is connected; Reactor-Core is offline."
    assert say(T.FULLY_OPERATIONAL) is None          # completion's to announce


def test_a_failed_node_is_said_not_glossed():
    say, T = _kernel([], {"backend": "failed", "clean_slate": "resolved"})
    assert "backend failed" in say(T.ACTIVE_LOCAL)
