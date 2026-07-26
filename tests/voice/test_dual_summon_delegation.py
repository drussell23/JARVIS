"""The Dual-Summon Collision — "Hey JARVIS, ask Karen to verify the deployment".

Three mandated assertions:

1. A transcript naming both agents assigns PRIMARY and SECONDARY correctly.
2. The TTS queue prevents the secondary's audio from playing until the
   primary's hardware lock is released.
3. (In ``tests/governance/fixtures/conftest.py``) the duplicate ``test_before``
   basename no longer aborts collection — asserted here as a live collection
   run, so the fix cannot rot silently.

The second assertion is the one with teeth, and it is written to FAIL on the
bug rather than on a proxy for it: the mocked audio records real start/finish
timestamps, so an overlap is detected as an overlap, not inferred from call
order. Two utterances that merely *arrived* in the right sequence would pass a
call-order assertion while still being audible at the same time.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.audio.speech_scheduler import (
    SpeechRole,
    SpeechScheduler,
    mark_hardware_busy,
    mark_hardware_idle,
    reset_scheduler,
)
from backend.core.ouroboros.governance.remote_compute_dispatcher import (
    ProviderRoute,
    RemoteComputeDispatcher,
    Tier,
    TierUnavailable,
    Workload,
    decide_lane,
)
from backend.voice.agent_persona import AgentPersona
from backend.voice.agent_registry import arbitrate


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    """A scheduler carried between tests would leak a busy count and make the
    next test wait out the idle timeout."""
    monkeypatch.setenv("JARVIS_TTS_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTS_ACOUSTIC_TAIL_S", "0.05")
    monkeypatch.setenv("JARVIS_TTS_HARDWARE_IDLE_TIMEOUT_S", "5")
    reset_scheduler()
    yield
    reset_scheduler()


# ---------------------------------------------------------------------------
# Assertion 1 — role assignment
# ---------------------------------------------------------------------------


def test_dual_summon_assigns_primary_and_secondary() -> None:
    s = arbitrate("Hey JARVIS, ask Karen to verify the deployment")
    assert s is not None
    assert s.primary.persona is AgentPersona.JARVIS
    assert s.secondary is not None
    assert s.secondary.persona is AgentPersona.OV
    assert s.delegated_task == "verify the deployment"
    assert s.is_dual
    assert s.reason == "delegation:ask"


def test_roles_follow_who_was_addressed_not_who_appears_second() -> None:
    s = arbitrate("Karen, tell JARVIS to reboot the mac")
    assert s.primary.persona is AgentPersona.OV
    assert s.secondary.persona is AgentPersona.JARVIS
    assert s.delegated_task == "reboot the mac"


@pytest.mark.parametrize(
    "utterance,reason",
    [
        # Named, but as a TOPIC — nobody was handed work.
        ("Karen, JARVIS is down again", "mention_not_delegation"),
        # Both addressed at once. One still has to speak first, but neither
        # is delegating to the other.
        ("Karen and JARVIS, status report", "coordination_not_delegation"),
    ],
)
def test_a_second_name_is_not_automatically_a_delegation(
    utterance: str, reason: str,
) -> None:
    """The failure this prevents: splitting on the second wake word would
    summon an agent to perform a sentence in which it was the subject."""
    s = arbitrate(utterance)
    assert s is not None
    assert s.secondary is None, f"{utterance!r} spuriously delegated"
    assert s.reason == reason


def test_the_primary_text_excludes_the_delegated_clause() -> None:
    """The primary is not being asked to verify anything."""
    s = arbitrate("Hey JARVIS, ask Karen to verify the deployment")
    assert "verify" not in s.primary_text
    assert "Karen" not in s.primary_text


# ---------------------------------------------------------------------------
# Assertion 2 — the turnstile
# ---------------------------------------------------------------------------


class _Speaker:
    """Mocked audio with real timing.

    Records (agent, start, end) so overlap is measured, not assumed, and
    marks the hardware busy exactly as ``playback_gate`` does in production —
    which is what the conductor actually waits on."""

    def __init__(self) -> None:
        self.spans: List[Tuple[str, float, float]] = []

    async def play(self, agent: str, duration: float = 0.05) -> None:
        mark_hardware_busy()
        start = time.monotonic()
        try:
            await asyncio.sleep(duration)
        finally:
            end = time.monotonic()
            mark_hardware_idle()
            self.spans.append((agent, start, end))

    def overlaps(self) -> List[Tuple[str, str]]:
        bad = []
        for i, (a, a0, a1) in enumerate(self.spans):
            for (b, b0, b1) in self.spans[i + 1:]:
                if a0 < b1 and b0 < a1:
                    bad.append((a, b))
        return bad

    def order(self) -> List[str]:
        return [a for a, _s, _e in sorted(self.spans, key=lambda s: s[1])]


@pytest.mark.asyncio
async def test_secondary_audio_waits_for_the_primary_lock() -> None:
    """Assertion 2. Both agents are released at the same instant; the
    secondary must still not be audible until the primary is done."""
    sched, speaker = SpeechScheduler(), _Speaker()

    async def utterance(agent: str, role: SpeechRole) -> None:
        await sched.speak(
            lambda: speaker.play(agent), agent=agent, role=role,
        )

    await asyncio.gather(
        utterance("jarvis", SpeechRole.PRIMARY),
        utterance("karen", SpeechRole.SECONDARY),
    )

    assert not speaker.overlaps(), (
        f"agents talked over each other: {speaker.overlaps()}"
    )
    assert speaker.order() == ["jarvis", "karen"]
    await sched.aclose()


@pytest.mark.asyncio
async def test_role_beats_arrival_order_among_waiting_tickets() -> None:
    """A plain lock grants in arrival order, and arrival order is a race: the
    secondary's work is usually shorter, so it often finishes synthesis first.
    Among tickets that are WAITING, order must follow arbitration instead.

    Deliberately not a stronger claim. Making a ticket that arrives at an idle
    turnstile pause in case a higher-priority one shows up would add latency
    to every utterance to serve a case the pipeline already prevents — the
    primary's reply is always enqueued before the delegated one is awaited.
    Asserting a guarantee the system does not make would be a test that
    passes by describing a system that does not exist."""
    sched, speaker = SpeechScheduler(), _Speaker()

    mark_hardware_busy()                       # turnstile occupied
    try:
        second = asyncio.create_task(sched.speak(
            lambda: speaker.play("karen"), agent="karen",
            role=SpeechRole.SECONDARY,
        ))
        await asyncio.sleep(0.05)              # SECONDARY queued FIRST
        first = asyncio.create_task(sched.speak(
            lambda: speaker.play("jarvis"), agent="jarvis",
            role=SpeechRole.PRIMARY,
        ))
        await asyncio.sleep(0.05)
        assert not speaker.spans, "someone spoke while the speakers were busy"
    finally:
        mark_hardware_idle()

    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

    assert speaker.order() == ["jarvis", "karen"], (
        "the delegated agent spoke before the agent that was addressed"
    )
    assert not speaker.overlaps()
    await sched.aclose()


@pytest.mark.asyncio
async def test_the_acoustic_tail_is_held_between_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """afplay exits when CoreAudio has the samples, not when the room is
    quiet. Granting at that instant clips the second agent onto the first."""
    monkeypatch.setenv("JARVIS_TTS_ACOUSTIC_TAIL_S", "0.20")
    sched, speaker = SpeechScheduler(), _Speaker()

    await sched.speak(lambda: speaker.play("jarvis", 0.02),
                      agent="jarvis", role=SpeechRole.PRIMARY)
    await sched.speak(lambda: speaker.play("karen", 0.02),
                      agent="karen", role=SpeechRole.SECONDARY)

    (_a, _a0, a_end), (_b, b_start, _b1) = speaker.spans[0], speaker.spans[1]
    assert b_start - a_end >= 0.18, (
        f"only {b_start - a_end:.3f}s of silence between agents"
    )
    await sched.aclose()


@pytest.mark.asyncio
async def test_an_unscheduled_utterance_still_blocks_the_queue() -> None:
    """Several playback sites predate the scheduler. The conductor waits on
    the REAL device — the same busy signal playback_gate publishes — so a
    legacy announcement is yielded to rather than talked over."""
    sched, speaker = SpeechScheduler(), _Speaker()

    mark_hardware_busy()                       # a legacy site starts playing
    ticket = asyncio.create_task(sched.speak(
        lambda: speaker.play("karen"), agent="karen", role=SpeechRole.PRIMARY,
    ))
    await asyncio.sleep(0.1)
    assert not speaker.spans, "the queue spoke over an unscheduled utterance"

    mark_hardware_idle()                       # legacy site finishes
    await asyncio.wait_for(ticket, timeout=5)
    assert speaker.order() == ["karen"]
    await sched.aclose()


@pytest.mark.asyncio
async def test_a_wedged_utterance_does_not_mute_the_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail OPEN. Holding the turnstile for a caller that never returns would
    silence every later utterance — worse than the overlap being prevented."""
    monkeypatch.setenv("JARVIS_TTS_TICKET_TIMEOUT_S", "1")
    sched, speaker = SpeechScheduler(), _Speaker()

    async def _wedged() -> None:
        await asyncio.sleep(30)

    stuck = asyncio.create_task(sched.speak(
        _wedged, agent="wedged", role=SpeechRole.PRIMARY,
    ))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(
        sched.speak(lambda: speaker.play("karen"),
                    agent="karen", role=SpeechRole.SECONDARY),
        timeout=5,
    )
    assert speaker.order() == ["karen"]
    stuck.cancel()
    await sched.aclose()


