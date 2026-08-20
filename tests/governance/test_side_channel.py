"""Regression spine for the `/btw` side-question lane.

The lane exists because a question and a command shared one serialized
consumer: ``/ask`` awaited a provider on the body of
``operator_input_queue._drain``, so the operator's next keystroke queued
behind the answer to their last one. Every property below is one of the
three that, together, make an aside an aside — remove any one and the
feature degenerates into ``/ask`` or into ``/goal``:

  1. submission is INSTANT and never awaits a provider;
  2. answering is NON-PREEMPTIVE — it yields to the organism's load;
  3. deferral is BOUNDED — courtesy, never starvation.

Plus the things that make it survivable: refusal instead of dropping,
addressed delivery, cancellation that a late provider result cannot
undo, and an unanswered question that is reported rather than vanishing
with the process.

Hermetic throughout: the answering substrate is injected, so no test
here touches a network, a key, or a budget.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import side_channel as sc


# ---------------------------------------------------------------------------
# Fakes — shaped like the real substrate's returns, never like its bugs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeArtifact:
    ref: str = "q-1"
    answer: str = "Because the route was BACKGROUND."
    cost_usd: float = 0.0012
    model: str = "fake-model"


@dataclass(frozen=True)
class _FakeVerdict:
    value: str


@dataclass(frozen=True)
class _FakeReport:
    verdict: Any
    artifact: Optional[_FakeArtifact]
    diagnostic: str = ""


class _RecordingAsker:
    """Stands in for ``fast_path_qa.ask_question``."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        report: Optional[_FakeReport] = None,
        accepts_grounding: bool = True,
    ) -> None:
        self.calls: List[dict] = []
        self.delay = delay
        self.accepts_grounding = accepts_grounding
        self.report = report or _FakeReport(
            verdict=_FakeVerdict("answered"), artifact=_FakeArtifact(),
        )
        self.started = asyncio.Event()

    async def __call__(self, question, *, op_id="", grounding=None, **kw):
        if grounding is not None and not self.accepts_grounding:
            raise TypeError(
                "ask_question() got an unexpected keyword argument "
                "'grounding'"
            )
        self.calls.append({
            "question": question, "op_id": op_id, "grounding": grounding,
        })
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.report


