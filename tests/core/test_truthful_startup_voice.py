"""Startup lines are true when they are heard (2026-10-16 boot).

"Code evolution successful." was spoken at every boot with nothing evolved
(the Coding Council announced coming online through the evolution-COMPLETE
path, past the coordinator that had deferred it), and "Project Trinity
activation sequence beginning" was heard after Trinity had connected (queued
behind other speech, spoken once it was no longer true)."""
from __future__ import annotations

import asyncio
from pathlib import Path


def test_a_line_that_stopped_being_true_is_not_spoken(monkeypatch):
    from backend.core.supervisor import unified_voice_orchestrator as uvo

    orch = uvo.UnifiedVoiceOrchestrator()
    spoken = []

    async def speak_message(message):
        spoken.append(message.text)
    monkeypatch.setattr(orch, "_speak_message", speak_message)
    monkeypatch.setattr(orch.config, "min_interval_seconds", 0.0, raising=False)
    phase_done = {"trinity": False}

    async def main():
        orch._running = True
        worker = asyncio.ensure_future(orch._process_queue())
        await orch.speak("Trinity beginning.", source=uvo.VoiceSource.STARTUP,
                         still_true=lambda: not phase_done["trinity"])
        phase_done["trinity"] = True       # the phase finished while it waited
        await orch.speak("Trinity online.", source=uvo.VoiceSource.STARTUP)
        for _ in range(100):
            if orch._queue.qsize() == 0 and spoken:
                break
            await asyncio.sleep(0.02)
        orch._running = False
        await asyncio.wait_for(worker, timeout=5)

    phase_done_at_enqueue = phase_done.copy()
    asyncio.run(main())
    assert phase_done_at_enqueue == {"trinity": False}
    assert spoken == ["Trinity online."]


def test_a_truth_check_that_fails_keeps_the_line():
    from backend.core.supervisor import unified_voice_orchestrator as uvo

    def broken():
        raise RuntimeError("no readiness yet")
    message = uvo.VoiceMessage("x", uvo.VoicePriority.MEDIUM, uvo.VoiceSource.STARTUP,
                               still_true=broken)
    assert uvo.UnifiedVoiceOrchestrator._still_true(message) is True


def test_a_phase_line_is_true_until_its_boot_node_settles():
    from backend.core import progressive_readiness as pr
    from backend.core.supervisor.startup_narrator import _phase_running

    readiness = pr.get_readiness()
    check = _phase_running("trinity-test-node")
    assert check() is True
    readiness.mark_running("trinity-test-node")
    assert check() is True
    asyncio.run(readiness.mark_resolved("trinity-test-node"))
    assert check() is False


def test_coming_online_is_not_announced_as_an_evolution():
    src = Path("backend/core/coding_council/startup.py").read_text(encoding="utf-8")
    assert 'task_id="startup"' not in src
    assert "owns_startup_voice = bool(should_announce)" in src
    assert "CODING_COUNCIL_VOICE_ANNOUNCE and owns_startup_voice" in src