@pytest.mark.asyncio
async def test_barge_in_drops_queued_but_not_playing() -> None:
    """Cancelling the utterance currently on the speakers from here would
    leave the turnstile held by a dead ticket; that one is cancelled through
    its own cancel_event."""
    sched, speaker = _fresh_sched(), _Speaker()

    playing = asyncio.create_task(sched.speak(
        lambda: speaker.play("jarvis", 0.3), agent="jarvis",
        role=SpeechRole.PRIMARY,
    ))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(sched.speak(
        lambda: speaker.play("karen"), agent="karen",
        role=SpeechRole.SECONDARY,
    ))
    await asyncio.sleep(0.02)

    assert sched.cancel_pending() >= 1
    assert await queued is False, "a cancelled ticket reported success"
    assert await playing is True, "barge-in killed the utterance already playing"
    assert speaker.order() == ["jarvis"]
    await sched.aclose()


def _fresh_sched() -> SpeechScheduler:
    return SpeechScheduler()


@pytest.mark.asyncio
async def test_disabled_scheduler_is_a_true_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TTS_SCHEDULER_ENABLED", "false")
    sched, speaker = SpeechScheduler(), _Speaker()
    await sched.speak(lambda: speaker.play("karen"), agent="karen")
    assert speaker.order() == ["karen"]
    assert sched.pending() == 0


