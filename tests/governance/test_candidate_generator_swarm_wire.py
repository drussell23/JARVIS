"""candidate_generator ← Agentic Swarm live-seam wire (Step 2a) + Wire #2.

Mandated bulletproof:
  1. The standard small-file route stays 100% byte-identical — the short-circuit
     returns None (flag off, small file, OR resolver fail-closed) so dispatch
     falls through untouched.
  2. A big file with a confident target symbol routes to intercept_full_content
     and returns a swarm GenerationResult that short-circuits generation.
  3. A simulated asyncio.CancelledError mid-execution bubbles up cleanly (no
     swallow, no ghost dispatcher state) — structured concurrency.

Plus: Wire #2's StrategyOutcomeLogger attach is fault-tolerant (a raising bus
never escapes → boot is never poisoned).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.full_content_interceptor as fci
from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator
from backend.core.ouroboros.governance.chunked_generation_bridge import (
    StrategyOutcomeLogger,
)
from backend.core.ouroboros.governance.full_content_interceptor import InterceptResult


class _FakeClient:
    _model = "test-dw-397b"

    async def generate(self, **kwargs):  # never reached when interceptor is mocked
        return SimpleNamespace(content="")


def _gen(tmp_path) -> CandidateGenerator:
    g = CandidateGenerator.__new__(CandidateGenerator)   # bypass heavy __init__
    g._repo_root = str(tmp_path)
    g._client = _FakeClient()
    return g


def _deadline() -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=120)


def _big_source(target_def: str = "def alpha(a, b):\n    return a - b\n") -> str:
    parts = ['"""Enterprise module."""', "", target_def]
    for i in range(400):   # > big_file_line_threshold (300)
        parts.append(f"def _pad_{i}():\n    return {i}\n")
    return "\n".join(parts)


def _ctx(rel_path: str, *, goal: str = "", route: str = "standard") -> SimpleNamespace:
    return SimpleNamespace(
        provider_route=route,
        target_files=(rel_path,),
        description=goal,
        intake_evidence_json="",
        op_id="op-abcdef123456",
    )


# ---------------------------------------------------------------------------
# (1) Standard route stays byte-identical — short-circuit declines.
# ---------------------------------------------------------------------------


async def test_flag_off_is_noop_even_for_big_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_SWARM_ROUTING_ENABLED", raising=False)  # default off
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen(tmp_path)
    out = await g._maybe_swarm_short_circuit(_ctx("big.py", goal="fix alpha"), _deadline())
    assert out is None  # flag off → standard route, byte-identical


async def test_small_file_is_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "small.py"
    p.write_text("def alpha(a, b):\n    return a - b\n")
    g = _gen(tmp_path)
    out = await g._maybe_swarm_short_circuit(_ctx("small.py", goal="fix alpha"), _deadline())
    assert out is None  # small file → standard route


async def test_resolver_fail_closed_is_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen(tmp_path)
    # Ambiguous goal, no traceback → resolver fails closed → standard route.
    out = await g._maybe_swarm_short_circuit(
        _ctx("big.py", goal="please make it better somehow"), _deadline(),
    )
    assert out is None


async def test_background_route_never_swarms(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen(tmp_path)
    out = await g._maybe_swarm_short_circuit(
        _ctx("big.py", goal="fix alpha", route="background"), _deadline(),
    )
    assert out is None


# ---------------------------------------------------------------------------
# (2) Big file + confident symbol → routes to the swarm interceptor.
# ---------------------------------------------------------------------------


async def test_big_file_confident_symbol_routes_to_swarm(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    src = _big_source()
    p = tmp_path / "big.py"
    p.write_text(src)
    g = _gen(tmp_path)

    seen = {}

    async def _fake_intercept(source, file_path, symbols, agent_fn, **kwargs):
        seen["symbols"] = list(symbols)
        seen["file_path"] = file_path
        return InterceptResult(
            strategy="agentic_swarm", content="STITCHED_CONTENT",
            stitched=True, converged_nodes=["alpha"],
        )

    monkeypatch.setattr(fci, "intercept_full_content", _fake_intercept)

    out = await g._maybe_swarm_short_circuit(
        _ctx("big.py", goal="the alpha function returns the wrong value"),
        _deadline(),
    )
    assert out is not None
    assert out.provider_name == "doubleword-agentic-swarm"
    assert len(out.candidates) == 1
    cand = out.candidates[0]
    assert cand["full_content"] == "STITCHED_CONTENT"
    assert cand["file_path"] == "big.py"
    # The resolver fed the confident symbol into the swarm.
    assert "alpha" in seen["symbols"]


async def test_swarm_drift_falls_through_to_standard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen(tmp_path)

    async def _drifted(source, file_path, symbols, agent_fn, **kwargs):
        return InterceptResult(strategy="aborted_drift", content=source,
                               stitched=False, drifted=True)

    monkeypatch.setattr(fci, "intercept_full_content", _drifted)
    out = await g._maybe_swarm_short_circuit(
        _ctx("big.py", goal="fix alpha function"), _deadline(),
    )
    assert out is None  # ghost-edit drift → safe fall-through, no corruption


# ---------------------------------------------------------------------------
# (3) Cancellation propagates cleanly (structured concurrency).
# ---------------------------------------------------------------------------


async def test_cancellation_bubbles_up_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen(tmp_path)

    async def _cancelled(source, file_path, symbols, agent_fn, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(fci, "intercept_full_content", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await g._maybe_swarm_short_circuit(
            _ctx("big.py", goal="fix alpha function"), _deadline(),
        )
    # No dispatcher state was mutated — the helper is stateless, so a clean
    # re-raise leaves nothing to leak (structured-concurrency teardown).


# ---------------------------------------------------------------------------
# Wire #2 — fault-tolerant attach (a raising bus never poisons boot).
# ---------------------------------------------------------------------------


async def test_wire2_attach_is_fault_tolerant() -> None:
    class _BoomBus:
        async def subscribe(self, pattern, handler):
            raise RuntimeError("SQLite locked / bus unavailable")

    logger_obj = StrategyOutcomeLogger(conn=None)
    # attach_to_bus must swallow the fault and return None — never propagate.
    sub_id = await logger_obj.attach_to_bus(_BoomBus())
    assert sub_id is None


# ---------------------------------------------------------------------------
# (3) The agent client is RESOLVED from the provider seat, never assumed.
#
# The wire read ``self._client``; no constructor sets it. The fixture above
# injects it by hand, which is exactly why 205 lines of tests were green
# while the 2026-09-05 devtest baseline could not reach GENERATE once
# (``'CandidateGenerator' object has no attribute '_client'`` on BOTH
# attempts of every op). These pin the production shape.
# ---------------------------------------------------------------------------


class _PrimeProviderShape:
    """``PrimeProvider`` keeps its client in ``_state.client``."""

    def __init__(self, client) -> None:
        self._state = SimpleNamespace(client=client)


def _gen_production_shape(tmp_path, *, jprime=None, primary=None, fallback=None) -> CandidateGenerator:
    g = CandidateGenerator.__new__(CandidateGenerator)
    g._repo_root = str(tmp_path)
    g._jprime = jprime
    g._primary = primary
    g._fallback = fallback
    return g


def test_agent_client_comes_from_the_prime_providers_state(tmp_path) -> None:
    client = _FakeClient()
    g = _gen_production_shape(tmp_path, primary=_PrimeProviderShape(client))
    assert g._swarm_agent_client() is client


def test_agent_client_prefers_jprime_then_primary_then_fallback(tmp_path) -> None:
    a, b, c = _FakeClient(), _FakeClient(), _FakeClient()
    g = _gen_production_shape(
        tmp_path,
        jprime=_PrimeProviderShape(a), primary=_PrimeProviderShape(b),
        fallback=_PrimeProviderShape(c),
    )
    assert g._swarm_agent_client() is a
    g._jprime = None
    assert g._swarm_agent_client() is b
    g._primary = SimpleNamespace()          # a provider with no client at all
    assert g._swarm_agent_client() is c


def test_agent_client_ignores_objects_that_cannot_generate(tmp_path) -> None:
    g = _gen_production_shape(
        tmp_path, primary=_PrimeProviderShape(SimpleNamespace(_model="x")),
    )
    assert g._swarm_agent_client() is None


async def test_no_client_declines_the_swarm_without_raising(tmp_path, monkeypatch) -> None:
    """Big file, confident symbol, flag on -- and no brain: standard route."""
    monkeypatch.setenv("JARVIS_SWARM_ROUTING_ENABLED", "true")
    p = tmp_path / "big.py"
    p.write_text(_big_source())
    g = _gen_production_shape(tmp_path)     # no seats, no injected client

    async def _must_not_run(*a, **k):
        raise AssertionError("interceptor reached without a client")

    monkeypatch.setattr(fci, "intercept_full_content", _must_not_run)
    out = await g._maybe_swarm_short_circuit(
        _ctx("big.py", goal="the alpha function returns the wrong value"), _deadline(),
    )
    assert out is None


async def test_dispatch_seam_turns_a_swarm_fault_into_the_standard_route(tmp_path, monkeypatch) -> None:
    """The contract the docstring promised: any fault inside the short-circuit
    is 'standard route', never a failed generation. CancelledError still
    propagates."""
    g = _gen_production_shape(tmp_path)

    async def _boom(context, deadline):
        raise AttributeError("'CandidateGenerator' object has no attribute '_client'")

    monkeypatch.setattr(g, "_maybe_swarm_short_circuit", _boom)
    assert await g._swarm_or_none(_ctx("big.py"), _deadline()) is None

    async def _cancel(context, deadline):
        raise asyncio.CancelledError()

    monkeypatch.setattr(g, "_maybe_swarm_short_circuit", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await g._swarm_or_none(_ctx("big.py"), _deadline())


def test_dispatch_calls_the_guarded_seam_not_the_raw_short_circuit() -> None:
    """Pin the call site: ``_generate_dispatch`` must go through
    ``_swarm_or_none``. A refactor that calls the raw method again
    re-opens the 2026-09-05 hole."""
    import inspect
    src = inspect.getsource(CandidateGenerator._generate_dispatch)
    assert "_swarm_or_none(" in src
    assert "_maybe_swarm_short_circuit(" not in src