class _Sink:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.sessions: List[Optional[str]] = []

    def __call__(self, markup: str) -> None:
        from backend.core.ouroboros.battle_test.attach_session import (
            current_session,
        )
        self.lines.append(markup)
        self.sessions.append(current_session())

    @property
    def joined(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test gets a fresh singleton, no readers, no live signals.

    The default situation readers reach into the live cockpit
    substrates; leaving them installed would make these tests depend on
    whatever the process happens to have booted.
    """
    sc.reset_default_side_channel_for_tests()
    for name in sc.situation_reader_names():
        sc.unregister_situation_reader(name)
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 0)
    sc._PRESSURE.clear()

    # The pressure dimension is pinned to the CACHE, not to the host.
    # Left live, these tests read the machine they run on: this box sits
    # between WARN and HIGH under a full suite, so a run that passed in
    # the morning fails in the afternoon for reasons that have nothing
    # to do with the code. Tests that care about pressure store a value
    # explicitly; the rest see UNKNOWN.
    async def _no_refresh(*, force: bool = False):
        return sc._PRESSURE.read()

    monkeypatch.setattr(sc, "refresh_memory_pressure", _no_refresh)
    yield
    sc.reset_default_side_channel_for_tests()


async def _settle(predicate, timeout: float = 2.0) -> bool:
    """Await a condition without sleeping a fixed amount."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Property 1 — submission is instant
# ---------------------------------------------------------------------------


async def test_submit_returns_before_the_provider_is_even_called():
    """THE defect. `/ask` awaited the provider on the input consumer;
    `/btw` must hand back a ticket while the provider has not started."""
    asker = _RecordingAsker(delay=5.0)
    channel = sc.SideChannel(asker=asker)

    t0 = time.monotonic()
    outcome = channel.submit("why did that route to DoubleWord?")
    elapsed = time.monotonic() - t0

    assert outcome.accepted
    assert outcome.ticket is not None
    assert outcome.ticket.ref == "s-1"
    assert outcome.ticket.state is sc.TicketState.PENDING
    # Instant is a property, not a vibe: the ticket exists before the
    # asker has run at all.
    assert elapsed < 0.25
    assert asker.calls == []
    await channel.aclose()


def test_submit_is_synchronous_by_signature():
    """A coroutine here would silently reintroduce the blocking path —
    the caller is `operator_input_queue`'s drain, which awaits handlers.
    """
    assert not asyncio.iscoroutinefunction(sc.SideChannel.submit)
    assert not asyncio.iscoroutinefunction(sc.ask_aside)


async def test_submit_without_a_running_loop_still_ledgers():
    """A REPL surface may submit before the lane's loop exists. The
    ticket must be recorded rather than lost, even with no worker."""
    channel = sc.SideChannel(asker=_RecordingAsker())
    outcome = await asyncio.get_running_loop().run_in_executor(
        None, channel.submit, "asked from a thread",
    )
    assert outcome.accepted
    assert channel.lookup("s-1") is not None


# ---------------------------------------------------------------------------
# The happy path — answered, grounded, delivered, addressed
# ---------------------------------------------------------------------------


async def test_answer_is_delivered_to_the_cockpit_that_asked():
    sink = _Sink()
    sc.set_answer_sink(sink)
    asker = _RecordingAsker()
    channel = sc.SideChannel(asker=asker)

    channel.submit("why is that slow?", session="cockpit-a")
    assert await _settle(lambda: bool(sink.lines))

    ticket = channel.lookup("s-1")
    assert ticket is not None
    assert ticket.state is sc.TicketState.ANSWERED
    assert ticket.answer_ref == "q-1"
    assert ticket.cost_usd == pytest.approx(0.0012)
    assert "Because the route was BACKGROUND." in sink.joined
    assert "why is that slow?" in sink.joined      # the question travels
    assert "/expand q-1" in sink.joined            # and so does the artifact
    # Addressed, not broadcast: the delivery ran inside the originating
    # session's scope even though it happened on a different task.
    assert sink.sessions == ["cockpit-a"]
    await channel.aclose()


async def test_grounding_carries_charter_and_situation():
    sc.register_situation_reader("probe", lambda: "Status line: GENERATE")
    asker = _RecordingAsker()
    channel = sc.SideChannel(asker=asker)
    channel.submit("what is happening?")
    assert await _settle(lambda: bool(asker.calls))

    grounding = asker.calls[0]["grounding"]
    assert "NO authority" in grounding
    assert "Status line: GENERATE" in grounding
    # The question reaches the substrate UNMODIFIED — grounding is a
    # system-side block, so the parked q-N records what was typed.
    assert asker.calls[0]["question"] == "what is happening?"
    assert asker.calls[0]["op_id"] == "btw-s-1"
    await channel.aclose()


async def test_grounding_provenance_is_recorded_on_the_ticket():
    sc.register_situation_reader("alpha", lambda: "A")
    sc.register_situation_reader("beta", lambda: "")   # contributes nothing
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("q")
    assert await _settle(
        lambda: (channel.lookup("s-1") or channel.lookup("s-1"))
        and channel.lookup("s-1").state is sc.TicketState.ANSWERED
    )
    # An answer grounded in nothing must not look like one grounded in
    # the status line.
    assert channel.lookup("s-1").grounded_by == ("alpha",)
    await channel.aclose()


async def test_asker_without_grounding_support_still_answers():
    """A rollback of the substrate must cost the aside its context, not
    the aside."""
    asker = _RecordingAsker(accepts_grounding=False)
    channel = sc.SideChannel(asker=asker)
    channel.submit("q")
    assert await _settle(
        lambda: channel.lookup("s-1").state is sc.TicketState.ANSWERED
    )
    assert asker.calls[-1]["grounding"] is None
    await channel.aclose()


# ---------------------------------------------------------------------------
# Property 2 + 3 — non-preemptive, and bounded
# ---------------------------------------------------------------------------


def _ticket(age_s: float = 0.0, defers: int = 0) -> sc.SideQuestion:
    return sc.SideQuestion(
        ref="s-1", text="q",
        submitted_at=time.monotonic() - age_s,
        defer_count=defers,
    )


def test_admission_defers_while_ops_are_in_flight(monkeypatch):
    monkeypatch.setenv(sc.ENV_OPS_HEADROOM, "2")
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 3)
    verdict = sc.assess_admission(_ticket())
    assert verdict.state is sc.AdmissionState.DEFER
    assert not verdict.admit
    assert "3 op(s) in flight" in verdict.reason
    assert verdict.retry_after_s > 0


