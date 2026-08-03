"""Gate the capability without holding the generation loop open.

The mandated scenario is `test_lock_screen_suspends_then_denies_cleanly`: a
mocked LLM tool call for `lock_screen`, asserting the router yields SUSPENDED
and unblocks the async task, then a mocked DENIED response arriving out of band,
asserting `OPERATOR_DENIED_EXECUTION` reaches the context without a fatal error.

THE DEADLOCK THIS SUITE DEFENDS
---------------------------------
`ApprovalProvider.await_decision` is documented as "block until a decision is
made or timeout expires". Awaiting it inside a tool call holds the LLM's task
open for as long as a human takes to read a prompt — outliving the provider
timeout, the route's generation budget (120s on IMMEDIATE), and the operator's
patience, in that order. The turn then dies of a timeout that had nothing to do
with the model.

`test_the_generation_task_is_never_held_open` is the one to keep: it runs the
route against a provider whose `await_decision` sleeps for an hour and asserts
the call returns in milliseconds. If someone "simplifies" the suspension into a
wait, that test is the only thing that notices.

AND WHY A DENIAL IS A RESULT
------------------------------
Raising on denial makes the model see an error, and a model that sees an error
retries — usually with a reworded version of the same call. The refusal has to
enter the context as a fact to be reasoned about, which is why
`OPERATOR_DENIED_EXECUTION` is returned rather than thrown.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import pytest

from backend.system_control import capability_router as crt
from backend.system_control.capability_registry import (
    CapabilityRegistry,
    capability,
)
from backend.system_control.capability_router import (
    DENIED_PAYLOAD,
    CapabilityRouter,
    Outcome,
)


class _MockController:
    """Two capabilities: one gated, one free."""

    def __init__(self) -> None:
        self.locked = 0
        self.reads = 0

    @capability(mutates=True)
    async def lock_screen(self, enable_voice_feedback: bool = True) -> tuple:
        """Lock the macOS screen.

        Args:
            enable_voice_feedback: Narrate the action
        """
        self.locked += 1
        return (True, "locked")

    @capability(reads_only=True)
    async def get_battery(self) -> dict:
        """Battery percentage.

        Capability: read-only
        """
        self.reads += 1
        return {"percent": 88}


class _Provider:
    """Stands in for `ApprovalProvider`. `await_decision` is deliberately slow."""

    def __init__(self) -> None:
        self.requested: list = []

    async def request(self, context: Any) -> str:
        self.requested.append(context)
        return f"req-{len(self.requested)}"

    async def await_decision(self, request_id: str, timeout_s: float):
        # An hour. Any implementation that awaits this inside a turn is broken,
        # and the timing assertion below is what proves nobody does.
        await asyncio.sleep(3600)
        raise AssertionError("await_decision was called on the generation path")


class _Decision:
    """Shaped like `ApprovalResult`.

    Carries a `nonce` because a verdict without one is no longer an answer:
    the challenge landed after these tests were written, and they were updated
    to satisfy it rather than the check being relaxed to satisfy them.
    """

    def __init__(self, name: str, nonce: str = "") -> None:
        self.status = type("S", (), {"name": name})()
        self.nonce = nonce


def _router(controller: Optional[_MockController] = None) -> tuple:
    ctl = controller or _MockController()
    reg = CapabilityRegistry(ctl).hydrate()
    prov = _Provider()
    return CapabilityRouter(registry=reg, provider=prov, target=ctl), ctl, prov


class TestTheMandatedScenario:
    @pytest.mark.asyncio
    async def test_lock_screen_suspends_then_denies_cleanly(self):
        """THE scenario: suspend, unblock, deny out of band, no fatal error."""
        router, ctl, prov = _router()

        out = await router.route("lock_screen", {"enable_voice_feedback": False})

        # 1. Suspended, and the machine was NOT touched.
        assert out.outcome == Outcome.SUSPENDED.value
        assert out.suspended is True
        assert ctl.locked == 0, "the capability ran before consent"
        assert out.request_id, "no consent request id to resume with"
        assert out.context_note == "[SYSTEM: Awaiting operator consent]"
        assert prov.requested, "the operator was never asked"

        # A suspended turn must NOT re-trigger — it has nothing to say yet.
        assert out.should_retrigger is False

        # 2. The operator declines, out of band.
        resumed = await router.resume(out.request_id, _Decision("REJECTED"))

        assert resumed.outcome == Outcome.DENIED.value
        assert resumed.context_note == DENIED_PAYLOAD
        assert ctl.locked == 0, "a denied capability executed anyway"
        # 3. And the agent gets another turn to acknowledge it.
        assert resumed.should_retrigger is True

    @pytest.mark.asyncio
    async def test_the_generation_task_is_never_held_open(self):
        """THE one to keep.

        The provider's `await_decision` sleeps an hour. If the router ever awaits
        it on the tool path, this test hangs instead of passing — which is the
        only signal that would catch the regression.
        """
        router, _ctl, _prov = _router()
        t0 = time.monotonic()
        out = await asyncio.wait_for(router.route("lock_screen"), timeout=2.0)
        assert time.monotonic() - t0 < 1.0, "the turn was held open"
        assert out.suspended

    @pytest.mark.asyncio
    async def test_approval_executes_and_re_triggers(self):
        router, ctl, _prov = _router()
        out = await router.route("lock_screen")
        resumed = await router.resume(out.request_id,
                                      _Decision("APPROVED", out.nonce))
        assert resumed.outcome == Outcome.EXECUTED.value
        assert ctl.locked == 1
        assert resumed.should_retrigger is True
        assert "locked" in resumed.context_note

    @pytest.mark.asyncio
    async def test_a_read_only_capability_never_suspends(self):
        """The gate is for mutation. A reader must not cost the operator a
        prompt — that is how a consent dialog becomes noise people dismiss."""
        router, ctl, prov = _router()
        out = await router.route("get_battery")
        assert out.outcome == Outcome.EXECUTED.value
        assert ctl.reads == 1
        assert prov.requested == [], "consent was requested for a read"


class TestFailingClosed:
    @pytest.mark.asyncio
    async def test_no_provider_denies_rather_than_proceeds(self):
        """A gated capability with no way to ask is not permission to proceed."""
        ctl = _MockController()
        router = CapabilityRouter(registry=CapabilityRegistry(ctl).hydrate(),
                                  provider=None, target=ctl)
        out = await router.route("lock_screen")
        assert out.outcome == Outcome.DENIED.value
        assert out.context_note == DENIED_PAYLOAD
        assert ctl.locked == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("decision", [
        None, "maybe", object(), _Decision("PENDING"), _Decision("EXPIRED"),
        _Decision("SUPERSEDED"),
    ])
    async def test_anything_that_is_not_approval_is_denial(self, decision):
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        resumed = await router.resume(out.request_id, decision)
        assert resumed.outcome == Outcome.DENIED.value
        assert ctl.locked == 0

    @pytest.mark.asyncio
    async def test_an_unknown_capability_is_reported_not_executed(self):
        router, _ctl, _p = _router()
        out = await router.route("format_the_disk")
        assert out.outcome == Outcome.UNKNOWN_CAPABILITY.value
        assert out.should_retrigger is True

    @pytest.mark.asyncio
    async def test_an_expired_consent_does_not_execute(self, monkeypatch):
        """An operator answering tomorrow must not run a command whose world
        has moved on."""
        monkeypatch.setenv("JARVIS_CAPABILITY_CONSENT_TTL_S", "30")
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        parked = router._parked[out.request_id]
        parked.created = time.time() - 10_000
        resumed = await router.resume(out.request_id, _Decision("APPROVED"))
        assert resumed.outcome == Outcome.EXPIRED.value
        assert ctl.locked == 0
        assert "TIMED_OUT" in resumed.context_note

    @pytest.mark.asyncio
    async def test_resuming_an_unknown_id_never_raises(self):
        router, _c, _p = _router()
        out = await router.resume("no-such-request", _Decision("APPROVED"))
        assert out.outcome == Outcome.FAILED.value

    @pytest.mark.asyncio
    async def test_a_consent_cannot_be_replayed(self):
        """Popping on resume: an approval is spent once, so a replayed message
        cannot execute a mutation twice."""
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        await router.resume(out.request_id, _Decision("APPROVED", out.nonce))
        again = await router.resume(out.request_id,
                                    _Decision("APPROVED", out.nonce))
        assert ctl.locked == 1
        assert again.outcome == Outcome.FAILED.value


class TestEveryOutcomeSpeaksToTheModel:
    @pytest.mark.asyncio
    async def test_a_failing_capability_returns_rather_than_raises(self):
        class _Boom:
            @capability(reads_only=True)
            async def get_thing(self) -> str:
                """Fetch.

                Capability: read-only
                """
                raise RuntimeError("kernel said no")
        ctl = _Boom()
        router = CapabilityRouter(registry=CapabilityRegistry(ctl).hydrate(),
                                  provider=_Provider(), target=ctl)
        out = await router.route("get_thing")
        assert out.outcome == Outcome.FAILED.value
        assert "TOOL_FAILED" in out.context_note
        assert out.should_retrigger is True

    @pytest.mark.asyncio
    async def test_every_outcome_carries_a_context_note(self):
        """A turn that ends without telling the model why is a turn the model
        will simply repeat."""
        router, _c, _p = _router()
        for call in ("lock_screen", "get_battery", "nonexistent"):
            out = await router.route(call)
            assert out.context_note, f"{call} said nothing to the model"

    @pytest.mark.asyncio
    async def test_the_denied_token_is_stable_and_greppable(self):
        """The agent must recognise this exact condition across turns; a
        reworded sentence would read as a new failure."""
        assert DENIED_PAYLOAD == "[SYSTEM: OPERATOR_DENIED_EXECUTION]"

    @pytest.mark.asyncio
    async def test_stats_expose_the_boundary(self):
        router, _c, _p = _router()
        out = await router.route("lock_screen")
        assert router.stats()["suspended"] == 1
        assert router.stats()["pending"] == 1
        assert out.request_id in router.pending()
        await router.resume(out.request_id, _Decision("REJECTED"))
        assert router.stats()["denied"] == 1
        assert router.stats()["pending"] == 0

    @pytest.mark.asyncio
    async def test_the_master_switch_bypasses_the_gate(self, monkeypatch):
        """Off means the legacy path: execute directly, no consent surface."""
        monkeypatch.setenv("JARVIS_CAPABILITY_ROUTER_ENABLED", "0")
        router, ctl, prov = _router()
        out = await router.route("lock_screen")
        assert out.outcome == Outcome.EXECUTED.value
        assert ctl.locked == 1 and prov.requested == []


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_many_suspensions_do_not_block_each_other(self):
        """Ten gated calls must all return; a shared lock would serialise them
        behind the first operator prompt."""
        router, ctl, _p = _router()
        outs = await asyncio.wait_for(
            asyncio.gather(*(router.route("lock_screen") for _ in range(10))),
            timeout=3.0)
        assert all(o.suspended for o in outs)
        assert len({o.request_id for o in outs}) == 10
        assert ctl.locked == 0


class TestTheNonceChallenge:
    """A verdict is only an answer to THIS question if it carries THIS nonce.

    The signed-bundle boundary stops an unsigned process from speaking for the
    Secure Enclave. It does nothing about REPLAY: anything that can write to
    the local IPC socket could capture one approval and resend it forever,
    defeating the boundary one layer below it. The nonce closes that.
    """

    class _Verdict:
        def __init__(self, name: str, nonce: str = "") -> None:
            self.status = type("S", (), {"name": name})()
            self.nonce = nonce

    @pytest.mark.asyncio
    async def test_a_suspension_mints_a_challenge(self):
        router, _c, _p = _router()
        out = await router.route("lock_screen")
        assert out.nonce, "no challenge was issued with the suspension"
        assert len(out.nonce) >= 32, "challenge is too short to resist guessing"

    @pytest.mark.asyncio
    async def test_the_right_nonce_is_accepted(self):
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        res = await router.resume(out.request_id,
                                  self._Verdict("APPROVED", out.nonce))
        assert res.outcome == Outcome.EXECUTED.value
        assert ctl.locked == 1

    @pytest.mark.asyncio
    async def test_a_replayed_verdict_from_another_request_is_denied(self):
        """THE attack. Capture an approval for one call, replay it at the next."""
        router, ctl, _p = _router()
        first = await router.route("lock_screen")
        await router.resume(first.request_id,
                            self._Verdict("APPROVED", first.nonce))
        assert ctl.locked == 1

        second = await router.route("lock_screen")
        replayed = await router.resume(second.request_id,
                                       self._Verdict("APPROVED", first.nonce))
        assert replayed.outcome == Outcome.DENIED.value
        assert ctl.locked == 1, "a replayed approval executed a second lock"
        assert "nonce" in replayed.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("nonce", ["", None, "wrong", "x" * 43])
    async def test_a_missing_or_wrong_nonce_fails_closed(self, nonce):
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        res = await router.resume(out.request_id,
                                  self._Verdict("APPROVED", nonce))
        assert res.outcome == Outcome.DENIED.value
        assert ctl.locked == 0

    @pytest.mark.asyncio
    async def test_a_dict_verdict_works_too(self):
        """The IPC delivers JSON, so the verdict arrives as a dict — the
        surfaces that answer should not have to learn a Python class."""
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        res = await router.resume(out.request_id,
                                  {"status": "APPROVED", "nonce": out.nonce})
        assert res.outcome == Outcome.EXECUTED.value and ctl.locked == 1

    @pytest.mark.asyncio
    async def test_the_challenge_is_single_use(self):
        """Consent is popped on resume, so even the CORRECT nonce cannot run
        the capability twice."""
        router, ctl, _p = _router()
        out = await router.route("lock_screen")
        await router.resume(out.request_id,
                            self._Verdict("APPROVED", out.nonce))
        again = await router.resume(out.request_id,
                                    self._Verdict("APPROVED", out.nonce))
        assert ctl.locked == 1
        assert again.outcome == Outcome.FAILED.value

    @pytest.mark.asyncio
    async def test_two_suspensions_get_different_challenges(self):
        router, _c, _p = _router()
        a = await router.route("lock_screen")
        b = await router.route("lock_screen")
        assert a.nonce != b.nonce

    def test_comparison_is_constant_time(self):
        """A verdict over a local socket is attacker-influenced input; an
        early-exit `==` leaks the prefix one byte at a time to anything that
        can time it."""
        import inspect
        from backend.system_control import capability_router as m
        assert "compare_digest" in inspect.getsource(m._verify_nonce)

    def test_the_challenge_uses_a_csprng(self):
        """`uuid4` guarantees uniqueness; an anti-replay token needs
        UNPREDICTABILITY."""
        import inspect
        from backend.system_control import capability_router as m
        assert "secrets" in inspect.getsource(m._mint_nonce)