# ---------------------------------------------------------------------------
# Lane arbitration — the secondary must not take the primary's provider lane
# ---------------------------------------------------------------------------


def test_delegated_work_never_takes_the_immediate_lane() -> None:
    """IMMEDIATE is Claude-direct because a human is waiting in real time. A
    delegated turn taking it makes the ADDRESSED agent wait behind work the
    operator did not ask it for."""
    decision = decide_lane(Workload(agent="ov", task="verify the deployment"))
    assert decision.route is ProviderRoute.STANDARD
    assert not decision.shares_primary_lane
    assert decision.tier is Tier.DOUBLEWORD


def test_an_immediate_stamp_is_demoted() -> None:
    forced = decide_lane(Workload(
        agent="ov", task="check logs", route=ProviderRoute.IMMEDIATE,
    ))
    assert forced.route is ProviderRoute.STANDARD
    assert forced.reason == "demoted_from_primary_lane"


def test_heavy_work_plans_before_it_executes() -> None:
    decision = decide_lane(Workload(agent="ov", task="refactor the audio plane"))
    assert decision.route is ProviderRoute.COMPLEX
    assert decision.tier is Tier.CLAUDE


def test_work_touching_this_machine_goes_sovereign() -> None:
    """A remote tier cannot see the screen or the working tree."""
    for task in ("read my screen", "check the uncommitted worktree"):
        assert decide_lane(Workload(agent="ov", task=task)).tier is Tier.JPRIME