def test_admission_defers_under_memory_pressure(monkeypatch):
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 0)
    sc._PRESSURE.store("critical")
    verdict = sc.assess_admission(_ticket())
    assert verdict.state is sc.AdmissionState.DEFER
    assert "memory pressure critical" in verdict.reason


def test_unknown_signals_never_arm_the_gate(monkeypatch):
    """A probe that cannot read must not be able to HOLD a question by
    reporting a value it never measured — nor to clear one."""
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: None)
    sc._PRESSURE.clear()
    verdict = sc.assess_admission(_ticket())
    assert verdict.state is sc.AdmissionState.READY
    assert dict(verdict.signals)["ops_inflight"] == "unknown"
    assert dict(verdict.signals)["memory_pressure"] == "unknown"


def test_pressure_reading_ages_out_to_unknown_never_to_ok(monkeypatch):
    monkeypatch.setenv(sc.ENV_PRESSURE_TTL_S, "1")
    sc._PRESSURE.store("critical")
    assert sc._PRESSURE.read() == "critical"
    monkeypatch.setattr(
        sc.time, "monotonic", lambda: time.monotonic() + 60.0,
    )
    # None, NOT "ok" — the sampler that decays to healthy is the one
    # that silently disarms the guard it feeds.
    assert sc._PRESSURE.read() is None


def test_high_pressure_alone_does_not_hold_an_aside():
    """The threshold that made this lane defer everything on a warm box.
    `MemoryPressureGate` bounds FAN-OUT; an aside is one bounded
    read-only call, so HIGH is not its concern — only CRITICAL, where
    the process may be reaped before anyone reads the answer."""
    sc._PRESSURE.store("high")
    assert sc.assess_admission(_ticket()).state is sc.AdmissionState.READY
    sc._PRESSURE.store("critical")
    assert sc.assess_admission(_ticket()).state is sc.AdmissionState.DEFER


def test_pressure_hold_levels_are_tunable(monkeypatch):
    monkeypatch.setenv(sc.ENV_PRESSURE_HOLD, "high,critical")
    sc._PRESSURE.store("high")
    assert sc.assess_admission(_ticket()).state is sc.AdmissionState.DEFER


@pytest.mark.parametrize("value", ["", "   ", ",,,"])
def test_a_blank_hold_knob_keeps_the_default_not_nothing(value, monkeypatch):
    """Disarming a dimension must be a decision, never a typo."""
    monkeypatch.setenv(sc.ENV_PRESSURE_HOLD, value)
    assert sc.pressure_hold_levels() == ("critical",)


def test_a_failed_probe_does_not_erase_a_good_reading():
    sc._PRESSURE.store("high")
    sc._PRESSURE.store(None)
    assert sc._PRESSURE.read() == "high"


def test_deferral_is_bounded_then_forced(monkeypatch):
    monkeypatch.setenv(sc.ENV_OPS_HEADROOM, "1")
    monkeypatch.setenv(sc.ENV_DEFER_MAX_S, "30")
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 9)

    assert sc.assess_admission(_ticket(age_s=5)).state is (
        sc.AdmissionState.DEFER
    )
    forced = sc.assess_admission(_ticket(age_s=31))
    # Past the ceiling the question is asked ANYWAY. A deferral with no
    # ceiling is a drop with better manners.
    assert forced.state is sc.AdmissionState.FORCED
    assert forced.admit
    assert "waited" in forced.reason


def test_admission_can_be_disabled_wholesale(monkeypatch):
    monkeypatch.setenv(sc.ENV_ADMISSION, "false")
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 99)
    assert sc.assess_admission(_ticket()).state is sc.AdmissionState.READY


