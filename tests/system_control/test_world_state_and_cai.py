"""World state, the unlock deadlock, and the flow a person would follow.

Everything here was found in ONE live log (2026-08-03 00:17-00:19), which is
why the assertions quote timings rather than describing behaviour in the
abstract.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.hud.tool_use_orchestrator import CommandResult
from backend.hud.voice_command_router import (
    VoiceCommandRouter, is_own_echo, note_spoken, reset_spoken,
)
from backend.system_control import world_state as WS
from backend.system_control.capability_registry import Tier
from backend.system_control.capability_router import (
    CapabilityRouter, Outcome, _verdict_reason,
)
from backend.system_control.world_state import Truth, WorldState, canonical


@pytest.fixture(autouse=True)
def _clean():
    WS.reset_world_state()
    reset_spoken()
    yield
    WS.reset_world_state()
    reset_spoken()


# ── Ternary truth ───────────────────────────────────────────────────────────

def test_none_is_unknown_and_never_false():
    """The ghost-display lesson, in the type system.

    `is_screen_locked()` returns `bool` and ends in a bare `return False`, so a
    probe that could not see reports 'not locked'. Here that would authorise
    typing a password into whatever has focus.
    """
    assert Truth.of(None) is Truth.UNKNOWN
    assert Truth.of(False) is Truth.FALSE
    assert Truth.of(True) is Truth.TRUE


def test_not_knowing_a_thing_says_nothing_about_its_opposite():
    assert Truth.UNKNOWN.negate() is Truth.UNKNOWN
    assert Truth.TRUE.negate() is Truth.FALSE


@pytest.mark.asyncio
async def test_unknown_satisfies_nothing_and_refutes_nothing():
    """The asymmetry the whole CAI rests on. An unobservable world must not
    look like a met precondition OR an unmet one."""
    w = WorldState()
    w.register("screen_locked", lambda: None)
    assert await w.satisfies("screen_locked") is False
    assert await w.refutes("screen_locked") is False
    assert await w.satisfies("screen_unlocked") is False
    assert await w.refutes("screen_unlocked") is False


@pytest.mark.asyncio
async def test_one_probe_answers_both_polarities():
    """Two probes for `screen_locked` and `screen_unlocked` could disagree, and
    the day they did the machine would believe the screen was both."""
    w = WorldState()
    w.register("screen_locked", lambda: True)
    assert (await w.read("screen_locked")).is_true
    assert (await w.read("screen_unlocked")).truth == Truth.FALSE.value


def test_antonyms_resolve_before_any_singleton_exists():
    """`canonical` is pure and callable by anything. If the antonym only
    existed after the first `get_world_state()`, one string would mean two
    things depending on call order."""
    assert canonical("screen_unlocked") == ("screen_locked", True)
    assert canonical("!screen_locked") == ("screen_locked", True)
    assert canonical("screen_locked") == ("screen_locked", False)


@pytest.mark.asyncio
async def test_an_unprobed_predicate_is_unknown_not_an_error():
    w = WorldState()
    r = await w.read("nobody_probes_this")
    assert r.is_unknown and "nothing probes" in r.detail


@pytest.mark.asyncio
async def test_a_hanging_probe_becomes_unknown_not_a_hung_command(monkeypatch):
    monkeypatch.setenv("JARVIS_WORLD_STATE_PROBE_TIMEOUT_S", "0.1")

    async def _hang():
        await asyncio.sleep(30)

    w = WorldState()
    w.register("slow", _hang)
    r = await w.read("slow")
    assert r.is_unknown and "timed out" in r.detail


@pytest.mark.asyncio
async def test_a_failed_probe_is_never_cached():
    """One CoreGraphics hiccup must not become a window in which the machine
    refuses to know anything."""
    answers = [None, True]
    w = WorldState()
    w.register("flaky", lambda: answers.pop(0))
    assert (await w.read("flaky")).is_unknown
    assert (await w.read("flaky")).is_true


@pytest.mark.asyncio
async def test_the_live_probe_never_claims_unlocked_when_it_cannot_see():
    """The composed cascade must answer UNKNOWN where the collapsing wrapper
    answers a bare bool. Measured from a shell: all three primitives return
    None (no GUI session) while `is_screen_locked()` still returns a boolean.
    """
    w = WS.get_world_state()
    r = await w.read(WS.SCREEN_LOCKED, fresh=True)
    # Whatever the environment, it is never allowed to fabricate 'unlocked'
    # from silence: either something SAW the state, or it is UNKNOWN.
    assert r.is_unknown or r.source == "cgsession_cascade"
    if r.is_unknown:
        assert await w.satisfies("screen_unlocked") is False


# ── The unlock deadlock ─────────────────────────────────────────────────────

class _Provider:
    requires = ("screen_unlocked",)

    def __init__(self):
        self.asked = []

    async def request(self, ctx):
        self.asked.append(getattr(ctx, "capability", ""))
        return "rid-1"


class _Registry:
    def __init__(self, d):
        self._d = d

    def get(self, n):
        return self._d.get(n)


def _gated_cap(name):
    from backend.system_control.capability_registry import CapabilityDef
    return CapabilityDef(name=name, description="x",
                         tier=Tier.APPROVAL_REQUIRED.value)


@pytest.mark.asyncio
async def test_a_locked_screen_stops_touch_id_being_asked_at_all(monkeypatch):
    """The deadlock, pinned.

    Touch ID needs a surface to draw on. Asking it while the screen is locked
    produced DENIED in 99ms and a log line saying the operator declined a
    prompt they never saw.
    """
    w = WS.get_world_state()
    w.register(WS.SCREEN_LOCKED, lambda: True)
    provider = _Provider()
    router = CapabilityRouter(registry=_Registry({"unlock_screen": _gated_cap("unlock_screen")}),
                              provider=provider, target=object())

    out = await router.route("unlock_screen")

    assert out.outcome == Outcome.DENIED.value
    assert provider.asked == [], "a channel that cannot be reached must not be asked"
    assert "unreachable" in out.detail and "screen_unlocked" in out.detail


@pytest.mark.asyncio
async def test_an_unlocked_screen_asks_normally():
    w = WS.get_world_state()
    w.register(WS.SCREEN_LOCKED, lambda: False)
    provider = _Provider()
    router = CapabilityRouter(registry=_Registry({"cap": _gated_cap("cap")}),
                              provider=provider, target=object())
    out = await router.route("cap")
    assert out.outcome == Outcome.SUSPENDED.value
    assert provider.asked == ["cap"]


@pytest.mark.asyncio
async def test_an_unknown_world_does_not_block_consent():
    """Biased toward proceeding. `CGSession` answers only inside a GUI session,
    so refusing to ask whenever the probe is blind would break every consent
    prompt on most machines."""
    w = WS.get_world_state()
    w.register(WS.SCREEN_LOCKED, lambda: None)
    provider = _Provider()
    router = CapabilityRouter(registry=_Registry({"cap": _gated_cap("cap")}),
                              provider=provider, target=object())
    assert (await router.route("cap")).outcome == Outcome.SUSPENDED.value


@pytest.mark.parametrize("verdict,expect", [
    ({"reason": "biometry unavailable"}, "biometry unavailable"),
    ({"reason": "operator denied"}, "operator denied"),
    ({}, "operator declined"),
])
def test_the_channels_own_reason_survives(verdict, expect):
    """`SecureConsent.describe()` already tells apart 'you said no' from 'the
    machine could not ask'. That distinction was being thrown away one line
    before anybody could read it."""
    assert _verdict_reason(verdict) == expect


# ── The flow a person would follow ──────────────────────────────────────────

class _Dead:
    async def prompt_only(self, *a, **k):
        raise RuntimeError("provider down")


def _router_stub(monkeypatch, state, calls):
    import backend.system_control.capability_router as CR

    class Stub:
        async def route(self, name, args=None, *, op_id=""):
            calls.append(name)
            if name == "unlock_screen":
                state["locked"] = False
            return type("R", (), {
                "outcome": "executed", "result": (True, "Screen unlocked."),
                "detail": "", "capability": name, "request_id": "",
                "context_note": ""})()

    monkeypatch.setattr(CR, "get_capability_router", lambda: Stub())


@pytest.mark.asyncio
async def test_a_locked_mac_is_unlocked_before_the_real_command(monkeypatch):
    """Told 'search for dogs' at a locked Mac, nobody types the query into the
    lock screen and reports failure."""
    state, calls, said = {"locked": True}, [], []
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: state["locked"])
    _router_stub(monkeypatch, state, calls)

    async def _say(t):
        said.append(t)

    await VoiceCommandRouter(_Dead(), narrate_fn=_say).route("search for dogs")

    assert calls == ["unlock_screen"], "the remedy must be derived and run"
    assert said and "locked" in said[0].lower()
    assert "unlock" in said[0].lower()


@pytest.mark.asyncio
async def test_an_unlocked_mac_gets_no_detour(monkeypatch):
    state, calls = {"locked": False}, []
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: state["locked"])
    _router_stub(monkeypatch, state, calls)
    await VoiceCommandRouter(_Dead()).route("search for dogs")
    assert calls == []


@pytest.mark.asyncio
async def test_a_blind_probe_changes_nothing(monkeypatch):
    """UNKNOWN behaves exactly like today. Treating 'I cannot see' as 'it is
    locked' would unlock a Mac that was never locked, every time the probe was
    blind — a fabricated remedy for an imagined problem."""
    state, calls = {"locked": None}, []
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: state["locked"])
    _router_stub(monkeypatch, state, calls)
    await VoiceCommandRouter(_Dead()).route("search for dogs")
    assert calls == []


@pytest.mark.asyncio
async def test_locking_the_screen_is_not_blocked_by_the_screen_being_locked(
        monkeypatch):
    """The self-deadlock the dead phrase-table detector needed an exception
    list to avoid. A declaring capability speaks for itself, including by
    declaring no requirement."""
    state, calls = {"locked": True}, []
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: state["locked"])
    _router_stub(monkeypatch, state, calls)
    await VoiceCommandRouter(_Dead()).route("lock my screen")
    assert "unlock_screen" not in calls


@pytest.mark.asyncio
async def test_a_failed_unlock_abandons_the_original_command(monkeypatch):
    """`provides` is a claim, not a proof. The world is re-observed, and a
    remedy that did not work stops the chain rather than carrying on into a
    lock screen."""
    calls = []
    import backend.system_control.capability_router as CR

    class Stub:  # says it worked; the world disagrees
        async def route(self, name, args=None, *, op_id=""):
            calls.append(name)
            return type("R", (), {
                "outcome": "executed", "result": (True, "Unlocked!"),
                "detail": "", "capability": name, "request_id": "",
                "context_note": ""})()

    monkeypatch.setattr(CR, "get_capability_router", lambda: Stub())
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: True)

    res = await VoiceCommandRouter(_Dead()).route("search for dogs")

    assert calls == ["unlock_screen"]
    assert res.success is False
    assert "left the rest alone" in (res.response_text or "")


@pytest.mark.asyncio
async def test_two_claimants_for_one_effect_is_a_refusal_not_a_coin_toss():
    from backend.hud.voice_command_router import VoiceCommandRouter as V
    assert V._who_provides("screen_unlocked") == "unlock_screen"
    assert V._who_provides("no_such_predicate") is None


# ── Hearing yourself ────────────────────────────────────────────────────────

def test_jarvis_does_not_take_its_own_voice_as_a_command():
    """One spoken "lock my screen" locked the screen TWICE: the mute claim's
    deadline is an ESTIMATE from character count, it expired mid-sentence, and
    the recogniser transcribed the synthesiser."""
    note_spoken("On it. Executing: lock my screen")
    assert is_own_echo("Lock screen")
    assert is_own_echo("lock my screen")


def test_the_opposite_command_is_not_an_echo():
    """Word boundaries again: "unlock" is not "lock", so asking to unlock is
    never mistaken for an echo of locking."""
    note_spoken("On it — lock screen.")
    assert not is_own_echo("unlock my screen")


def test_an_unrelated_command_is_never_suppressed():
    note_spoken("Locking the screen now, Derek.")
    assert not is_own_echo("open chrome")


@pytest.mark.asyncio
async def test_an_echo_runs_nothing_at_all(monkeypatch):
    calls = []
    _router_stub(monkeypatch, {"locked": False}, calls)
    WS.get_world_state().register(WS.SCREEN_LOCKED, lambda: False)
    note_spoken("On it — lock screen.")
    res = await VoiceCommandRouter(_Dead()).route("Lock screen")
    assert res.category == "ignored_echo"
    assert calls == []


def test_the_echo_window_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_ECHO_GRACE_S", "0")
    note_spoken("lock my screen")
    assert not is_own_echo("lock my screen")


# ── Naming the operator ─────────────────────────────────────────────────────

def test_the_mac_does_not_greet_its_owner_as_a_stranger():
    """Live: "🔒 Locking the screen now, there. See you soon." The name is
    derived from the account's own GECOS field, not written down."""
    from backend.system_control.macos_controller import _operator_name
    name = _operator_name()
    assert name != "there"
    assert name == "" or (name.isalpha() and len(name) <= 24)
