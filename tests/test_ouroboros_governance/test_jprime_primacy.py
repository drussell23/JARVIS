"""Acceptance tests for Phase 3 Scope α — J-Prime primacy for BACKGROUND/SPECULATIVE.

These tests lock the contract described in ``JARVIS_JPRIME_PRIMACY`` +
:class:`_governance_state.JPrimeState`: when the flag is on and a
:class:`PrimeProvider`-shaped handle is wired, ``BACKGROUND`` and
``SPECULATIVE`` routes dispatch to J-Prime first (under the hoisted
``Semaphore(1)``) and only fall through to DoubleWord when the sem is
saturated or J-Prime fails. ``IMMEDIATE``, ``STANDARD``, and ``COMPLEX``
routes must be untouched — primacy is deliberately scoped to the
cost-optimized cascade so J-Prime's ~8–12s CPU latency cannot regress
the Apr 11 first-token fix.

All test state flows through :mod:`_governance_state` (hoisted sem,
stickiness dict, counters) so there is no reload-hostile state on the
hot ``CandidateGenerator`` instance to begin with — Phase 3 Scope α
only lands if the middle-path hoist rides in the same PR.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import _governance_state
from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator
from backend.core.ouroboros.governance.op_context import GenerationResult


# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


def _deadline(seconds: float = 60.0) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)


def _bg_context(op_id: str = "op-bg-test") -> SimpleNamespace:
    """Minimal BACKGROUND-route context.

    ``CandidateGenerator._generate_background`` only reaches for a
    handful of fields via ``getattr``, so a ``SimpleNamespace`` is
    enough — building a real ``OperationContext`` would drag the
    entire risk engine / routing policy stack into these tests for no
    extra coverage.
    """
    return SimpleNamespace(
        op_id=op_id,
        operation_id=op_id,
        signal_urgency="low",
        signal_source="ai_miner",
        provider_route="background",
        target_files=("backend/foo.py",),
        task_complexity="simple",
        description="miner finding",
    )


def _spec_context(op_id: str = "op-spec-test") -> SimpleNamespace:
    return SimpleNamespace(
        op_id=op_id,
        operation_id=op_id,
        signal_urgency="low",
        signal_source="intent_discovery",
        provider_route="speculative",
        target_files=("backend/foo.py",),
        task_complexity="simple",
        description="intent guess",
    )


class _FakePrimeProvider:
    """Minimal PrimeProvider stand-in for primacy unit tests.

    Exposes exactly the surface ``_try_jprime_primacy`` reads:
    ``provider_name`` (non-empty string to pass the is-wired check) and
    an async ``generate(context, deadline)`` that returns a
    ``GenerationResult``. Default behavior is a happy-path hit with one
    candidate so tests only have to customize the failure cases.
    """

    def __init__(
        self,
        *,
        result: GenerationResult | None = None,
        raise_exc: BaseException | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.provider_name = "gcp-jprime"
        self._result = result or GenerationResult(
            candidates=(
                {
                    "candidate_id": "c-jprime-1",
                    "file_path": "backend/foo.py",
                    "full_content": "# from jprime\n",
                    "rationale": "fake jprime",
                },
            ),
            provider_name="gcp-jprime",
            generation_duration_s=0.2,
        )
        self._raise = raise_exc
        self._delay = delay_s
        self.call_count = 0

    async def generate(
        self,
        context,  # noqa: ANN001  (intentionally loose for test fake)
        deadline: datetime,
    ) -> GenerationResult:
        self.call_count += 1
        if self._delay > 0.0:
            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        return self._result


def _fake_dw() -> MagicMock:
    """DoubleWord stand-in configured to look available and succeed."""
    dw = MagicMock()
    dw.provider_name = "doubleword-397b"
    dw.is_available = True
    dw._realtime_enabled = True
    dw.generate = AsyncMock(
        return_value=GenerationResult(
            candidates=(
                {
                    "candidate_id": "c-dw-1",
                    "file_path": "backend/foo.py",
                    "full_content": "# from dw\n",
                    "rationale": "fake dw",
                },
            ),
            provider_name="doubleword-397b",
            generation_duration_s=0.3,
        )
    )
    return dw


def _build_generator(
    *,
    jprime: _FakePrimeProvider | None,
    dw: MagicMock | None,
) -> CandidateGenerator:
    """Construct a CandidateGenerator with optional J-Prime + DW handles.

    ``primary`` and ``fallback`` are Claude-shaped MagicMocks that
    should never be called from BACKGROUND/SPECULATIVE paths — if any
    test sees a call to them, the route discipline has broken.
    """
    primary = jprime or MagicMock(
        provider_name="unused-primary",
        is_available=False,
    )
    fallback = MagicMock(provider_name="unused-fallback", is_available=False)
    return CandidateGenerator(
        primary=primary,
        fallback=fallback,
        tier0=dw,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJPrimeStateSingleton:
    """Scope α: hoisted state root must behave like 3A/3B."""

    def test_first_call_wins(self) -> None:
        _governance_state.reset_for_tests()
        s1 = _governance_state.get_jprime_state()
        s2 = _governance_state.get_jprime_state()
        assert s1 is s2
        assert s1.jprime_sem is s2.jprime_sem
        assert s1.counters is s2.counters

    def test_sem_is_semaphore_1(self) -> None:
        _governance_state.reset_for_tests()
        state = _governance_state.get_jprime_state()
        assert isinstance(state.jprime_sem, asyncio.Semaphore)
        # Semaphore(1) — one slot available on a fresh state.
        assert not state.jprime_sem.locked()

    def test_model_stickiness_placeholder_present(self) -> None:
        """Scope β/γ will populate; Scope α must ship the container."""
        _governance_state.reset_for_tests()
        state = _governance_state.get_jprime_state()
        assert state.model_stickiness == {}
        # Assignable — so β can land without a state-class migration.
        state.model_stickiness["test-intent"] = "placeholder"
        assert _governance_state.get_jprime_state().model_stickiness[
            "test-intent"
        ] == "placeholder"

    def test_reset_for_tests_clears_singleton(self) -> None:
        _governance_state.reset_for_tests()
        first = _governance_state.get_jprime_state()
        first.counters.jprime_hits = 42
        _governance_state.reset_for_tests()
        second = _governance_state.get_jprime_state()
        assert second is not first
        assert second.counters.jprime_hits == 0

    def test_jprime_primacy_enabled_env_flag(self, monkeypatch) -> None:
        monkeypatch.delenv("JARVIS_JPRIME_PRIMACY", raising=False)
        assert _governance_state.jprime_primacy_enabled() is False
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        assert _governance_state.jprime_primacy_enabled() is True
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "false")
        assert _governance_state.jprime_primacy_enabled() is False


class TestJPrimePrimacyFlagOff:
    """Flag default — BACKGROUND/SPECULATIVE go to DW as before."""

    @pytest.mark.asyncio
    async def test_background_flag_off_skips_jprime(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("JARVIS_JPRIME_PRIMACY", raising=False)
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        result = await gen._generate_background(_bg_context(), _deadline())
        assert result.provider_name == "doubleword-397b"
        assert jprime.call_count == 0
        assert dw.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_speculative_flag_off_skips_jprime(
        self, monkeypatch
    ) -> None:
        """SPECULATIVE with flag off raises ``speculative_deferred``.

        Keeps the pre-Scope-α contract: a deferred op is not a failure
        and not a synchronous result either. J-Prime must not be
        touched.
        """
        monkeypatch.delenv("JARVIS_JPRIME_PRIMACY", raising=False)
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        with pytest.raises(RuntimeError, match="speculative_deferred"):
            await gen._generate_speculative(_spec_context(), _deadline())
        assert jprime.call_count == 0


class TestJPrimePrimacyFlagOn:
    """Flag on — BACKGROUND/SPECULATIVE prefer J-Prime first."""

    @pytest.mark.asyncio
    async def test_background_flag_on_happy_path(self, monkeypatch) -> None:
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        result = await gen._generate_background(_bg_context(), _deadline())
        assert result.provider_name == "gcp-jprime"
        assert jprime.call_count == 1
        assert dw.generate.await_count == 0

        counters = _governance_state.get_jprime_state().counters
        assert counters.jprime_hits == 1
        assert counters.jprime_sem_overflows == 0
        assert counters.jprime_failures == 0
        assert counters.fallthrough_to_dw == 0

    @pytest.mark.asyncio
    async def test_speculative_flag_on_upgrades_to_sync_hit(
        self, monkeypatch
    ) -> None:
        """J-Prime primacy turns deferred SPECULATIVE into a sync hit.

        This is the whole point of routing primacy before the DW
        fire-and-forget branch: a cheap synchronous J-Prime result is
        strictly better than a deferred DW batch the orchestrator has
        to track separately.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        result = await gen._generate_speculative(_spec_context(), _deadline())
        assert result.provider_name == "gcp-jprime"
        assert jprime.call_count == 1
        # DW not touched at all on the primacy hit path.
        assert dw.generate.await_count == 0

    @pytest.mark.asyncio
    async def test_background_jprime_failure_falls_through_to_dw(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider(
            raise_exc=RuntimeError("jprime_generate_boom")
        )
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        result = await gen._generate_background(_bg_context(), _deadline())
        # Fell through — DW serves the result.
        assert result.provider_name == "doubleword-397b"
        assert jprime.call_count == 1
        assert dw.generate.await_count == 1

        counters = _governance_state.get_jprime_state().counters
        assert counters.jprime_hits == 0
        assert counters.jprime_failures == 1
        assert counters.fallthrough_to_dw == 1

    @pytest.mark.asyncio
    async def test_background_jprime_empty_result_falls_through_to_dw(
        self, monkeypatch
    ) -> None:
        """Empty candidate tuple counts as a failure and falls through.

        Regression guard: a happy HTTP response with zero candidates
        used to silently return upstream, and the caller would then see
        a ``GenerationResult`` with no usable work. Primacy must treat
        "no candidates" as a miss and let DW try.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider(
            result=GenerationResult(
                candidates=(),
                provider_name="gcp-jprime",
                generation_duration_s=0.1,
            )
        )
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        result = await gen._generate_background(_bg_context(), _deadline())
        assert result.provider_name == "doubleword-397b"
        counters = _governance_state.get_jprime_state().counters
        assert counters.jprime_failures == 1
        assert counters.fallthrough_to_dw == 1

    @pytest.mark.asyncio
    async def test_background_sem_saturation_falls_through_to_dw(
        self, monkeypatch
    ) -> None:
        """A held sem routes the next op straight to DW with no queuing.

        Scope α explicitly refuses to queue behind a single in-flight
        J-Prime call — the sem overflow path exists so burst background
        workloads hit DW instead of serializing through one slow
        client-side slot.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        # Manually acquire the sem so the next op observes it as locked.
        state = _governance_state.get_jprime_state()
        await state.jprime_sem.acquire()
        try:
            result = await gen._generate_background(
                _bg_context(), _deadline()
            )
            assert result.provider_name == "doubleword-397b"
            # J-Prime never dispatched because the sem short-circuited.
            assert jprime.call_count == 0
            counters = state.counters
            assert counters.jprime_sem_overflows == 1
            assert counters.jprime_hits == 0
            assert counters.fallthrough_to_dw == 1
        finally:
            state.jprime_sem.release()

    @pytest.mark.asyncio
    async def test_background_no_jprime_handle_acts_like_flag_off(
        self, monkeypatch
    ) -> None:
        """Flag on but no PrimeProvider wired → behave as flag off.

        In production this is the usual state during Body integration
        (J-Prime endpoint not yet enabled). The flag must be a no-op
        until a handle shows up, rather than raising or blocking the
        DW-only path.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        dw = _fake_dw()
        gen = _build_generator(jprime=None, dw=dw)

        result = await gen._generate_background(_bg_context(), _deadline())
        assert result.provider_name == "doubleword-397b"
        assert dw.generate.await_count == 1


class TestJPrimePrimacyRouteScope:
    """Primacy must NOT touch IMMEDIATE/STANDARD/COMPLEX routes."""

    @pytest.mark.asyncio
    async def test_primacy_never_called_for_standard_route(
        self, monkeypatch
    ) -> None:
        """Direct unit check on ``_try_jprime_primacy``.

        ``_generate_dispatch`` only calls the primacy helper indirectly
        via the BACKGROUND/SPECULATIVE branches, but the helper itself
        is route-agnostic by design — it's the *caller sites* that
        scope it. This test documents that scoping: a caller that
        passes ``route_label="STANDARD"`` with a fresh state and a
        live J-Prime handle still gets a synchronous hit, proving the
        route scoping lives at the caller, not inside the helper. The
        production code must therefore keep primacy out of the
        IMMEDIATE/STANDARD/COMPLEX dispatch paths.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        gen = _build_generator(jprime=jprime, dw=_fake_dw())

        result = await gen._try_jprime_primacy(
            _bg_context(), _deadline(), route_label="STANDARD-TEST",
        )
        assert result is not None
        assert result.provider_name == "gcp-jprime"

    @pytest.mark.asyncio
    async def test_dispatch_immediate_does_not_touch_jprime_primacy(
        self, monkeypatch
    ) -> None:
        """IMMEDIATE route must hit the Claude-direct path, never primacy.

        Regression guard for the Apr 11 first-token fix — a 60s
        IMMEDIATE cap would be unrecoverable if J-Prime's 8–12s CPU
        latency sat on top of the cascade. We verify by stubbing
        ``_generate_immediate`` and asserting it's the branch the
        dispatcher picks, not the primacy helper.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        jprime = _FakePrimeProvider()
        gen = _build_generator(jprime=jprime, dw=_fake_dw())

        immediate_marker = GenerationResult(
            candidates=(
                {
                    "candidate_id": "c-claude-1",
                    "file_path": "backend/foo.py",
                    "full_content": "# from claude\n",
                    "rationale": "claude direct",
                },
            ),
            provider_name="claude-sonnet",
            generation_duration_s=0.5,
        )
        gen._generate_immediate = AsyncMock(return_value=immediate_marker)  # type: ignore[method-assign]

        ctx = SimpleNamespace(
            op_id="op-imm",
            operation_id="op-imm",
            signal_urgency="critical",
            signal_source="test_failure",
            provider_route="immediate",
            target_files=("backend/foo.py",),
            task_complexity="simple",
            description="urgent",
        )
        result = await gen._generate_dispatch(ctx, _deadline())
        assert result is immediate_marker
        # Primacy helper never touched — J-Prime not called.
        assert jprime.call_count == 0


class TestJPrimeStateReloadSafety:
    """The whole point of the hoist — sem/counters survive reload."""

    @pytest.mark.asyncio
    async def test_sem_identity_preserved_across_generator_rebuilds(
        self, monkeypatch
    ) -> None:
        """Two generators see the SAME sem, counters, and stickiness.

        This is what prevents an ``importlib.reload(candidate_generator)``
        from silently dropping the client-side concurrency limit — even
        if ``CandidateGenerator.__init__`` re-runs, the hoisted
        ``JPrimeState`` singleton keeps the same sem token and counter
        ints across instances.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        gen_a = _build_generator(jprime=_FakePrimeProvider(), dw=_fake_dw())
        gen_b = _build_generator(jprime=_FakePrimeProvider(), dw=_fake_dw())

        assert gen_a._jprime_state is gen_b._jprime_state
        assert gen_a._jprime_sem is gen_b._jprime_sem
        assert gen_a._jprime_counters is gen_b._jprime_counters

    @pytest.mark.asyncio
    async def test_counter_increments_visible_across_generators(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        gen_a = _build_generator(jprime=_FakePrimeProvider(), dw=_fake_dw())
        await gen_a._generate_background(_bg_context("op-a"), _deadline())

        gen_b = _build_generator(jprime=_FakePrimeProvider(), dw=_fake_dw())
        # gen_b sees gen_a's hit reflected in its own counter view
        # because both aliases share the singleton counters object.
        assert gen_b._jprime_counters.jprime_hits == 1


class TestJPrimePrimacyConcurrentOverflow:
    """End-to-end sem contention — concurrent ops should split across J-Prime and DW."""

    @pytest.mark.asyncio
    async def test_two_concurrent_bg_ops_split_across_providers(
        self, monkeypatch
    ) -> None:
        """Fire two BACKGROUND ops in parallel; exactly one hits J-Prime.

        Scenario: two background ops race for the single J-Prime sem
        slot. The first grabs it and starts a slow generate; the second
        observes ``sem.locked()`` and falls through to DW. Exactly one
        J-Prime call, exactly one DW call, both ops get a result.

        This is the production signal that primacy is actually sharing
        load the way the design intends — if both ops went to J-Prime
        we'd have serialized (regressing the Apr 11 fix); if both went
        to DW the sem would be unused.
        """
        monkeypatch.setenv("JARVIS_JPRIME_PRIMACY", "true")
        _governance_state.reset_for_tests()

        # Slow J-Prime fake so the second op is guaranteed to arrive
        # while the sem is held.
        jprime = _FakePrimeProvider(delay_s=0.1)
        dw = _fake_dw()
        gen = _build_generator(jprime=jprime, dw=dw)

        results: List[Tuple[int, str]] = []

        async def _run(op_index: int) -> None:
            r = await gen._generate_background(
                _bg_context(op_id=f"op-{op_index}"), _deadline(),
            )
            results.append((op_index, r.provider_name))

        await asyncio.gather(_run(0), _run(1))

        providers = sorted(name for _, name in results)
        assert providers == ["doubleword-397b", "gcp-jprime"]
        assert jprime.call_count == 1
        assert dw.generate.await_count == 1

        counters = _governance_state.get_jprime_state().counters
        assert counters.jprime_hits == 1
        assert counters.jprime_sem_overflows == 1
        assert counters.fallthrough_to_dw == 1