def test_backoff_is_bounded_and_jittered(monkeypatch):
    monkeypatch.setenv(sc.ENV_DEFER_BASE_S, "1")
    monkeypatch.setenv(sc.ENV_DEFER_CEILING_S, "5")
    samples = [sc._backoff_delay(n) for n in range(0, 12)]
    assert all(0.05 <= s <= 5.0 for s in samples)
    # Jitter: several asides that defer together must not re-check in
    # lockstep against the very signals telling them to wait.
    assert len({round(s, 6) for s in samples}) > 1


async def test_a_forced_answer_says_it_was_forced(monkeypatch):
    monkeypatch.setenv(sc.ENV_OPS_HEADROOM, "1")
    monkeypatch.setenv(sc.ENV_DEFER_MAX_S, "0.01")
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 5)
    sink = _Sink()
    sc.set_answer_sink(sink)
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("q")
    assert await _settle(lambda: bool(sink.lines), timeout=3.0)
    assert channel.lookup("s-1").forced is True
    assert "admitted under load" in sink.joined
    await channel.aclose()


async def test_a_deferred_ticket_is_visibly_deferred(monkeypatch):
    monkeypatch.setenv(sc.ENV_OPS_HEADROOM, "1")
    monkeypatch.setenv(sc.ENV_DEFER_MAX_S, "600")
    monkeypatch.setenv(sc.ENV_DEFER_BASE_S, "0.1")
    monkeypatch.setattr(sc, "_inflight_op_count", lambda: 4)
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("q")
    # DEFERRED is a distinct state from PENDING on purpose: "waiting its
    # turn" and "waiting for room" look identical in a depth counter.
    assert await _settle(
        lambda: channel.lookup("s-1").state is sc.TicketState.DEFERRED,
        timeout=3.0,
    )
    assert channel.lookup("s-1").defer_reason
    assert channel.lookup("s-1").defer_count >= 1
    await channel.aclose()


# ---------------------------------------------------------------------------
# Bounds — refuse, never drop
# ---------------------------------------------------------------------------


def test_queue_full_refuses_with_an_actionable_reason(monkeypatch):
    channel = sc.SideChannel(max_live=2, asker=_RecordingAsker())
    assert channel.submit("one").accepted
    assert channel.submit("two").accepted
    third = channel.submit("three")
    assert not third.accepted
    assert "queue full" in third.reason
    assert "/btw cancel" in third.reason
    assert channel.refused == 1
    # Refused means NOT ledgered — the operator was told, so there is no
    # phantom ticket they believe is coming.
    assert channel.all_refs() == ("s-1", "s-2")


def test_eviction_never_touches_a_live_ticket():
    channel = sc.SideChannel(capacity=4, max_live=10,
                             asker=_RecordingAsker())
    for i in range(6):
        channel.submit(f"question {i}")
    # Capacity is 4 but all six are unanswered: nothing terminal exists
    # to evict, so nothing is evicted. Silently discarding an unanswered
    # question is the one outcome this module refuses.
    assert len(channel.all_refs()) == 6
    channel.cancel("s-1")
    channel.cancel("s-2")
    channel.submit("question 6")
    refs = channel.all_refs()
    assert "s-1" not in refs and "s-2" not in refs
    assert len(refs) == 5


def test_refs_are_monotonic_and_never_reused():
    channel = sc.SideChannel(capacity=2, max_live=10,
                             asker=_RecordingAsker())
    for i in range(4):
        channel.submit(f"q{i}")
        channel.cancel(f"s-{i + 1}")
    assert channel.all_refs()[-1] == "s-4"
    assert channel.snapshot().next_seq == 5


def test_identical_live_questions_coalesce():
    channel = sc.SideChannel(asker=_RecordingAsker())
    first = channel.submit("Why is  that   slow?")
    second = channel.submit("why is that slow?")     # same, differently typed
    assert second.accepted and second.coalesced
    assert second.ticket.ref == first.ticket.ref
    assert len(channel.all_refs()) == 1


def test_a_repeat_of_a_FINISHED_question_is_a_new_ticket():
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("same question")
    channel.cancel("s-1")
    again = channel.submit("same question")
    # Coalescing folds impatience, not curiosity: once a question is
    # resolved, asking it again is a new question.
    assert again.accepted and not again.coalesced
    assert again.ticket.ref == "s-2"


