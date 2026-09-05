"""L2 repair of a big file regenerates the failing SYMBOLS, not the file.

The 2026-09-05 baseline: L2 asked the local lane for a full-file candidate
of providers.py (521,613 bytes); the model returned 30-32 KB, twice, and
the op died `full_content too short`. A repair path that needs the whole
file re-emitted cannot work for any module that size on a local model.

The GENERATE path already solves this for big files: the swarm
short-circuit resolves target symbols, runs one agent turn per symbol, and
stitches the result back into the original source. L2 never reached it,
because `governed_loop_service` hands L2 the PROVIDER and L2 awaits
`provider.generate` directly. This is the same composition on the L2 path,
built from the same four functions, gated the same way.

Contract pinned here:
  * small file      -> None   (the existing single-shot path, byte-identical)
  * gate off        -> None
  * no agent client -> None
  * resolver fails  -> None
  * intercept drift -> None
  * any exception   -> None   (CancelledError propagates)
  * big + resolved + stitched -> a GenerationResult carrying the STITCHED
    file, tagged so the record shows how it was made
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from backend.core.ouroboros.governance import repair_engine as re_mod
from backend.core.ouroboros.governance import candidate_generator as cg_mod
from backend.core.ouroboros.governance.repair_engine import RepairEngine


BIG = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(3000))   # > big-file gate
SMALL = "def f0():\n    return 0\n"


class _Client:
    _model = "local-test-model"

    async def generate(self, **kw: Any) -> str:
        return "unused"


def _provider(with_client: bool = True):
    p = SimpleNamespace()
    if with_client:
        p._state = SimpleNamespace(client=_Client())
    return p


def _engine(tmp_path, provider=None):
    budget = SimpleNamespace(per_iter_provider_timeout_s=30.0, max_iterations=2,
                             timebox_s=120.0)
    eng = RepairEngine.__new__(RepairEngine)
    eng._budget = budget
    eng._prime = provider if provider is not None else _provider()
    eng._repo_root = str(tmp_path)
    eng._ledger = None
    return eng


def _ctx(tmp_path, source: str):
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    return SimpleNamespace(op_id="op-l2-test-0001", target_files=("mod.py",),
                           description="fix f1", goal_id="")


def _repair_ctx():
    return SimpleNamespace(
        failing_tests=("tests/test_mod.py::test_f1",),
        failure_summary='  File "mod.py", line 4, in f1\n    return 1\nAssertionError',
        current_candidate_content="", current_candidate_file_path="mod.py",
        iteration=1, max_iterations=2, failure_class="test",
        failure_signature_hash="", dependency_cone=(),
    )


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(re_mod.L2_SYMBOL_SCOPED_ENV, "true")


@pytest.fixture
def stubs(monkeypatch):
    """Resolver + interceptor as the GENERATE swarm sees them, controllable."""
    calls: dict = {"resolve": [], "intercept": []}

    def resolve(**kw):
        calls["resolve"].append(kw)
        return SimpleNamespace(resolved=True, symbol_names=("f1",), method="declared",
                               confidence=1.0, primary=("f1",), cluster=())

    async def intercept(source, path, symbols, agent, **kw):
        calls["intercept"].append((path, list(symbols)))
        return SimpleNamespace(content=source.replace("return 1\n", "return 100\n", 1),
                               stitched=True, drifted=False,
                               converged_nodes=list(symbols), rag_recovered_nodes=[])

    monkeypatch.setattr(re_mod, "_swarm_stack", lambda: (
        lambda s, **k: s.count("\n") > 100,          # is_big_file
        resolve, intercept,
        lambda **kw: SimpleNamespace(**kw),           # ProductionAgentTurnFn
        lambda ctx, path: ("f1",),                    # _declared_symbols_for
    ))
    return calls


# ---------------------------------------------------------------------------
# Declines: every one of them is "standard route, byte-identical"
# ---------------------------------------------------------------------------


async def test_small_file_declines_before_touching_anything(tmp_path, on, stubs):
    eng = _engine(tmp_path)
    out = await eng._symbol_scoped_repair(_ctx(tmp_path, SMALL), _repair_ctx())
    assert out is None
    assert stubs["resolve"] == [] and stubs["intercept"] == []


async def test_gate_off_declines(tmp_path, monkeypatch, stubs):
    monkeypatch.delenv(re_mod.L2_SYMBOL_SCOPED_ENV, raising=False)
    eng = _engine(tmp_path)
    assert await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx()) is None
    assert stubs["intercept"] == []


async def test_no_agent_client_declines(tmp_path, on, stubs):
    eng = _engine(tmp_path, provider=_provider(with_client=False))
    assert await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx()) is None
    assert stubs["intercept"] == []


async def test_resolver_fail_closed_declines(tmp_path, on, monkeypatch, stubs):
    big, _res, intercept, agent, decl = re_mod._swarm_stack()
    monkeypatch.setattr(re_mod, "_swarm_stack", lambda: (
        big, lambda **kw: SimpleNamespace(resolved=False, symbol_names=()),
        intercept, agent, decl))
    eng = _engine(tmp_path)
    assert await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx()) is None
    assert stubs["intercept"] == []


async def test_drift_or_no_stitch_declines(tmp_path, on, monkeypatch, stubs):
    big, resolve, _i, agent, decl = re_mod._swarm_stack()

    async def drifted(source, path, symbols, agent_fn, **kw):
        return SimpleNamespace(content=source, stitched=False, drifted=True,
                               converged_nodes=[], rag_recovered_nodes=[])
    monkeypatch.setattr(re_mod, "_swarm_stack", lambda: (big, resolve, drifted, agent, decl))
    eng = _engine(tmp_path)
    assert await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx()) is None


async def test_an_exception_inside_is_a_decline_not_a_failure(tmp_path, on, monkeypatch, stubs):
    big, resolve, _i, agent, decl = re_mod._swarm_stack()

    async def boom(*a, **k):
        raise RuntimeError("swarm wire fell over")
    monkeypatch.setattr(re_mod, "_swarm_stack", lambda: (big, resolve, boom, agent, decl))
    eng = _engine(tmp_path)
    assert await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx()) is None


async def test_cancellation_propagates(tmp_path, on, monkeypatch, stubs):
    big, resolve, _i, agent, decl = re_mod._swarm_stack()

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(re_mod, "_swarm_stack", lambda: (big, resolve, cancelled, agent, decl))
    eng = _engine(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx())


# ---------------------------------------------------------------------------
# The landing
# ---------------------------------------------------------------------------


async def test_big_file_lands_a_stitched_candidate(tmp_path, on, stubs):
    eng = _engine(tmp_path)
    out = await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx())
    assert out is not None
    cand = out.candidates[0]
    assert cand["file_path"] == "mod.py"
    assert "return 100" in cand["full_content"], "the STITCHED file, not the model's"
    assert len(cand["full_content"]) > len(BIG) - 10, "whole file preserved around the graft"
    assert out.provider_name == re_mod.L2_SYMBOL_SCOPED_PROVIDER
    assert "symbol-scoped" in cand["rationale"]


async def test_the_resolver_is_fed_the_failure_frames_and_declared_symbols(tmp_path, on, stubs):
    eng = _engine(tmp_path)
    await eng._symbol_scoped_repair(_ctx(tmp_path, BIG), _repair_ctx())
    kw = stubs["resolve"][0]
    assert kw["file_path"] == "mod.py"
    assert any("mod.py" in f for f in kw["traceback_frames"]), "failure_summary frames reach the resolver"
    assert tuple(kw["declared_symbols"]) == ("f1",)
    assert stubs["intercept"][0] == ("mod.py", ["f1"])


async def test_the_agent_client_is_resolved_the_same_way_as_the_swarm(tmp_path):
    """One resolver for both paths: the GENERATE swarm and L2 must never
    disagree about where the provider's client lives."""
    p = _provider()
    assert cg_mod.agent_client_from([p]) is p._state.client
    assert cg_mod.agent_client_from([_provider(with_client=False)]) is None
    injected = _Client()
    assert cg_mod.agent_client_from([p], injected=injected) is injected


def test_the_generate_swarm_still_uses_the_shared_resolver() -> None:
    import inspect
    src = inspect.getsource(cg_mod.CandidateGenerator._swarm_agent_client)
    assert "agent_client_from(" in src, "the swarm seam must delegate, not duplicate"


def test_l2_wires_the_scoped_path_before_the_single_shot_call() -> None:
    import inspect
    src = inspect.getsource(re_mod.RepairEngine._generate_repair_candidate)
    assert "_symbol_scoped_repair(" in src
    assert src.index("_symbol_scoped_repair(") < src.index("gen_result = await asyncio.wait_for("), (
        "the scoped path must be consulted before the whole-file call")
