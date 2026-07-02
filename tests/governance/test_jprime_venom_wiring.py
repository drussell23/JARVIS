"""Venom on the sovereign path -- the 6/6 last boss (bt-iso-1782960801).

Definitive census with every infra layer fixed (node pinned, transport
sovereign, readiness-gated): 19 one-shot streams, 40 Iron Gate rejections,
ZERO tool-loop activity. `_failover_local_dispatch` builds its PrimeProvider
WITHOUT `tool_loop`/`mcp_client`, so `_generate_impl` takes the single-shot
branch (providers.py:5721) -- the 32B cannot execute read_file/search_code and
the exploration-first Iron Gate is STRUCTURALLY unsatisfiable on the local
path (infinite GENERATE_RETRY until the wall).

Proves: the local dispatch passes the primary provider's already-wired
ToolLoopCoordinator (the same one the DW path runs, governed_loop_service
wires it into every provider) into the local PrimeProvider, flipping
generation onto the existing multi-turn Venom branch (providers.py:5555) --
tool advertisements, envelope parser, exploration crediting all downstream.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.providers as prov
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
)


def _deadline():
    return datetime.now(timezone.utc) + timedelta(seconds=120)


class _Prof:
    async def run_calibrated(self, fn):
        return await fn()


def _wire(monkeypatch, gen, captured):
    monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_ENABLED", "false")

    async def _model(ep):
        return "qwen2.5-coder:32b"

    async def _nctx(ep):
        return 8192

    monkeypatch.setattr(gen, "_resolve_dispatch_model_name", _model)
    monkeypatch.setattr(gen, "_negotiate_num_ctx", _nctx)
    monkeypatch.setattr(gen, "_failover_profiler_for", lambda ep, cfg: _Prof())

    class _FakeProvider:
        def __init__(self, client, **kw):
            captured.update(kw)

        async def generate(self, context, deadline):
            return SimpleNamespace(candidates=("patch",), provider_name="gcp-jprime")

    monkeypatch.setattr(prov, "PrimeProvider", _FakeProvider)


async def test_local_dispatch_passes_primary_tool_loop(monkeypatch):
    sentinel_loop = object()
    sentinel_mcp = object()
    fake_primary = SimpleNamespace(
        provider_name="doubleword", _tool_loop=sentinel_loop, _mcp_client=sentinel_mcp,
    )
    gen = CandidateGenerator(primary=fake_primary, jprime=None)
    captured = {}
    _wire(monkeypatch, gen, captured)

    ctx = SimpleNamespace(op_id="op-venom-wire", intake_evidence_json="")
    res = await gen._failover_local_dispatch(ctx, _deadline(), "http://n:11434")

    assert res is not None
    assert captured.get("tool_loop") is sentinel_loop
    assert captured.get("mcp_client") is sentinel_mcp


async def test_local_dispatch_tolerates_loopless_primary(monkeypatch):
    fake_primary = SimpleNamespace(provider_name="claude")   # no _tool_loop attr
    gen = CandidateGenerator(primary=fake_primary, jprime=None)
    captured = {}
    _wire(monkeypatch, gen, captured)

    ctx = SimpleNamespace(op_id="op-venom-none", intake_evidence_json="")
    res = await gen._failover_local_dispatch(ctx, _deadline(), "http://n:11434")

    assert res is not None
    assert captured.get("tool_loop") is None                 # legacy one-shot preserved


async def test_local_dispatch_venom_master_disable(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_VENOM_ENABLED", "false")
    fake_primary = SimpleNamespace(
        provider_name="doubleword", _tool_loop=object(), _mcp_client=object(),
    )
    gen = CandidateGenerator(primary=fake_primary, jprime=None)
    captured = {}
    _wire(monkeypatch, gen, captured)

    ctx = SimpleNamespace(op_id="op-venom-off", intake_evidence_json="")
    res = await gen._failover_local_dispatch(ctx, _deadline(), "http://n:11434")

    assert res is not None
    assert captured.get("tool_loop") is None                 # kill switch honored
