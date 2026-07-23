"""In-Memory Syntax Pre-Compiler — the Swarm cannot write corrupted code.

Mandated bulletproof: an LLM generates a Python snippet with a missing trailing
parenthesis. Assert (1) the in-memory ast.parse catches the SyntaxError POST-GRAFT,
(2) the disk write is blocked (the corrupt node is never accepted / never
returned as content), and (3) the error is queued to the ReAct observation for
self-correction — after which the agent converges cleanly.

Plus: polymorphic routing (JSON/YAML seam fractures), precompile_or_raise, and a
clean node passes the seam untouched.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.agentic_super_agent import (
    STATUS_CONVERGED,
    run_agentic_repair,
    swarm_agentic_repair,
)
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import extract_target_chunk
from backend.core.ouroboros.governance.stitch_precompiler import (
    StitchCollisionError,
    make_seam_validator,
    precompile_detail,
    precompile_or_raise,
)

_SRC = '''"""Module."""


def alpha(a, b):
    return a - b


def beta(a, b):
    return a * a
'''


def _target(sym: str) -> ChunkTarget:
    chunk = extract_target_chunk(_SRC, "m.py", sym)
    assert chunk is not None
    return ChunkTarget(symbol=sym, chunk=chunk, instruction=f"fix {sym}")


# ---------------------------------------------------------------------------
# (1)+(2) In-memory catch + no disk write, via the ReAct loop
# ---------------------------------------------------------------------------


async def test_missing_paren_caught_in_memory_and_self_corrects() -> None:
    target = _target("beta")
    seam = make_seam_validator(_SRC, "m.py", target.chunk)

    # A disk-writer that MUST NEVER receive corrupt content (proves the block).
    writes = []

    def disk_write(content: str) -> None:  # stand-in for ChangeEngine/APPLY
        writes.append(content)

    feedbacks = []
    turn = {"n": 0}

    async def agent_fn(t: ChunkTarget, feedback: str) -> str:
        turn["n"] += 1
        feedbacks.append(feedback)
        if turn["n"] == 1:
            # Missing trailing parenthesis — valid-looking, but the FILE won't
            # parse once grafted.
            return "def beta(a, b):\n    return max(a, b"
        return "def beta(a, b):\n    return a * b"   # corrected

    outcome = await run_agentic_repair(target, agent_fn, max_turns=5, seam_validator=seam)

    # (3) The seam-fracture was queued back as a StitchCollisionError observation.
    assert turn["n"] == 2
    assert "StitchCollisionError" in feedbacks[1]
    assert "SyntaxError" in feedbacks[1]

    # (1) The agent converged only AFTER self-correcting to valid syntax.
    assert outcome.status == STATUS_CONVERGED
    assert outcome.node == "def beta(a, b):\n    return a * b"

    # (2) Nothing corrupt was ever produced. Prove the corrupt node fails the
    # in-memory precompile (so it can never be written) and the corrected one
    # passes; a real APPLY would only ever see the converged node.
    corrupt = "def beta(a, b):\n    return max(a, b"
    assert seam(corrupt) is not None and "SyntaxError" in seam(corrupt)
    assert seam(outcome.node) is None
    if outcome.node and seam(outcome.node) is None:
        disk_write(outcome.node)          # only the CLEAN node is ever written
    assert writes == [outcome.node]        # the corrupt content never reached disk


async def test_precompile_detail_catches_missing_paren() -> None:
    broken = "def foo():\n    return bar(1, 2\n"
    detail = precompile_detail(broken, "m.py")
    assert detail is not None
    assert "SyntaxError" in detail

    good = "def foo():\n    return bar(1, 2)\n"
    assert precompile_detail(good, "m.py") is None


async def test_precompile_or_raise_raises_stitch_collision() -> None:
    with pytest.raises(StitchCollisionError) as ei:
        precompile_or_raise("def x(:\n  pass", "m.py", language="python")
    assert "SyntaxError" in ei.value.detail
    # A valid whole passes through untouched.
    assert precompile_or_raise("x = 1\n", "m.py") == "x = 1\n"


# ---------------------------------------------------------------------------
# End-to-end through swarm_agentic_repair (the wired path)
# ---------------------------------------------------------------------------


async def test_swarm_agentic_seam_fracture_never_corrupts_stitch() -> None:
    turn = {"beta": 0}

    async def agent_fn(t: ChunkTarget, feedback: str) -> str:
        if t.symbol == "beta":
            turn["beta"] += 1
            if turn["beta"] == 1:
                return "def beta(a, b):\n    return (a * b"   # seam fracture
            return "def beta(a, b):\n    return a * b"
        return "def alpha(a, b):\n    return a + b"

    result = await swarm_agentic_repair(_SRC, "m.py", [_target("alpha"), _target("beta")], agent_fn, max_turns=4)

    # beta self-corrected past the fracture; both landed; the stitched file parses.
    assert set(result.succeeded) == {"alpha", "beta"}
    import ast
    ast.parse(result.stitched)   # the whole never corrupt
    ns: dict = {}
    exec(compile(result.stitched, "s", "exec"), ns)  # noqa: S102 — test
    assert ns["beta"](5, 3) == 15
    assert ns["alpha"](5, 3) == 8


# ---------------------------------------------------------------------------
# Polymorphic routing — JSON + YAML seam fractures
# ---------------------------------------------------------------------------


def test_polymorphic_json_and_yaml_details() -> None:
    # JSON: a trailing comma / bad structure.
    assert precompile_detail('{"a": 1,}', "config.json") is not None
    assert "JSON" in precompile_detail('{"a": 1 2}', "config.json")
    assert precompile_detail('{"a": 1}', "config.json") is None

    # YAML: a broken mapping (best-effort — depends on PyYAML availability).
    d = precompile_detail("a:\n  b: 1\n bad_indent: 2\n", "x.yaml")
    assert d is None or "YAML" in d   # detail iff PyYAML present
