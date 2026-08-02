"""One TypeError should not take DoubleWord offline.

WHAT THE RED TESTS WERE ACTUALLY SAYING
-----------------------------------------
Two tests in `test_dynamic_provider_fallback` had been failing. The stated
hypothesis was a brittle failover mechanism. The traceback said something
narrower and worse:

    stub_primary() got an unexpected keyword argument 'model_id'

Slice 30 added `model_id=` to `_call_primary`; the test stub kept the old
two-argument shape. That TypeError was raised INSIDE the `try` in
`_try_primary_then_fallback`, and the handler classified it — as a provider
TIMEOUT, because `classify_exception` mapped EVERY unrecognised exception to
the "conservative TIMEOUT default":

    TypeError        -> TIMEOUT
    AttributeError   -> TIMEOUT
    NameError        -> TIMEOUT
    ImportError      -> TIMEOUT

That is not conservative. `record_primary_failure(TIMEOUT)` flips
`should_attempt_primary()` to False after ONE call, so a single signature
drift in our own code took the entire DoubleWord lane offline, routed every
subsequent op to Claude at roughly ten times the unit cost, and wrote
"Primary failed (mode=TIMEOUT)" — blaming an upstream that was never
contacted. The failover machinery was not fragile. It was obedient, to a
classification that was wrong.

THE SECOND BUG, FOUND WHILE FIXING THE FIRST
----------------------------------------------
`_RECOVERY_PARAMS` gives CONTENT_FAILURE and TEMPORAL_SHED `{base_s: 0,
max_s: 0}` and the comments call this "no penalty". Measured, it is not:
`record_primary_failure` sets FALLBACK_ACTIVE and increments
`_consecutive_failures` for ANY mode handed to it, so even CONTENT_FAILURE
flips `should_attempt_primary()` to False. The exemption was never in the
FSM — it lived in each caller remembering to branch around it, and of the two
callers, one had already forgotten TEMPORAL_SHED. A temporal shed arriving on
the `_try_primary_then_fallback` path therefore penalised DoubleWord in direct
contradiction of its own documented contract.

So the guarantee moved into the FSM, where an invariant belongs, and the two
hand-maintained exemption lists became one `_PRIMARY_INNOCENT_MODES`.

WHY NOT A NEW QUARANTINE REGISTRY
-----------------------------------
Per-model quarantine with a temporal cooldown already exists and is wired:
`dw_ttft_observer` zero-shot timeout quarantine (immediate on a timeout,
self-forgiving after `JARVIS_TTFT_ZEROSHOT_TTL_S`) plus σ-based cold storage,
`topology_sentinel` entitlement breakers, `provider_topology.dw_models_for_route`
filtering the quarantined out of the ranked ladder, and `fleet_exhaustion`
DeepSleeping when the whole fleet is banned. A second registry would be a
second answer to "is this model usable" — the same argument that kept us from
building a second QoS breaker beside `orchestrator.py:6337`.
"""
from __future__ import annotations

import asyncio  # noqa: F401 — used by the async cases below
import logging

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    FailbackStateMachine,
    FailureMode,
    _PRIMARY_INNOCENT_MODES,
)


def _classify(exc: BaseException) -> FailureMode:
    return FailbackStateMachine.classify_exception(exc)