def test_long_questions_are_truncated_not_refused(monkeypatch):
    monkeypatch.setenv(sc.ENV_MAX_QUESTION_CHARS, "64")
    channel = sc.SideChannel(asker=_RecordingAsker())
    outcome = channel.submit("x" * 500)
    assert outcome.accepted
    assert len(outcome.ticket.text) == 64


def test_empty_and_garbage_submissions_are_refused_not_raised():
    channel = sc.SideChannel(asker=_RecordingAsker())
    assert not channel.submit("").accepted
    assert not channel.submit("   ").accepted
    assert not channel.submit(None).accepted


def test_master_flag_off_names_the_flag(monkeypatch):
    monkeypatch.setenv(sc.ENV_MASTER, "false")
    outcome = sc.SideChannel(asker=_RecordingAsker()).submit("q")
    assert not outcome.accepted
    assert sc.ENV_MASTER in outcome.reason


def test_unrecognised_flag_text_keeps_the_default(monkeypatch):
    """A typo'd flag must not silently disarm the lane."""
    monkeypatch.setenv(sc.ENV_MASTER, "ture")
    assert sc.side_channel_enabled() is True


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancel_stops_an_in_flight_provider_call():
    asker = _RecordingAsker(delay=30.0)
    sink = _Sink()
    sc.set_answer_sink(sink)
    channel = sc.SideChannel(asker=asker)
    channel.submit("q")
    assert await _settle(lambda: asker.started.is_set())

    assert channel.cancel("s-1") is not None
    assert channel.lookup("s-1").state is sc.TicketState.CANCELLED
    # Nothing is delivered for a question the operator withdrew.
    await asyncio.sleep(0.05)
    assert sink.lines == []
    await channel.aclose()


async def test_a_late_answer_cannot_resurrect_a_cancelled_ticket():
    """The race that matters: `cancel` runs on the operator's task while
    the worker is mid-answer on its own."""
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("q")
    channel.cancel("s-1")
    ticket = channel.lookup("s-1")
    # Simulate the provider result landing after the withdrawal.
    assert channel._transition(
        "s-1", state=sc.TicketState.ANSWERED, answer="too late",
    ) is None
    assert channel.lookup("s-1").state is sc.TicketState.CANCELLED
    assert ticket.state is sc.TicketState.CANCELLED
    await channel.aclose()


def test_cancelling_an_unknown_or_finished_ref_is_a_no_op():
    channel = sc.SideChannel(asker=_RecordingAsker())
    assert channel.cancel("s-99") is None
    channel.submit("q")
    assert channel.cancel("s-1") is not None
    assert channel.cancel("s-1") is None


# ---------------------------------------------------------------------------
# Verdicts that are not answers
# ---------------------------------------------------------------------------


async def test_a_disabled_substrate_is_reported_not_swallowed():
    """`disabled` and `budget_exhausted` are the two most likely
    outcomes on a fresh install. An operator who sees nothing cannot
    tell them from a lane that ate the question."""
    report = _FakeReport(
        verdict=_FakeVerdict("disabled"),
        artifact=None,
        diagnostic="gate disabled via JARVIS_FAST_PATH_QA_ENABLED=false",
    )
    sink = _Sink()
    sc.set_answer_sink(sink)
    channel = sc.SideChannel(asker=_RecordingAsker(report=report))
    channel.submit("q")
    assert await _settle(lambda: bool(sink.lines))
    assert channel.lookup("s-1").state is sc.TicketState.FAILED
    assert "JARVIS_FAST_PATH_QA_ENABLED" in sink.joined
    await channel.aclose()


async def test_an_asker_that_raises_becomes_a_visible_failure():
    async def _boom(*a, **kw):
        raise RuntimeError("provider exploded")

    sink = _Sink()
    sc.set_answer_sink(sink)
    channel = sc.SideChannel(asker=_boom)
    channel.submit("q")
    assert await _settle(lambda: bool(sink.lines))
    assert channel.lookup("s-1").state is sc.TicketState.FAILED
    await channel.aclose()


