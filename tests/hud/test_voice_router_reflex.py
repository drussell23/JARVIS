"""The voice path must act, and must tell the truth about it.

Reconstructs the exact failure from the boot log of 2026-08-02 22:51:54: the
operator said "lock my screen", the classifier's model call was refused, its
fallback was the MORE model-dependent branch, that was refused too, and JARVIS
answered by reading the refusal string aloud through a speech synthesiser.

`DeadCortex` below is that outage. Every test in this file runs with it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.hud.tool_use_orchestrator import CommandResult
from backend.hud.voice_command_router import (
    VoiceCommandRouter, _humanise, _make_failure_speakable,
    _read_controller_result, reset_spoken,
)


@pytest.fixture(autouse=True)
def _forget_what_jarvis_said():
    """The router now narrates "On it — lock screen" before acting, and that
    utterance is recorded so the microphone cannot hear it back as a command.
    Across tests the ledger would leak, and one test's narration would suppress
    the next test's command — which is exactly the production bug in miniature.
    """
    reset_spoken()
    yield
    reset_spoken()

REFUSAL = ("session_budget_preflight_refused:soak_circuit_tripped:"
           "soak_cost_cap_exceeded:$238111.9488>=$2.0000:on_boot: "
           "provider=doubleword est=$0.1000 > session_remaining=$0.0000")


class DeadCortex:
    """Every model dispatch refused — the measured outage."""

    def __init__(self):
        self.calls = 0

    async def prompt_only(self, *a, **k):
        self.calls += 1
        raise RuntimeError(REFUSAL)


class _Routed:
    def __init__(self, outcome, result=None, detail="", capability="lock_screen"):
        self.outcome = outcome
        self.result = result
        self.detail = detail
        self.capability = capability
        self.request_id = "rid-1234567890"
        self.context_note = ""


@pytest.fixture()
def router():
    return VoiceCommandRouter(DeadCortex())


def _fake_router(monkeypatch, routed):
    """Intercept at the capability boundary — nothing may touch the real Mac."""
    import backend.system_control.capability_router as CR
    calls = []

    class Stub:
        async def route(self, name, args=None, *, op_id=""):
            calls.append((name, dict(args or {})))
            return routed

    monkeypatch.setattr(CR, "get_capability_router", lambda: Stub())
    return calls


# ── The reflex fires without the cortex ─────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_my_screen_reaches_the_capability_with_the_model_dead(
        router, monkeypatch):
    calls = _fake_router(monkeypatch, _Routed(
        "executed", result=(True, "🔒 Locking the screen now, Derek.")))

    res = await router.route("lock my screen")

    assert calls == [("lock_screen", {})], "the capability was never reached"
    assert res.success and res.category == "system_action"
    assert res.response_text == "🔒 Locking the screen now, Derek."


@pytest.mark.asyncio
async def test_no_model_call_is_made_at_all(router, monkeypatch):
    """Not merely 'it works when the model is down' — it does not ASK.

    Two round-trips for a sentence that names a capability outright is the
    cost this exists to delete, and it is a cost paid when the model is UP.
    """
    _fake_router(monkeypatch, _Routed("executed", result=(True, "ok")))
    await router.route("lock my screen")
    assert router._dw.calls == 0


@pytest.mark.asyncio
async def test_the_capability_is_called_with_no_invented_arguments(
        router, monkeypatch):
    calls = _fake_router(monkeypatch, _Routed("executed", result=(True, "ok")))
    await router.route("lock my screen")
    assert calls[0][1] == {}


# ── It does not lie about what happened ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_capability_that_failed_without_raising_is_not_a_success(
        router, monkeypatch):
    """`lock_screen` answers `(False, "Unable to lock screen")` WITHOUT
    raising, so the router reports EXECUTED. Reading only that outcome would
    announce a locked screen to somebody looking at an unlocked one."""
    _fake_router(monkeypatch, _Routed(
        "executed", result=(False, "❌ Unable to lock screen (all methods failed).")))

    res = await router.route("lock my screen")

    assert res.success is False
    assert "Unable to lock screen" in res.response_text


@pytest.mark.asyncio
async def test_awaiting_consent_is_neither_success_nor_failure(
        router, monkeypatch):
    _fake_router(monkeypatch, _Routed("suspended", detail="operator consent required"))

    res = await router.route("lock my screen")

    assert res.pending is True and res.success is False
    assert "approval" in res.response_text.lower()


@pytest.mark.asyncio
async def test_nobody_to_ask_is_not_reported_as_a_refusal(router, monkeypatch):
    """The operator needs opposite follow-ups for 'you said no' and 'nothing
    could ask you'."""
    _fake_router(monkeypatch, _Routed(
        "denied", detail="no approval provider available — failing closed"))

    res = await router.route("lock my screen")

    assert "couldn't ask anyone" in res.response_text
    assert "declined" not in res.response_text


@pytest.mark.asyncio
async def test_an_operator_refusal_says_so(router, monkeypatch):
    _fake_router(monkeypatch, _Routed("denied", detail="operator declined"))
    res = await router.route("lock my screen")
    assert "declined" in res.response_text


# ── The model path still speaks like a person ───────────────────────────────

@pytest.mark.asyncio
async def test_an_unreflexive_command_still_goes_to_the_model(router):
    res = await router.route("open chrome and check my email")
    assert router._dw.calls > 0
    assert res.success is False


