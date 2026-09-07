"""Dynamic Radius & Stitch — a hundred-thousand-line file never reaches the model whole.

Pins: the fail-closed recursive shrinker (radius → no imports → node →
compressed node → decline), budget-aware strategy selection that degrades to
RAG and never to whole-file, the map-reduce node prompt that carries the
framing and a READ-ONLY radius, the L2 seam fracture routed into LessonMemory
without touching the swarm, and the local-lane wiring in CandidateGenerator.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import context_budget as cb
from backend.core.ouroboros.governance import intelligent_chunking as ic
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import extract_target_chunk

REPO = Path(__file__).resolve().parents[2]

BIG = (
    "import os\nimport sys\nfrom typing import Any\n\n\n"
    + "\n\n".join(f"def filler_{i}(x):\n    return x + {i}\n" for i in range(40))
    + "\n\nclass Engine:\n    \"\"\"An engine.\"\"\"\n\n"
    + "    def other(self):\n        return 1\n\n"
    + "    def target(self, a, b):\n        \"\"\"Add.\"\"\"\n"
    + "".join(f"        v{i} = a + b + {i}\n" for i in range(60))
    + "        return v59\n"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("JARVIS_DW_BIG_FILE_LINE_THRESHOLD", "JARVIS_DW_MAX_CONTEXT_TOKENS", "JARVIS_CONTEXT_INGEST_CEILING_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    cb.reset_cache(); yield; cb.reset_cache()


# --------------------------------------------------------------------------
# the shrinker
# --------------------------------------------------------------------------

def test_full_radius_when_it_fits():
    r = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", 100_000)
    assert r is not None and r.level == "radius"
    assert "import os" in r.context and "class Engine" in r.context and "def target" in r.context
    assert "filler_7" not in r.context, "siblings are pruned"


def test_imports_are_the_first_thing_dropped():
    full = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", 100_000)
    r = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", full.tokens - 1)
    assert r is not None and r.level == "radius_no_imports"
    assert "import os" not in r.context and "class Engine" in r.context and "def target" in r.context


def test_then_the_class_shell_then_the_node_is_compressed():
    node = extract_target_chunk(BIG, "engine.py", "Engine.target").source_code
    node_tokens = cb.estimate_tokens(node)
    r = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", node_tokens)
    assert r is not None and r.level == "node" and "class Engine" not in r.context
    r = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", node_tokens // 2)
    assert r is not None and r.level == "node_compressed" and r.tokens <= node_tokens // 2
    assert "def target" in r.context


def test_fail_closed_when_nothing_fits():
    assert ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", 1) is None
    assert ic.shrink_radius_to_budget(BIG, "engine.py", "no_such_symbol", 10_000) is None
    assert ic.shrink_radius_to_budget("def broken(:\n", "b.py", "broken", 10_000) is None


def test_every_level_respects_the_budget():
    for budget in (100_000, 400, 200, 120, 60):
        r = ic.shrink_radius_to_budget(BIG, "engine.py", "Engine.target", budget)
        if r is not None:
            assert r.tokens <= budget and r.level in ic.SHRINK_LEVELS


# --------------------------------------------------------------------------
# strategy selection is budget-aware and never whole-file above the ceiling
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_over_ceiling_selects_a_shrunk_radius_not_the_file():
    async def _n(_e):
        return 800                                     # tiny window: ceiling ~530 tokens
    await cb.prime_budget("ep", _n)
    plan = ic.select_extraction_strategy(BIG, "engine.py", "Engine.target")
    assert plan.strategy == "ast" and plan.forbade_whole_file
    assert cb.estimate_tokens(plan.context) <= cb.ingest_ceiling_tokens()
    assert "filler_3" not in plan.context


@pytest.mark.asyncio
async def test_a_node_that_cannot_fit_degrades_to_rag_never_whole():
    async def _n(_e):
        return 800
    await cb.prime_budget("ep", _n)
    plan = ic.select_extraction_strategy(BIG, "engine.py", "Engine.target", node_budget_tokens=1)
    assert plan.strategy == "rag" and plan.forbade_whole_file and plan.context != BIG


def test_under_ceiling_stays_whole():
    plan = ic.select_extraction_strategy("def f():\n    return 1\n", "f.py", "f")
    assert plan.strategy == "whole"


# --------------------------------------------------------------------------
# the node prompt: framing + READ-ONLY radius, never the file
# --------------------------------------------------------------------------

def test_node_prompt_carries_framing_and_radius_but_not_the_file():
    from backend.core.ouroboros.governance.agent_turn_adapter import ProductionAgentTurnFn
    from backend.core.ouroboros.governance.chunked_generation_bridge import MAP_REDUCE_FRAMING
    chunk = extract_target_chunk(BIG, "engine.py", "Engine.target")
    target = ChunkTarget(symbol="Engine.target", chunk=chunk, instruction="make it add")
    seen = []

    def _ctx(t):
        seen.append(t.symbol)
        return ic.shrink_radius_to_budget(BIG, "engine.py", t.symbol, 100_000).context

    fn = ProductionAgentTurnFn(client=None, tool_backend=None, framing=MAP_REDUCE_FRAMING, node_context_fn=_ctx)
    prompt = fn._node_prompt(target, "")
    assert prompt.startswith(MAP_REDUCE_FRAMING)
    assert "Read-only surrounding context" in prompt and "class Engine" in prompt
    assert "filler_5" not in prompt, "the whole file never enters the prompt"
    assert seen == ["Engine.target"]
    assert "Return ONLY the complete" in prompt


def test_node_prompt_without_context_fn_is_unchanged_in_shape():
    from backend.core.ouroboros.governance.agent_turn_adapter import ProductionAgentTurnFn
    chunk = extract_target_chunk(BIG, "engine.py", "Engine.target")
    target = ChunkTarget(symbol="Engine.target", chunk=chunk, instruction="x")
    fn = ProductionAgentTurnFn(client=None, tool_backend=None)
    p = fn._node_prompt(target, "")
    assert "Read-only surrounding context" not in p and p.startswith("You are repairing exactly ONE function")

    def _boom(_t):
        raise RuntimeError("no context today")

    fn2 = ProductionAgentTurnFn(client=None, tool_backend=None, node_context_fn=_boom)
    assert "You are repairing exactly ONE function" in fn2._node_prompt(target, "")


# --------------------------------------------------------------------------
# L2 seam fracture → LessonMemory, swarm untouched
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seam_fracture_and_unconverged_become_lessons(monkeypatch):
    from backend.core.ouroboros.governance import agentic_super_agent as asa
    from backend.core.ouroboros.governance import lesson_memory as lm
    recorded = []

    async def _rec(**kw):
        recorded.append(kw); return "recorded"

    monkeypatch.setattr(lm, "record_lesson", _rec)
    chunk = extract_target_chunk(BIG, "engine.py", "Engine.target")
    target = ChunkTarget(symbol="Engine.target", chunk=chunk, instruction="x")

    async def _agent(_t, feedback):
        return "def target(self, a, b):\n    return a + b\n"

    calls = []

    def _seam(node):
        calls.append(node); return "StitchCollisionError: boundary hallucinated"

    out = await asa.run_agentic_repair(target, _agent, max_turns=2, seam_validator=_seam)
    await asyncio.gather(*tuple(asa._LESSON_TASKS), return_exceptions=True)
    assert out.status == asa.STATUS_UNCONVERGED and len(calls) == 2
    classes = [r["error_class"] for r in recorded]
    assert classes.count("stitch_boundary_hallucination") == 2 and "stitch_node_unconverged" in classes
    assert all(r["phase"] == "STITCH" for r in recorded)


@pytest.mark.asyncio
async def test_a_lesson_store_fault_never_reaches_the_swarm(monkeypatch):
    from backend.core.ouroboros.governance import agentic_super_agent as asa
    from backend.core.ouroboros.governance import lesson_memory as lm

    async def _boom(**kw):
        raise RuntimeError("store locked")

    monkeypatch.setattr(lm, "record_lesson", _boom)
    chunk = extract_target_chunk(BIG, "engine.py", "Engine.target")
    target = ChunkTarget(symbol="Engine.target", chunk=chunk, instruction="x")

    async def _agent(_t, _f):
        return "def target(self, a, b):\n    return a + b\n"

    out = await asa.run_agentic_repair(target, _agent, max_turns=1)
    await asyncio.gather(*tuple(asa._LESSON_TASKS), return_exceptions=True)
    assert out.converged


# --------------------------------------------------------------------------
# the wiring — the local lane primes, frames, and re-prompts
# --------------------------------------------------------------------------

def test_the_local_lane_is_wired():
    src = (REPO / "backend/core/ouroboros/governance/candidate_generator.py").read_text(encoding="utf-8")
    for needle in ("await _cb.prime_budget(", "self._negotiate_num_ctx", "MAP_REDUCE_FRAMING", "extract_public_api(source, path)",
                   "node_context_fn=_radius_for", "rag_agent_fn=_rag_reprompt", "def local_lane_endpoint()"):
        assert needle in src, needle
    gls = (REPO / "backend/core/ouroboros/governance/governed_loop_service.py").read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.candidate_generator import local_lane_endpoint" in gls
    from backend.core.ouroboros.governance.candidate_generator import local_lane_endpoint
    assert callable(local_lane_endpoint)


def test_the_soak_no_longer_pins_the_interceptor_off():
    soak = Path("/mnt/c/Users/Jarvis/AppData/Local/Temp/claude/C--Users-Jarvis-Desktop-TrinityAi/ebb29656-c1bb-499d-9a1a-7df2e9565afe/scratchpad/goal_soak.sh")
    if soak.is_file():
        assert "export JARVIS_FULL_CONTENT_INTERCEPT_ENABLED=false" not in soak.read_text(encoding="utf-8")