async def test_one_bad_ticket_does_not_wedge_the_lane():
    """A drain that dies on one handler would make this lane strictly
    worse than the blocking `/ask` it replaces."""
    calls = {"n": 0}

    async def _flaky(question, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first one explodes")
        return _FakeReport(
            verdict=_FakeVerdict("answered"), artifact=_FakeArtifact(),
        )

    channel = sc.SideChannel(asker=_flaky)
    channel.submit("first")
    channel.submit("second")
    assert await _settle(
        lambda: channel.lookup("s-2") is not None
        and channel.lookup("s-2").state is sc.TicketState.ANSWERED,
        timeout=3.0,
    )
    assert channel.lookup("s-1").state is sc.TicketState.FAILED
    await channel.aclose()


# ---------------------------------------------------------------------------
# Situation digest
# ---------------------------------------------------------------------------


def test_one_bad_reader_costs_only_its_own_section():
    def _boom() -> str:
        raise RuntimeError("reader exploded")

    sc.register_situation_reader("bad", _boom)
    sc.register_situation_reader("good", lambda: "still here")
    block = sc.compose_situation_sync()
    assert "still here" in block.text
    assert block.readers == ("bad", "good")[1:]


def test_digest_is_truncated_from_the_tail(monkeypatch):
    monkeypatch.setenv(sc.ENV_SITUATION_MAX_CHARS, "200")
    sc.register_situation_reader("head", lambda: "HEAD" + "a" * 150)
    sc.register_situation_reader("tail", lambda: "TAIL" + "z" * 400)
    block = sc.compose_situation_sync()
    assert block.truncated
    assert block.text.startswith("HEAD")
    assert "truncated" in block.text
    assert len(block.text) < 400


def test_readers_can_be_restricted_by_allowlist(monkeypatch):
    sc.register_situation_reader("wanted", lambda: "keep me")
    sc.register_situation_reader("noisy", lambda: "drop me")
    monkeypatch.setenv(sc.ENV_SITUATION_READERS, "wanted")
    block = sc.compose_situation_sync()
    assert block.readers == ("wanted",)
    assert "drop me" not in block.text


def test_situation_can_be_disabled(monkeypatch):
    sc.register_situation_reader("x", lambda: "context")
    monkeypatch.setenv(sc.ENV_SITUATION, "false")
    assert sc.compose_situation_sync().text == ""


def test_charter_travels_even_with_no_situation():
    grounding = sc.build_grounding(sc.SituationBlock())
    assert "NO authority" in grounding
    assert "/goal" in grounding


def test_charter_is_operator_overridable(monkeypatch):
    monkeypatch.setenv(sc.ENV_CHARTER, "answer in haiku")
    assert sc.charter() == "answer in haiku"
    assert "answer in haiku" in sc.build_grounding(sc.SituationBlock())


async def test_a_hanging_reader_costs_context_not_the_answer(monkeypatch):
    monkeypatch.setenv(sc.ENV_SITUATION_BUDGET_S, "0.1")
    sc.register_situation_reader("slow", lambda: (time.sleep(3), "late")[1])
    block = await sc.compose_situation()
    assert block.text == ""


def test_default_readers_are_registered_and_named():
    sc.reset_default_side_channel_for_tests()
    names = sc.situation_reader_names()
    assert {"status", "inflight", "narrative", "ops", "posture",
            "queue"} <= set(names)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_unanswered_asides_are_reported_at_shutdown():
    sink = _Sink()
    sc.set_answer_sink(sink)
    channel = sc.SideChannel(asker=_RecordingAsker(delay=30.0))
    channel.submit("never answered", session="cockpit-b")
    await asyncio.sleep(0.05)
    await channel.aclose()
    assert channel.lookup("s-1").state is sc.TicketState.ABANDONED
    assert "unanswered at shutdown" in sink.joined
    assert "s-1" in sink.joined


def test_the_termination_hook_is_sync_and_loop_free():
    """It runs on a threading.Thread precisely so it survives a wedged
    loop — so it must not touch a Task, a Future, or a loop."""
    import inspect
    assert not asyncio.iscoroutinefunction(sc._abandon_pending_hook)
    assert not asyncio.iscoroutinefunction(sc.SideChannel.abandon_live_sync)
    body = inspect.getsource(sc.SideChannel.abandon_live_sync)
    for forbidden in ("await ", "asyncio.", ".cancel()", "create_task"):
        assert forbidden not in body, forbidden


def test_termination_hook_registers_once_and_is_flag_gated():
    from backend.core.ouroboros.battle_test.termination_hook import (
        TerminationPhase,
    )
    from backend.core.ouroboros.battle_test.termination_hook_registry import (
        TerminationHookRegistry,
    )
    registry = TerminationHookRegistry()
    assert sc.register_termination_hooks(registry) == 1
    assert sc.register_termination_hooks(registry) == 0
    names = [
        h.name for h in registry.for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        )
    ]
    assert sc.TERMINATION_HOOK_NAME in names


