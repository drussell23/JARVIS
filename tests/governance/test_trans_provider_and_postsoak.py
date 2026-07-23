"""Trans-Provider Re-Router + Post-Soak Verification Circuit — final hardening.

Mandated bulletproof: a 3-agent Swarm where Agent 2 hits a mid-loop provider
collapse. Assert (1) Agent 2 migrates to the fallback tier WITHOUT interrupting
Agents 1 & 3, (2) the Rolling VFS stitches all 3 outputs cleanly, and (3) the
Post-Soak Verification Circuit confirms AST integrity + logs the trace to SQLite.
"""

from __future__ import annotations

import ast
import sqlite3

import pytest

from backend.core.ouroboros.governance.agentic_super_agent import swarm_agentic_repair
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import extract_target_chunk
from backend.core.ouroboros.governance.post_soak_verification import post_soak_verify
from backend.core.ouroboros.governance.stream_rupture import StreamRuptureError
from backend.core.ouroboros.governance.swarm_trace import SwarmTracer
from backend.core.ouroboros.governance.trans_provider_rerouter import (
    TransProviderReRouter,
)

_SRC = '''"""Module."""


def alpha(a, b):
    return a - b


def beta(a, b):
    return a * a


def gamma(xs):
    return xs[0]
'''

_FIX = {
    "alpha": "def alpha(a, b):\n    return a + b",
    "beta": "def beta(a, b):\n    return a * b",
    "gamma": "def gamma(xs):\n    return sorted(xs)[0]",
}


def _targets():
    ts = []
    for sym in ("alpha", "beta", "gamma"):
        ch = extract_target_chunk(_SRC, "m.py", sym)
        assert ch is not None
        ts.append(ChunkTarget(symbol=sym, chunk=ch, instruction=f"fix {sym}"))
    return ts


async def test_agent2_reroutes_to_fallback_siblings_unaffected_then_postsoak(tmp_path) -> None:
    primary_calls = []
    fallback_calls = []

    # PRIMARY tier: beta's provider collapses mid-loop (StreamRuptureError);
    # alpha + gamma succeed on the primary.
    async def primary(target, feedback):
        primary_calls.append(target.symbol)
        if target.symbol == "beta":
            raise StreamRuptureError(
                provider="doubleword", elapsed_s=8.0, bytes_received=0,
                rupture_timeout_s=8.0, phase="inter_chunk",
            )
        return _FIX[target.symbol]

    # FALLBACK tier (FailoverLifecycle-resolved): repairs beta cleanly.
    async def fallback(target, feedback):
        fallback_calls.append(target.symbol)
        return _FIX[target.symbol]

    rerouter = TransProviderReRouter(primary, fallback)

    result = await swarm_agentic_repair(_SRC, "m.py", _targets(), rerouter, max_turns=4)

    # (1) Agent 2 (beta) migrated to the fallback tier; the whole swarm did NOT
    # abort — alpha + gamma completed on the primary, untouched.
    assert rerouter.was_rerouted("beta") is True
    assert rerouter.reroutes and rerouter.reroutes[0][0] == "beta"
    assert "stream_rupture" in rerouter.reroutes[0][1]
    assert "beta" in fallback_calls
    assert "alpha" not in fallback_calls and "gamma" not in fallback_calls  # siblings never fell back

    # (2) The Rolling VFS stitched all 3 cleanly.
    assert set(result.succeeded) == {"alpha", "beta", "gamma"}
    assert result.failed == []
    ast.parse(result.stitched)
    ns: dict = {}
    exec(compile(result.stitched, "s", "exec"), ns)  # noqa: S102 — test
    assert ns["alpha"](5, 3) == 8
    assert ns["beta"](5, 3) == 15     # repaired via the fallback tier
    assert ns["gamma"]([3, 1, 2]) == 1

    # (3) Post-Soak Verification Circuit: AST integrity + git-clean + trace flush.
    trace_path = tmp_path / "swarm_trace.jsonl"
    tracer = SwarmTracer(str(trace_path), op_id="op-postsoak")
    tracer.record_fan_out(sub_agent="beta", symbol="beta", node_start_line=8, node_end_line=9, concurrency=1)
    tracer.record_fan_in(sub_agent="beta", symbol="beta", converged=True, concurrency=1)

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    verdict = await post_soak_verify(
        file_path="m.py", stitched_content=result.stitched,
        trace_path=str(trace_path), sqlite_conn=conn,
        git_status_fn=lambda: [" M m.py"],   # working-tree change only (not staged)
    )

    assert verdict.ast_ok is True
    assert verdict.git_clean is True
    assert verdict.trace_flushed is True
    assert verdict.ready_for_promotion is True
    assert verdict.trace_records == 2       # fan_out + fan_in

    # The trace summary landed in SQLite.
    row = conn.execute(
        "SELECT file_path, records, converged, ready FROM swarm_trace_flush"
    ).fetchone()
    assert row == ("m.py", 2, 1, 1)


# ---------------------------------------------------------------------------
# Re-router unit behaviors
# ---------------------------------------------------------------------------


async def test_rerouter_sticky_per_symbol() -> None:
    calls = {"primary": 0, "fallback": 0}

    async def primary(target, feedback):
        calls["primary"] += 1
        raise StreamRuptureError(provider="dw", elapsed_s=1, bytes_received=0,
                                 rupture_timeout_s=1, phase="ttft")

    async def fallback(target, feedback):
        calls["fallback"] += 1
        return "ok"

    rr = TransProviderReRouter(primary, fallback)
    t = ChunkTarget(symbol="x", chunk=None, instruction="")
    assert await rr(t, "") == "ok"      # trip → fallback
    assert await rr(t, "") == "ok"      # sticky → straight to fallback
    assert calls["primary"] == 1        # primary tried once, then skipped
    assert calls["fallback"] == 2


async def test_rerouter_propagates_non_transport_error() -> None:
    async def primary(target, feedback):
        raise ValueError("a genuine logic bug, not transport")

    async def fallback(target, feedback):
        return "ok"

    rr = TransProviderReRouter(primary, fallback)
    t = ChunkTarget(symbol="y", chunk=None, instruction="")
    with pytest.raises(ValueError):
        await rr(t, "")                 # logic error is NOT swallowed by failover
    assert rr.was_rerouted("y") is False


# ---------------------------------------------------------------------------
# Post-Soak circuit unit behaviors
# ---------------------------------------------------------------------------


async def test_postsoak_blocks_on_ast_regression() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    verdict = await post_soak_verify(
        file_path="m.py", stitched_content="def broken(:\n  pass",   # SyntaxError
        sqlite_conn=conn, git_status_fn=lambda: [],
    )
    assert verdict.ast_ok is False
    assert verdict.ready_for_promotion is False
    assert any("ast_regression" in r for r in verdict.reasons)


async def test_postsoak_blocks_on_unexpected_staged_change() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    verdict = await post_soak_verify(
        file_path="m.py", stitched_content="x = 1\n", sqlite_conn=conn,
        git_status_fn=lambda: ["M  secrets.env"],   # a STAGED change to something else
    )
    assert verdict.ast_ok is True
    assert verdict.git_clean is False
    assert verdict.ready_for_promotion is False
    assert any("git_index_dirty" in r for r in verdict.reasons)
