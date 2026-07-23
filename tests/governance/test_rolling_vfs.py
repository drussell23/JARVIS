"""Rolling VFS + Sequential Compilation Lock — indestructible Fan-In.

Mandated bulletproof: two concurrent agents return payloads simultaneously.
Assert (1) the asyncio.Lock forces sequential parsing, (2) Agent 1 updates the
Rolling Buffer, (3) Agent 2's payload fails syntax ONLY when combined with Agent
1's update, and (4) Agent 2 receives RebaseCollisionError and self-corrects.

Plus: the lock caps concurrent validations at 1 under true contention; a clean
node commits and rebases the buffer.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.agentic_super_agent import (
    STATUS_CONVERGED,
    run_agentic_repair,
)
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.polyglot_chunker import (
    polymorphic_extract_target,
)
from backend.core.ouroboros.governance.stitch_precompiler import RollingVFS

# A nested JSON config; agent A edits the parent object, agent B edits a child.
_JSON = '{\n  "config": {\n    "beta": 2\n  }\n}\n'


def _target(source: str, symbol: str) -> ChunkTarget:
    chunk = polymorphic_extract_target(source, "config.json", symbol)
    assert chunk is not None, symbol
    return ChunkTarget(symbol=symbol, chunk=chunk, instruction=f"repair {symbol}")


async def test_rolling_vfs_rebase_collision_and_sequential_lock() -> None:
    vfs = RollingVFS(_JSON, "config.json")
    a_committed = asyncio.Event()

    # Agent A (config): rewrite the parent object, appending an "added" key AFTER
    # beta — which forces beta to need a trailing comma it did not need before.
    async def agent_a(target, feedback):
        return '"config": {\n    "beta": 2,\n    "added": 9\n  }'

    b_feedbacks = []
    b_turn = {"n": 0}

    # Agent B (config.beta): waits until A has committed, then submits a node that
    # is VALID against the original (beta was the last key → no comma) but BREAKS
    # once A's "added" key follows it. Self-corrects with the comma on turn 2.
    async def agent_b(target, feedback):
        b_feedbacks.append(feedback)
        await a_committed.wait()
        b_turn["n"] += 1
        if b_turn["n"] == 1:
            return '"beta": 2'         # no comma → fractures ONLY after A's update
        return '"beta": 22,'           # rebased fix: trailing comma restored

    async def run_a():
        r = await run_agentic_repair(
            _target(_JSON, "config"), agent_a, max_turns=3,
            seam_validator=vfs.seam_validator_for("config"),
        )
        a_committed.set()              # signal AFTER A's graft is committed
        return r

    res_a, res_b = await asyncio.gather(
        run_a(),
        run_agentic_repair(
            _target(_JSON, "config.beta"), agent_b, max_turns=4,
            seam_validator=vfs.seam_validator_for("config.beta"),
        ),
    )

    # (1) The heavy parse never ran in parallel — the compile lock held.
    assert vfs.max_concurrent_validations == 1

    # (2) Agent 1 successfully updated the Rolling Buffer.
    assert res_a.status == STATUS_CONVERGED
    assert '"added": 9' in vfs.buffer

    # (3)+(4) Agent 2's first payload fractured ONLY in combination with A's
    # update, and it received a RebaseCollisionError → self-corrected on turn 2.
    assert b_turn["n"] == 2
    assert any("RebaseCollisionError" in (f or "") for f in b_feedbacks)
    assert res_b.status == STATUS_CONVERGED

    # The rebased buffer is valid JSON carrying BOTH agents' changes.
    parsed = json.loads(vfs.buffer)
    assert parsed["config"]["beta"] == 22
    assert parsed["config"]["added"] == 9


async def test_rebase_error_only_when_combined_not_in_isolation() -> None:
    # Prove the discriminator: the SAME node ('"beta": 2', no comma) is CLEAN
    # against the original buffer but a RebaseCollision against A's updated one.
    vfs = RollingVFS(_JSON, "config.json")
    seam_beta = vfs.seam_validator_for("config.beta")

    # Against the pristine buffer, the comma-less node is fine (beta is last key).
    assert await seam_beta('"beta": 2') is None

    # Reset + apply A first, THEN the same node collides.
    vfs2 = RollingVFS(_JSON, "config.json")
    assert await vfs2.seam_validator_for("config")(
        '"config": {\n    "beta": 2,\n    "added": 9\n  }'
    ) is None
    err = await vfs2.seam_validator_for("config.beta")('"beta": 2')
    assert err is not None
    assert "RebaseCollisionError" in err


async def test_lock_caps_concurrent_validations_under_contention() -> None:
    # Two agents hit the lock simultaneously (no event gating). The lock + the
    # yield inside it would let a SECOND coroutine in if the lock were absent;
    # with the lock, peak concurrency is exactly 1.
    vfs = RollingVFS(_JSON, "config.json")

    async def clean(target, feedback):
        return '"beta": 2'   # valid against pristine buffer

    # Only ONE can actually commit config.beta cleanly; the point here is the
    # lock — run two validations of the SAME symbol concurrently.
    seam = vfs.seam_validator_for("config.beta")
    await asyncio.gather(seam('"beta": 2'), seam('"beta": 2'))
    assert vfs.max_concurrent_validations == 1


async def test_symbol_removed_by_prior_agent_is_rebase_collision() -> None:
    vfs = RollingVFS(_JSON, "config.json")
    # A rewrites config WITHOUT beta (removes the child entirely).
    assert await vfs.seam_validator_for("config")(
        '"config": {\n    "gamma": 3\n  }'
    ) is None
    # B can no longer resolve config.beta → RebaseCollisionError.
    err = await vfs.seam_validator_for("config.beta")('"beta": 2,')
    assert err is not None and "RebaseCollisionError" in err
    assert "no longer resolvable" in err