def test_termination_hook_on_an_unused_lane_does_nothing():
    sc.reset_default_side_channel_for_tests()
    sc._abandon_pending_hook(object())     # no channel was ever built


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_delivery_waits_for_a_modal_overlay_then_gives_up(monkeypatch):
    """An Iron Gate prompt is a decision in progress; painting an aside
    over it is the interruption the lane exists to avoid. Bounded,
    because a gate the operator walked away from must not swallow the
    answer with it."""
    monkeypatch.setattr(sc, "_overlay_owns_screen", lambda: True)
    t0 = time.monotonic()
    assert await sc._await_clear_screen(0.3) is False
    assert time.monotonic() - t0 >= 0.25


async def test_delivery_proceeds_once_the_overlay_clears(monkeypatch):
    state = {"up": True}
    monkeypatch.setattr(sc, "_overlay_owns_screen", lambda: state["up"])

    async def _clear() -> None:
        await asyncio.sleep(0.05)
        state["up"] = False

    asyncio.get_running_loop().create_task(_clear())
    assert await sc._await_clear_screen(3.0) is True


def test_emit_falls_back_to_the_attach_bridge_with_no_sink(monkeypatch):
    sc.set_answer_sink(None)
    seen: List[Tuple[str, Any]] = []
    import backend.core.ouroboros.battle_test.cockpit_attach as ca
    monkeypatch.setattr(
        ca, "publish_markup_global",
        lambda text, session=None: (seen.append((text, session)), True)[1],
    )
    assert sc.emit_markup("hello", "cockpit-c") is True
    assert seen == [("hello", "cockpit-c")]


def test_markup_from_model_and_operator_text_is_inert():
    """Question text is operator-controlled and answer text is
    model-controlled; neither may open a style tag."""
    ticket = sc.SideQuestion(
        ref="s-1", text="[bold]not a tag[/bold]",
        state=sc.TicketState.ANSWERED,
        answer="[red]neither is this[/red]", answer_ref="q-1",
    )
    rendered = sc.render_answer(ticket)
    # Escaped, i.e. every markup-looking run is preceded by a backslash.
    # Asserting the raw substring is absent would be wrong: `\[red]` still
    # CONTAINS `[red]` — what matters is that Rich cannot open a tag.
    from rich.text import Text
    body = "\n".join(
        ln for ln in rendered.splitlines()
        if "neither" in ln or "not a tag" in ln
    )
    assert "not a tag" in Text.from_markup(body).plain
    assert "[red]neither is this[/red]" in Text.from_markup(body).plain


# ---------------------------------------------------------------------------
# Observability + housekeeping
# ---------------------------------------------------------------------------


def test_snapshot_projects_bounded_state():
    channel = sc.SideChannel(asker=_RecordingAsker())
    channel.submit("q one")
    snap = channel.snapshot()
    payload = snap.to_dict()
    assert payload["live"] == 1
    assert payload["schema_version"] == sc.SIDE_CHANNEL_SCHEMA_VERSION
    assert payload["tickets"][0]["question"] == "q one"
    # The body is a LENGTH, not the text: an observability surface must
    # not become a way to read a whole answer out of the process.
    assert "answer_chars" in payload["tickets"][0]
    assert "answer" not in payload["tickets"][0]


