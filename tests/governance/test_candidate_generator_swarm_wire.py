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