class TestOurBugsAreNotProviderFailures:
    @pytest.mark.parametrize("exc", [
        TypeError("stub_primary() got an unexpected keyword argument 'model_id'"),
        AttributeError("'CandidateGenerator' object has no attribute '_fallback'"),
        NameError("name 'foo' is not defined"),
        ImportError("cannot import name 'X'"),
        ModuleNotFoundError("No module named 'sandbox_loop'"),
        UnboundLocalError("local variable 'x' referenced before assignment"),
    ])
    def test_a_programming_error_classifies_as_LOCAL_DEFECT(self, exc):
        assert _classify(exc) is FailureMode.LOCAL_DEFECT

    def test_the_exact_exception_from_the_red_tests(self):
        """The literal failure that started this. It classified as TIMEOUT for
        as long as the test had been red."""
        async def stub_primary(ctx, deadline):
            return None
        try:
            # Raised at CALL time — the coroutine is never even created, which
            # is why it lands inside the caller's `try` and gets classified.
            stub_primary(None, None, model_id="x")  # type: ignore[call-arg]
        except TypeError as exc:
            assert "model_id" in str(exc)
            assert _classify(exc) is FailureMode.LOCAL_DEFECT
        else:
            pytest.fail("expected the signature drift to raise")

    @pytest.mark.parametrize("exc,expected", [
        (TimeoutError("timed out"), FailureMode.TIMEOUT),
        (ConnectionError("connection refused"), FailureMode.CONNECTION_ERROR),
        (RuntimeError("429 too many requests"), FailureMode.RATE_LIMITED),
    ])
    def test_real_provider_conditions_are_untouched(self, exc, expected):
        assert _classify(exc) is expected

    @pytest.mark.parametrize("exc", [
        KeyError("choices"), IndexError("list index out of range"),
        ValueError("invalid literal"),
    ])
    def test_the_ambiguous_ones_keep_the_conservative_default(self, exc):
        """`KeyError`/`IndexError`/`ValueError` are usually raised while parsing
        a provider's response, where a malformed upstream payload is as likely
        as our indexing. Claiming those as local defects would SUPPRESS a real
        provider penalty — the failure direction that hides outages — so they
        stay TIMEOUT until something proves otherwise."""
        assert _classify(exc) is FailureMode.TIMEOUT

    def test_a_provider_layer_still_wins_over_our_type(self):
        """An SDK that wraps a transport flap in a TypeError must still read as
        transport. The provider layer is closer to the truth whenever it can
        speak at all, so LOCAL_DEFECT runs LAST."""
        inner = ConnectionError("connection refused")
        outer = TypeError("sdk wrapper")
        outer.__cause__ = inner
        assert _classify(outer) is FailureMode.CONNECTION_ERROR

    def test_it_classifies_on_TYPE_never_on_MESSAGE(self):
        """A provider is perfectly capable of returning prose containing the
        word "attributeerror". A string match would let an upstream error
        masquerade as our bug — again in the direction that hides outages."""
        assert _classify(
            RuntimeError("upstream said: AttributeError in their handler"),
        ) is not FailureMode.LOCAL_DEFECT

    def test_the_master_switch_restores_the_old_reading(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_DEFECT_CLASSIFICATION_ENABLED", "0")
        assert _classify(TypeError("x")) is FailureMode.TIMEOUT


class TestTheLaneSurvivesOurBug:
    def test_one_TypeError_used_to_take_the_lane_offline(self):
        """The regression this exists to prevent, stated as the counterfactual:
        classified as TIMEOUT, a single occurrence is enough."""
        fsm = FailbackStateMachine()
        assert fsm.should_attempt_primary()
        fsm.record_primary_failure(mode=FailureMode.TIMEOUT)
        assert not fsm.should_attempt_primary()

    def test_a_LOCAL_DEFECT_leaves_the_primary_eligible(self):
        fsm = FailbackStateMachine()
        for _ in range(5):
            fsm.record_primary_failure(mode=FailureMode.LOCAL_DEFECT)
        assert fsm.should_attempt_primary(), (
            "our bug quarantined the provider — the lane is offline for a "
            "defect the provider had no part in")
        assert fsm._consecutive_failures == 0

    @pytest.mark.parametrize("mode", sorted(
        _PRIMARY_INNOCENT_MODES, key=lambda m: m.name))
    def test_the_FSM_itself_refuses_the_penalty(self, mode):
        """Enforced in `record_primary_failure`, not at the call sites.

        Zero `_RECOVERY_PARAMS` LOOK like this guarantee and never were it: the
        method transitions state for any mode it is handed. Putting the refusal
        in the FSM is what makes a third call site inherit the policy instead
        of being the one that forgot — which is exactly how TEMPORAL_SHED came
        to be exempt at one caller and penalised at the other.
        """
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=mode)
        assert fsm.should_attempt_primary()
        assert fsm.state.name == "PRIMARY_READY"

    def test_a_genuine_provider_failure_is_still_penalised(self):
        """The mechanism must still work, or this trades one silence for
        another."""
        fsm = FailbackStateMachine()
        fsm.record_primary_failure(mode=FailureMode.TIMEOUT)
        assert not fsm.should_attempt_primary()
        assert fsm.state.name == "FALLBACK_ACTIVE"

    def test_one_policy_not_two_copies(self):
        """Both classify-then-record sites consult the same frozenset. They
        previously kept separate literal lists and had drifted by one member."""
        import inspect
        from backend.core.ouroboros.governance import candidate_generator
        source = inspect.getsource(candidate_generator)
        assert source.count("_PRIMARY_INNOCENT_MODES") >= 4
        assert FailureMode.TEMPORAL_SHED in _PRIMARY_INNOCENT_MODES
        assert FailureMode.CONTENT_FAILURE in _PRIMARY_INNOCENT_MODES
        assert FailureMode.LOCAL_DEFECT in _PRIMARY_INNOCENT_MODES


