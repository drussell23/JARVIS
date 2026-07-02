"""Time-Dilated Hydration/Dispatch Deadlines -- cure the stale clock.

Live evidence (bt-iso-1782973775): resumed-checkpoint ops entered the Venom
loop with budget=8.5-13.5s (late-cascade dispatches reach the sovereign seam
after DW-exhaust legs consumed the route budget; resurrection paths carry the
window-1 pipeline_deadline verbatim), while fresh ops got ~400s. A committed
sovereign dispatch on a heavy node must derive its runway from the node's OWN
physics: deadline = max(deadline, now + expected_rounds x EWMA_round_estimate),
clamped by the operator's op-envelope (JARVIS_PIPELINE_TIMEOUT_S) -- nothing
hardcoded; the GPU speeding up shrinks the dilation automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.candidate_generator as cg
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
)


def _remaining_s(deadline):
    return (deadline - datetime.now(tz=timezone.utc)).total_seconds()


class TestDilationMath:
    def test_scrap_deadline_is_dilated_to_round_economics(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "600")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 100_000.0)
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=9.0)
        dilated = cg._dilate_sovereign_deadline(scraps, prof, num_ctx=16640)
        # 5 rounds x 100s = 500s runway (< 600 envelope clamp)
        assert 480.0 <= _remaining_s(dilated) <= 510.0

    def test_envelope_clamps_the_dilation(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "10")
        monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "600")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 400_000.0)
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=5.0)
        dilated = cg._dilate_sovereign_deadline(scraps, prof, num_ctx=16640)
        # 10 x 400s = 4000s -> clamped to the operator's 600s op envelope
        assert 580.0 <= _remaining_s(dilated) <= 610.0

    def test_generous_deadline_never_shrinks(self, monkeypatch):
        """Dilation only EXTENDS -- a healthy remaining deadline larger than
        the computed runway is preserved (max, never min)."""
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "2")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 50_000.0)
        generous = datetime.now(tz=timezone.utc) + timedelta(seconds=550.0)
        dilated = cg._dilate_sovereign_deadline(generous, prof, num_ctx=16640)
        assert _remaining_s(dilated) >= 540.0

    def test_fast_gpu_shrinks_dilation(self, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 10_000.0)
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=9.0)
        dilated = cg._dilate_sovereign_deadline(scraps, prof, num_ctx=16640)
        # 5 x 10s = 50s -- a fast node gets a SMALL dilation, not a fixed wall
        assert 40.0 <= _remaining_s(dilated) <= 70.0

    def test_no_profiler_failsoft_identity(self):
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=9.0)
        assert cg._dilate_sovereign_deadline(scraps, None, num_ctx=0) is scraps

    def test_master_disable_is_identity(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIME_DILATION_ENABLED", "false")
        prof = SimpleNamespace(adaptive_timeout_ms=lambda *, prompt_tokens: 100_000.0)
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=9.0)
        assert cg._dilate_sovereign_deadline(scraps, prof, num_ctx=16640) is scraps


class TestDispatchWiring:
    async def test_dispatch_dilates_scrap_deadline(self, monkeypatch):
        """Drive the REAL _failover_local_dispatch with a 9s scrap deadline and
        a stub profiler: the fake provider must receive a DILATED deadline."""
        import backend.core.ouroboros.governance.providers as prov
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_ENABLED", "false")
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "600")

        gen = CandidateGenerator(
            primary=SimpleNamespace(provider_name="doubleword", _tool_loop=None,
                                    _mcp_client=None),
            jprime=None,
        )

        async def _model(ep):
            return "qwen2.5-coder:32b"

        async def _nctx(ep):
            return 16640

        class _Prof:
            def adaptive_timeout_ms(self, *, prompt_tokens):
                return 100_000.0

            async def run_calibrated(self, fn):
                return await fn()

        monkeypatch.setattr(gen, "_resolve_dispatch_model_name", _model)
        monkeypatch.setattr(gen, "_negotiate_num_ctx", _nctx)
        monkeypatch.setattr(gen, "_failover_profiler_for", lambda ep, cfg: _Prof())

        received = {}

        class _FakeProvider:
            def __init__(self, client, **kw):
                pass

            async def generate(self, context, deadline):
                received["deadline"] = deadline
                return SimpleNamespace(candidates=("p",), provider_name="gcp-jprime")

        monkeypatch.setattr(prov, "PrimeProvider", _FakeProvider)

        ctx = SimpleNamespace(op_id="op-dilate", intake_evidence_json="")
        scraps = datetime.now(tz=timezone.utc) + timedelta(seconds=9.0)
        res = await gen._failover_local_dispatch(ctx, scraps, "http://n:11434")

        assert res is not None
        assert _remaining_s(received["deadline"]) >= 400.0   # dilated, not scraps