@pytest.mark.asyncio
async def test_a_provider_refusal_is_never_read_aloud_verbatim(router):
    res = await router.route("open chrome and check my email")
    for leak in ("session_budget_preflight_refused", "$238111", "doubleword",
                 "soak_circuit_tripped", "Traceback"):
        assert leak not in (res.response_text or ""), (
            f"a diagnostic reached the speech synthesiser: {leak}")
    assert "spending limit" in res.response_text
    # The diagnostic is KEPT — for the log, which can be read at leisure.
    assert "session_budget_preflight_refused" in (res.error or "")


def test_the_translator_never_overwrites_a_truthful_message():
    """A real bug found while building this.

    The capability path answered "I couldn't ask anyone to approve lock
    screen, so I didn't do it" — true and actionable — and the translator saw
    the word "provider" inside `no approval provider available` and replaced
    it with "My language model isn't answering right now", which is false; no
    model was involved. A translator that can turn a true sentence into a
    false one is worse than the diagnostic it replaced.
    """
    honest = "I couldn't ask anyone to approve lock screen, so I didn't do it."
    res = CommandResult(success=False, category="system_action",
                        steps_completed=0, steps_total=1,
                        response_text=honest,
                        error="no approval provider available — failing closed")

    class Resolved:
        resolved = True
        outcome = "resolved"
        capability = "lock_screen"

    assert _make_failure_speakable(res, Resolved()).response_text == honest


def test_a_real_task_failure_keeps_its_own_words():
    res = CommandResult(success=False, category="app_action", steps_completed=0,
                        steps_total=1, response_text="Couldn't open Chrome.",
                        error="Couldn't open Chrome.")
    assert _make_failure_speakable(res, None).response_text == "Couldn't open Chrome."


@pytest.mark.parametrize("detail,expect", [
    ("Model error: 403 entitlement denied", "authorised"),
    ("Model error: read timeout after 120s", "too long"),
    ("Model error: cannot connect to host", "can't reach"),
    (REFUSAL, "spending limit"),
])
def test_provider_faults_are_classified_by_shape(detail, expect):
    res = CommandResult(success=False, category="composite", steps_completed=0,
                        steps_total=0, response_text=detail, error=detail)
    assert expect in _make_failure_speakable(res, None).response_text


def test_a_near_miss_is_offered_rather_than_acted_on():
    class NearMiss:
        resolved = False
        outcome = "low_confidence"
        capability = "lock_screen"

    res = CommandResult(success=False, category="composite", steps_completed=0,
                        steps_total=0, response_text=REFUSAL, error=REFUSAL)
    spoken = _make_failure_speakable(res, NearMiss()).response_text
    assert "Did you mean lock screen?" in spoken


# ── Small pieces ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,said", [
    ("lock_screen", "lock screen"),
    ("video.start_streaming", "start streaming"),
    ("", "that"),
])
def test_humanise_is_derived_not_looked_up(name, said):
    assert _humanise(name) == said


def test_controller_results_speak_for_themselves():
    assert _read_controller_result((True, "🔒 Locking now."), "lock screen") == (
        True, "🔒 Locking now.")
    ok, say = _read_controller_result(None, "lock screen")
    assert ok and "lock screen" in say


# ── The 2026-08-03 01:41 log ────────────────────────────────────────────────

def test_a_real_reason_is_never_replaced_with_you_declined():
    """Measured 01:41:27. The voice authority answered "I don't have a
    voiceprint for you on this Mac yet"; the DENIED branch recognised only the
    no-provider case and said "You declined unlock screen, so I left it
    alone." Derek declined nothing — the truth was discarded and replaced with
    a fabrication about the operator's own behaviour."""
    from backend.hud.voice_command_router import _reads_like_a_sentence
    authority = ("I don't have a voiceprint for you on this Mac yet, so I "
                 "can't confirm it's you.")
    assert _reads_like_a_sentence(authority)
    assert not _reads_like_a_sentence(
        "consent channel unreachable — HUDConsentProvider needs screen_unlocked")


@pytest.mark.asyncio
async def test_the_same_action_does_not_run_twice_concurrently(monkeypatch):
    """Boot held the loop for 31s, so Derek said it again and the screen
    locked TWICE. Echo suppression is blind to this: the repeat arrived before
    JARVIS had spoken, so there was nothing to be an echo of."""
    import backend.system_control.capability_router as CR
    calls = []

    class Slow:
        async def route(self, name, args=None, *, op_id=""):
            calls.append(name)
            await asyncio.sleep(0.3)
            return _Routed("executed", result=(True, "Locked."))

    monkeypatch.setattr(CR, "get_capability_router", lambda: Slow())
    r = VoiceCommandRouter(DeadCortex())
    await asyncio.gather(r.route("lock my screen"), r.route("Lock screen"))
    assert calls == ["lock_screen"], f"ran twice: {calls}"


@pytest.mark.asyncio
async def test_the_slot_is_released_so_a_capability_still_works_after(monkeypatch):
    """A leaked slot would turn one crash into "JARVIS will not lock my screen
    any more" — worse than the double-lock it prevents."""
    import backend.system_control.capability_router as CR
    calls = []

    class Boom:
        async def route(self, name, args=None, *, op_id=""):
            calls.append(name)
            raise RuntimeError("router exploded")

    monkeypatch.setattr(CR, "get_capability_router", lambda: Boom())
    r = VoiceCommandRouter(DeadCortex())
    await r.route("lock my screen")
    await r.route("lock my screen")
    assert calls == ["lock_screen", "lock_screen"], "the slot leaked"