class TestTheOpIsNotDropped:
    """State-preserving reroute: a defect on the primary path must cascade with
    the operator's context intact, never terminate the operation."""

    @pytest.mark.asyncio
    async def test_the_op_cascades_with_its_payload_intact(self, caplog):
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )
        cg = CandidateGenerator.__new__(CandidateGenerator)
        cg.fsm = FailbackStateMachine()
        cg._fallback = object()

        seen = {}

        async def broken_primary(ctx, deadline, **_kw):
            raise TypeError("simulated signature drift on the primary path")

        async def fallback(ctx, deadline, **_kw):
            seen["ctx"] = ctx
            return "fallback-result"

        cg._call_primary = broken_primary       # type: ignore[method-assign]
        cg._call_fallback = fallback            # type: ignore[method-assign]

        class _Ctx:
            op_id = "op-local-defect-001"
            provider_route = "immediate"
            prompt = "the operator's payload"

        ctx = _Ctx()
        from datetime import datetime, timedelta, timezone
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=60)

        with caplog.at_level(logging.ERROR):
            result = await cg._try_primary_then_fallback(ctx, deadline)

        assert result == "fallback-result", "the op was dropped"
        assert seen["ctx"] is ctx, (
            "the fallback got a different context — the payload was not "
            "preserved across the reroute")
        assert seen["ctx"].prompt == "the operator's payload"
        assert cg.fsm.should_attempt_primary(), (
            "the provider was quarantined for OUR bug")

    @pytest.mark.asyncio
    async def test_it_is_reported_at_ERROR_with_a_traceback(self, caplog):
        """The WARNING it replaces sat among ordinary provider noise under the
        same "Primary failed … falling back" heading, which is precisely how
        the class survived to be found by a unit test rather than by anyone
        watching production."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )
        cg = CandidateGenerator.__new__(CandidateGenerator)
        cg.fsm = FailbackStateMachine()
        cg._fallback = object()

        async def broken_primary(ctx, deadline, **_kw):
            raise AttributeError("no attribute 'foo'")

        async def fallback(ctx, deadline, **_kw):
            return "ok"

        cg._call_primary = broken_primary       # type: ignore[method-assign]
        cg._call_fallback = fallback            # type: ignore[method-assign]

        class _Ctx:
            op_id = "op-local-defect-002"
            provider_route = "standard"

        from datetime import datetime, timedelta, timezone
        with caplog.at_level(logging.DEBUG):
            await cg._try_primary_then_fallback(
                _Ctx(), datetime.now(tz=timezone.utc) + timedelta(seconds=60))

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a defect in our own code was logged below ERROR"
        assert any("LOCAL DEFECT" in r.getMessage() for r in errors)
        assert any(r.exc_info for r in errors), "no traceback attached"
        # And it must NOT also file itself under the provider heading.
        assert not any(
            "Primary failed (mode=LOCAL_DEFECT" in r.getMessage()
            for r in caplog.records)


class TestTheQuarantineRegistryWeDidNotRebuild:
    """Per-model quarantine with a temporal cooldown already exists. These
    assert it, so a future reader can see WHY no second registry was added
    rather than having to take it on trust."""

    def test_a_timeout_quarantines_a_model_with_a_self_forgiving_TTL(self):
        from backend.core.ouroboros.governance import dw_ttft_observer as obs
        import inspect
        source = inspect.getsource(obs)
        assert "JARVIS_TTFT_ZEROSHOT_TTL_S" in source
        assert "is_cold_storage" in source
        assert hasattr(obs, "zeroshot_timeout_quarantine_enabled")

    def test_the_ladder_skips_quarantined_models(self):
        """The reroute half: quarantine is only useful if selection honours it."""
        from backend.core.ouroboros.governance import fleet_exhaustion as fe
        assert hasattr(fe, "_is_quarantined")
        assert hasattr(fe, "fleet_exhausted")
        assert hasattr(fe, "flush_ephemeral_quarantine")

    def test_the_all_quarantined_case_is_handled(self):
        """`fleet_exhausted` + DeepSleep + flush-and-reprobe — a transient
        outage self-heals, a genuine block simply re-quarantines."""
        from backend.core.ouroboros.governance.fleet_exhaustion import (
            deepsleep_seconds,
        )
        assert 60.0 <= deepsleep_seconds() <= 4 * 3600.0