@pytest.mark.asyncio
async def test_dispatch_falls_back_through_the_chain_to_the_caller() -> None:
    """No tier wired is the DEFAULT state. It must degrade to the caller's own
    path, not to an error — remote compute is an optimisation."""
    dispatcher = RemoteComputeDispatcher()
    ran = []

    async def _caller() -> str:
        ran.append("caller")
        return "done locally"

    result = await dispatcher.dispatch(
        Workload(agent="ov", task="verify the deployment"), _caller,
    )
    assert result.value == "done locally"
    assert result.tier is Tier.CALLER
    assert result.fell_back is True
    assert ran == ["caller"]


@pytest.mark.asyncio
async def test_a_wired_tier_is_used_and_a_broken_one_is_skipped() -> None:
    async def _dead(_w: Workload) -> str:
        raise TierUnavailable("spot instance preempted")

    async def _alive(_w: Workload) -> str:
        return "verified"

    dispatcher = RemoteComputeDispatcher({
        Tier.DOUBLEWORD: _dead, Tier.CLAUDE: _alive,
    })

    async def _caller() -> str:
        raise AssertionError("the caller path ran while a tier was healthy")

    result = await dispatcher.dispatch(
        Workload(agent="ov", task="verify the deployment"), _caller,
    )
    assert result.value == "verified"
    assert result.tier is Tier.CLAUDE
    assert result.fell_back is True


@pytest.mark.asyncio
async def test_spawn_does_not_block_the_primary() -> None:
    """The whole reason the secondary is a task: the primary speaks first."""
    dispatcher = RemoteComputeDispatcher()
    started = time.monotonic()

    async def _slow() -> str:
        await asyncio.sleep(0.3)
        return "late"

    task = dispatcher.spawn(Workload(agent="ov", task="audit the logs"), _slow)
    assert time.monotonic() - started < 0.05, "spawn blocked the caller"
    result = await task
    assert result.value == "late"


@pytest.mark.asyncio
async def test_a_failing_delegated_turn_is_a_result_not_an_exception() -> None:
    dispatcher = RemoteComputeDispatcher()

    async def _boom() -> str:
        raise RuntimeError("provider exploded")

    result = await dispatcher.dispatch(Workload(agent="ov", task="check"), _boom)
    assert result.value is None
    assert "provider exploded" in result.error


# ---------------------------------------------------------------------------
# Assertion 3 — the collection bug stays fixed
# ---------------------------------------------------------------------------


def test_the_l2_corpus_is_data_and_is_not_collected() -> None:
    """The four ``problem_NNN/test_before.py`` files are the deliberately
    failing tests of the L2 repair corpus — DATA, not tests.

    Collecting them aborted ``pytest tests/governance`` outright (four modules
    resolving to the name ``test_before``) and, had the basenames merely been
    renamed, would have injected four known-red tests into every run, making
    "the corpus is red" indistinguishable from "the organism regressed".

    Scoped to the fixtures directory rather than all of tests/governance: that
    is where the collision lives, so it reproduces the bug in about a second
    instead of spending a minute collecting 41k tests to prove the same
    thing."""
    corpus = Path("tests/governance/fixtures/l2_exercise_corpus")
    data_files = sorted(corpus.glob("problem_*/test_before.py"))
    assert len(data_files) >= 2, (
        "the collision needs at least two same-named data files to exist; "
        "this test would pass vacuously without them"
    )

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/governance/fixtures",
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "import file mismatch" not in combined, combined[-1500:]
    assert "error" not in combined.lower() or "no tests ran" in combined.lower(), (
        combined[-1500:]
    )
    # And nothing under it was collected — it is data.
    assert "test_before" not in proc.stdout, proc.stdout[-1500:]