def test_every_env_knob_is_clamped(monkeypatch):
    for env, fn, lo, hi in (
        (sc.ENV_QUEUE_DEPTH, sc.queue_depth, 1, 256),
        (sc.ENV_LEDGER_SIZE, sc.ledger_size, 4, 1000),
        (sc.ENV_CONCURRENCY, sc.concurrency, 1, 8),
        (sc.ENV_OPS_HEADROOM, sc.ops_headroom, 0, 64),
        (sc.ENV_DEFER_MAX_S, sc.defer_max_s, 0, 3600),
        (sc.ENV_DELIVERY_HOLD_S, sc.delivery_hold_s, 0, 300),
        (sc.ENV_PRESSURE_TTL_S, sc.pressure_ttl_s, 1, 600),
    ):
        monkeypatch.setenv(env, "-999999")
        assert fn() == lo, env
        monkeypatch.setenv(env, "999999")
        assert fn() == hi, env
        monkeypatch.setenv(env, "not a number")
        assert lo <= fn() <= hi, env


def test_flags_are_declared_to_the_registry():
    class _Reg:
        def __init__(self) -> None:
            self.specs: List[Any] = []

        def register(self, spec: Any) -> None:
            self.specs.append(spec)

    reg = _Reg()
    count = sc.register_flags(reg)
    declared = {s.name for s in reg.specs}
    assert count == len(reg.specs)
    # Every knob this module reads must be findable via `/help flags`.
    for env in (
        sc.ENV_MASTER, sc.ENV_QUEUE_DEPTH, sc.ENV_LEDGER_SIZE,
        sc.ENV_CONCURRENCY, sc.ENV_ADMISSION, sc.ENV_OPS_HEADROOM,
        sc.ENV_DEFER_MAX_S, sc.ENV_DEFER_BASE_S, sc.ENV_DEFER_CEILING_S,
        sc.ENV_PRESSURE_TTL_S, sc.ENV_SITUATION,
        sc.ENV_SITUATION_BUDGET_S, sc.ENV_SITUATION_MAX_CHARS,
        sc.ENV_SITUATION_READERS, sc.ENV_DELIVERY_HOLD_S, sc.ENV_CHARTER,
        sc.ENV_PRESSURE_HOLD,
        sc.ENV_MAX_QUESTION_CHARS,
    ):
        assert env in declared, env


def test_authority_asymmetry_is_structural():
    """The lane carries text and decides WHEN to ask. It must never be
    able to decide WHAT to do."""
    import ast
    import pathlib
    source = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    forbidden = (
        "orchestrator", "iron_gate", "policy_engine", "change_engine",
        "candidate_generator", "urgency_router", "semantic_guardian",
        "tool_executor", "auto_committer", "risk_tier_floor",
    )
    for module in imported:
        assert not any(f in module for f in forbidden), module


def test_public_surface_matches_all():
    assert all(hasattr(sc, name) for name in sc.__all__)
    assert sorted(set(sc.__all__)) == sorted(sc.__all__)

def test_shipped_invariants_are_declared_and_hold():
    """The unit suite proves these TODAY; the pins make a later
    refactor that loses one fail the audit rather than fail silently."""
    import ast
    import pathlib
    invariants = sc.register_shipped_invariants()
    names = {i.invariant_name for i in invariants}
    assert {
        "side_channel_submit_is_sync",
        "side_channel_termination_hook_loop_free",
        "side_channel_admission_reads_never_probes",
        "side_channel_authority_asymmetry",
        "side_channel_no_parallel_provider",
        "side_channel_ticket_state_closed",
    } <= names
    source = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in invariants:
        assert inv.target_file.endswith("side_channel.py")
        assert inv.validate(tree, source) == (), inv.invariant_name


def test_an_invariant_actually_fires_on_a_violation():
    """A validator that returns () for everything is a pin that pins
    nothing — the shape this repo calls a zero-caller filter."""
    import ast
    invariants = {i.invariant_name: i for i in sc.register_shipped_invariants()}
    broken = ast.parse(
        "import asyncio\n"
        "class SideChannel:\n"
        "    async def submit(self, text):\n"
        "        return None\n"
    )
    assert invariants["side_channel_submit_is_sync"].validate(broken, "") != ()

    authority = ast.parse(
        "from backend.core.ouroboros.governance.orchestrator import Thing\n"
    )
    assert invariants[
        "side_channel_authority_asymmetry"
    ].validate(authority, "") != ()

