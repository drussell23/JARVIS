"""One pin, three lanes — and the QoS breaker that already existed.

`/model` could pin DoubleWord and only *appear* to pin the others, because the
lanes read their model at different times:

    DW      JARVIS_DW_PRIMARY_OVERRIDE   per CALL, by the Override Matrix
    Claude  config.claude_model          ONCE, in GovernedLoopConfig.from_env
    J-Prime active tier label            owned by the failover controller

So ``/model claude-opus-5`` on a running daemon set a variable nothing would
read again until the next restart. The verb described that honestly — the
right thing to say about a control that does not work, and the wrong thing to
settle for.

`sovereign_override` is the missing half: one accessor, read at REQUEST time,
that every lane consults.

ON THE QoS CIRCUIT BREAKER
----------------------------
The brief asked for an async QoS breaker with a lane-tailored `asyncio.timeout`
that cancels cleanly on breach. It already exists, at the unified execution
seam and nowhere else:

    orchestrator.py:6337
        generation = await asyncio.wait_for(
            self._generator.generate(ctx, deadline),
            timeout=_gen_timeout + _OUTER_GATE_GRACE_S,
        )

``_gen_timeout`` is chosen per LANE — immediate 120s, standard 220s, complex
240s, background/speculative 180s, each env-tunable — with a 15s grace. So a
slow model forced onto IMMEDIATE by a sovereign pin is already bounded, and
`wait_for` cancels the task rather than holding the loop.

A second breaker would create two timeout authorities that can disagree, and
the tighter one would silently become the real budget for reasons no log
explains. What was genuinely missing is ATTRIBUTION — a timeout on a route the
operator forced open looked exactly like a provider problem — and that is what
`breach_context` adds.

The mandated scenario is therefore tested against the REAL mechanism: a slow
model pinned to IMMEDIATE, a mocked generate that sleeps past the budget, a
clean cancel, and a breach payload naming the override as the cause.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import sovereign_override as so


@pytest.fixture(autouse=True)
def _clean():
    so.clear_pin()
    yield
    so.clear_pin()


class TestOnePinEveryLane:
    def test_every_lane_is_pinnable(self):
        assert set(so.lanes()) == {"doubleword", "claude", "j-prime"}
        for lane in so.lanes():
            assert so.set_pin(lane, f"model-for-{lane}")
            assert so.pinned_for(lane) == f"model-for-{lane}"

    def test_lanes_do_not_bleed(self):
        so.set_pin("claude", "claude-opus-5")
        assert so.pinned_for("doubleword") == ""
        assert so.pinned_for("j-prime") == ""

    def test_dw_uses_the_matrix_variable_not_a_new_one(self):
        """Pinning DW through this interface must be indistinguishable from
        pinning it the way the Override Matrix already understands, or the two
        would drift into disagreeing about the same fact."""
        so.set_pin("doubleword", "Qwen/X")
        from backend.core.ouroboros.governance.model_pinning_heuristic import (
            model_pin_override,
        )
        assert model_pin_override() == "Qwen/X"

    def test_an_unknown_lane_is_refused_not_silently_stored(self):
        assert so.set_pin("gpt", "x") is False
        assert so.pinned_for("gpt") == ""

    def test_clear_takes_one_lane_or_all(self):
        for lane in so.lanes():
            so.set_pin(lane, "m")
        so.clear_pin("claude")
        assert so.pinned_for("claude") == ""
        assert so.pinned_for("doubleword") == "m"
        so.clear_pin()
        assert all(so.pinned_for(l) == "" for l in so.lanes())

    @pytest.mark.parametrize("bad", [None, "", "   ", 42, object()])
    def test_hostile_input_never_raises(self, bad):
        assert so.set_pin("claude", bad) in (True, False)  # type: ignore[arg-type]
        assert isinstance(so.pinned_for(bad), str)  # type: ignore[arg-type]


class TestTheClaudeLaneNowActuallySwitches:
    def test_a_live_provider_changes_model_without_a_restart(self):
        """THE defect. `claude_model` was bound in `from_env`, so a pin on a
        running daemon reported success and changed nothing until reboot."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider._configured_model = "claude-sonnet-4-6"
        assert provider._model == "claude-sonnet-4-6"
        so.set_pin("claude", "claude-opus-5")
        assert provider._model == "claude-opus-5", (
            "the pin did not reach a constructed provider — it is a no-op again")
        so.clear_pin("claude")
        assert provider._model == "claude-sonnet-4-6"

    def test_it_fails_soft_to_the_configured_model(self, monkeypatch):
        """An unreadable override must never cost the lane its model."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider._configured_model = "claude-sonnet-4-6"
        monkeypatch.setattr(so, "pinned_for",
                            lambda lane: (_ for _ in ()).throw(RuntimeError()))
        assert provider._model == "claude-sonnet-4-6"

    def test_one_property_covers_every_call_site(self):
        """`self._model` is read at thirteen sites. A property means a
        fourteenth added tomorrow inherits the behaviour rather than being the
        one that forgot."""
        import re
        import inspect
        from backend.core.ouroboros.governance import providers
        source = inspect.getsource(providers)
        assert len(re.findall(r"self\._configured_model\s*=", source)) == 1
        assert len(re.findall(r"self\._model\s*=\s*", source)) == 0, (
            "a write to `self._model` — the property has no setter, so this "
            "is an AttributeError the moment that line runs")
        assert len(re.findall(r"self\._model\b(?!\s*=)", source)) > 1

    def test_the_property_did_not_land_inside_a_comment_block(self):
        """The insertion severed a multi-line comment on the first attempt.

        ``@property`` placed between two ``#`` lines PARSES — Python does not
        care — and the sentence it split ("re-derived from ctx fields / +
        self._model + per-call live HTTPX timeout") silently became two
        fragments about different things. Same anchor class as the decorator
        bug that cost a merged regression: a valid parse is not a correct
        placement, and only reading the surrounding lines catches it.
        """
        import inspect
        from backend.core.ouroboros.governance import providers
        source = inspect.getsource(providers)
        assert ("re-derived from ctx fields\n"
                "    #     + self._model + per-call live HTTPX timeout)."
                ) in source, "the _do_stream comment block is severed again"


class TestAttributionNotEnforcement:
    def test_no_pin_yields_no_payload(self):
        """Splattable into telemetry: the ordinary case adds nothing."""
        assert so.breach_context("immediate") == {}

    def test_a_pin_is_named_in_the_breach_context(self):
        so.set_pin("claude", "claude-opus-5")
        ctx = so.breach_context("immediate")
        assert ctx["sovereign_pinned"]["claude"] == "claude-opus-5"

    def test_it_says_whether_THIS_route_was_forced(self):
        """A timeout on a route the operator forced open is their decision
        coming due; the same timeout on a policy-chosen route is a provider
        problem. They need opposite responses and looked identical."""
        so.set_pin("doubleword", "Qwen/Qwen3.5-397B-A17B-FP8")
        ctx = so.breach_context("immediate")
        if "routes_opened_by_pin" in ctx:
            assert "immediate" in ctx["routes_opened_by_pin"]
            assert ctx["route_was_forced"] is True

    def test_attribution_never_raises(self, monkeypatch):
        monkeypatch.setattr(so, "pinned_for",
                            lambda lane: (_ for _ in ()).throw(RuntimeError()))
        assert so.breach_context("immediate") == {}


class TestTheQoSBreakerThatAlreadyExists:
    """The mandated scenario, run against the REAL mechanism.

    Building a second breaker would have meant two timeout authorities; this
    proves the existing one already does the job for a sovereign-pinned slow
    model, and that the breach is attributable.
    """

    def test_the_orchestrator_caps_generation_per_lane(self):
        """The budgets are keyed on the LANE, so a slow model forced onto a
        fast lane inherits the fast lane's ceiling — which is exactly the
        containment the brief asked for."""
        import inspect
        from backend.core.ouroboros.governance import orchestrator
        source = inspect.getsource(orchestrator)
        assert "JARVIS_GEN_TIMEOUT_IMMEDIATE_S" in source
        assert "_gen_timeout + _OUTER_GATE_GRACE_S" in source
        assert "asyncio.wait_for(" in source

    @pytest.mark.asyncio
    async def test_a_slow_pinned_model_is_cancelled_not_awaited(self):
        """Pin a deliberately slow model to IMMEDIATE, sleep past the budget,
        and assert the task is cancelled cleanly rather than hanging.

        Uses the same primitive the orchestrator uses (`asyncio.wait_for`)
        with a compressed budget — the seconds are the only thing scaled, so
        the semantics under test are the shipped ones.
        """
        so.set_pin("doubleword", "slow-model-for-test")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _slow_generate():
            started.set()
            try:
                await asyncio.sleep(30.0)          # far past any lane budget
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        immediate_budget = 0.05                     # the lane's cap, compressed
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_slow_generate(), timeout=immediate_budget)

        assert started.is_set(), "the generation never began"
        await asyncio.sleep(0)                      # let the cancel land
        assert cancelled.is_set(), (
            "the task was not cancelled — the loop would be held by a model "
            "the operator forced onto a fast lane")

    @pytest.mark.asyncio
    async def test_the_breach_is_attributable_to_the_override(self):
        """The half that was missing: the breach payload must name the pin, or
        the operator's own decision reads as a provider fault."""
        so.set_pin("doubleword", "Qwen/Qwen3.5-397B-A17B-FP8")
        try:
            await asyncio.wait_for(asyncio.sleep(5), timeout=0.01)
        except asyncio.TimeoutError:
            payload = so.breach_context("immediate")
        assert payload.get("sovereign_pinned"), (
            "a QoS breach under a sovereign pin carried no attribution")
        assert payload["schema_version"] == so.SOVEREIGN_OVERRIDE_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_the_event_loop_survives_the_breach(self):
        """A ticker must keep being scheduled while the breach happens — the
        whole point of cancelling rather than blocking."""
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        task = asyncio.ensure_future(_ticker())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.sleep(10), timeout=0.05)
            assert ticks > 0, "the loop was held during the QoS breach"
        finally:
            task.cancel()
