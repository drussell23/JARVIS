"""An op's retries are the pair, not noise to be overwritten.

``_admit_pending`` did ``self._pending[gen.op_id] = gen``. That made the
map last-write-wins, so an op generating more than once -- GENERATE_RETRY,
syntax repair, a sibling draw that itself retried -- kept only its final
attempt. Measured on soak bt-2026-08-31-185439: **31 generations across 13
ops produced 14 rows.**

The discarded attempts were the most valuable rows in the corpus. A retry
exists BECAUSE the previous attempt was rejected, so an op's lineage is
very often a genuine {rejected, chosen} pair on ONE prompt -- precisely
what DPO groups on, and precisely what the flat key destroyed.

The risk this structure introduces is memory: a lineage that grows without
bound, or a TTL that ages the wrong member. Those edges are what most of
these tests cover.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.observability import (
    trajectory_recorder as tr,
)


def _cand(h: str) -> Dict[str, Any]:
    return {
        "candidate_id": h, "candidate_hash": h,
        "file_path": "m.py", "full_content": f"# {h}\n",
    }


def _gen(op_id: str, hashes: List[str], **kw):
    base = dict(
        op_id=op_id, prompt="fix the helper", prompt_key="pk",
        candidates=tuple(_cand(h) for h in hashes),
        model_id="qwen3-coder:30b", provider_name="local_prime",
        is_noop=False, latency_ms=2000.0, prompt_tokens=100,
        completion_tokens=200, cost_usd=0.0, task_type="code_repair",
        session_id="s", tokens_estimated=False,
    )
    base.update(kw)
    return tr._PendingGeneration(**base)


@pytest.fixture()
def _rec(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_ENABLED", "true")
    tr.reset_recorder_for_tests(path=tmp_path / "ev" / "t.jsonl")
    yield tr.get_recorder()
    tr.reset_recorder_for_tests()


def _rows(rec, op_id: str) -> List[Dict[str, Any]]:
    return [
        r for r in (
            json.loads(ln)
            for ln in rec.path.read_text(encoding="utf-8").splitlines() if ln
        )
        if r["metadata"]["op_id"] == op_id
    ]


# --------------------------------------------------------------------------
# Lineage preservation
# --------------------------------------------------------------------------


def test_retries_are_appended_not_overwritten(_rec) -> None:
    """Three attempts of one op must produce three attempts' worth of rows."""

    async def _go():
        for hs in (["a1"], ["a2"], ["a3"]):
            await _rec._admit_pending(_gen("op-1", hs))
        assert len(_rec._pending["op-1"]) == 3, "lineage collapsed"
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-1", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-1")
    assert [r["metadata"]["candidate_hash"] for r in rows] == ["a1", "a2", "a3"]
    assert [r["metadata"]["attempt_index"] for r in rows] == [0, 1, 2]
    assert {r["metadata"]["lineage_size"] for r in rows} == {3}


def test_lineage_rows_share_a_prompt_so_dpo_can_group_them(_rec) -> None:
    """The whole point: same prompt, different answers, one op."""

    async def _go():
        await _rec._admit_pending(_gen("op-2", ["first"]))
        await _rec._admit_pending(_gen("op-2", ["second"]))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-2", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-2")
    assert len({r["user_input"] for r in rows}) == 1, "grouping key must match"
    assert len({r["assistant_output"] for r in rows}) == 2, "answers must differ"


def test_idempotency_key_is_attempt_scoped(_rec) -> None:
    """Two attempts must not collide when candidate_hash is absent.

    The key falls back to the candidate INDEX, which is 0 for the first
    candidate of every attempt -- so without the attempt in the key a
    downstream dedup would drop the retry, the row this change exists to
    keep.
    """

    async def _go():
        for _ in range(2):
            g = _gen("op-3", ["x"])
            g.candidates = ({"candidate_id": "c", "candidate_hash": "",
                             "file_path": "m.py", "full_content": "# body\n"},)
            await _rec._admit_pending(g)
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-3", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    keys = [r["metadata"]["idempotency_key"] for r in _rows(_rec, "op-3")]
    assert len(set(keys)) == 2, f"attempts collided: {keys}"


# --------------------------------------------------------------------------
# Per-candidate verdicts across a lineage
# --------------------------------------------------------------------------


def test_verdict_lands_on_the_attempt_that_produced_the_candidate(_rec) -> None:
    """Not "the newest attempt" -- the one that actually emitted it.

    Guessing would credit a retry's verdict to the wrong attempt and
    mislabel exactly the row a pair is built from.
    """

    async def _go():
        await _rec._admit_pending(_gen("op-4", ["old"]))
        await _rec._admit_pending(_gen("op-4", ["new"]))
        # Verdict for the FIRST attempt, arriving after the second exists.
        await _rec._attach_verdict(tr._CandidateVerdictEvent(
            op_id="op-4", candidate_hash="old", passed=False,
            failure_class="tests_failed",
        ))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-4", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    by_hash = {r["metadata"]["candidate_hash"]: r for r in _rows(_rec, "op-4")}
    assert by_hash["old"]["outcome"] == "failure"
    assert by_hash["old"]["metadata"]["verdict_source"] == "candidate"
    assert by_hash["new"]["outcome"] == "success"
    assert by_hash["new"]["metadata"]["verdict_source"] == "operation"


def test_unmatched_verdict_is_counted_not_guessed_onto_a_neighbour(_rec) -> None:
    async def _go():
        await _rec._admit_pending(_gen("op-5", ["real"]))
        await _rec._attach_verdict(tr._CandidateVerdictEvent(
            op_id="op-5", candidate_hash="ghost", passed=False, failure_class="",
        ))
        return dict(_rec.stats())

    stats = asyncio.run(_go())
    assert stats["orphan_candidate_verdicts"] == 1
    assert stats["candidate_verdicts_joined"] == 0


# --------------------------------------------------------------------------
# Memory: the risk this structure introduces
# --------------------------------------------------------------------------


def test_cap_counts_generations_not_ops(
    _rec, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single retry-storming op must not hold unbounded memory.

    Counting OPS would let one op grow a lineage of thousands while the
    map looked one entry deep -- the leak a list-valued map invites.
    """
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_PENDING_MAX", "8")

    async def _go():
        for i in range(25):
            await _rec._admit_pending(_gen("op-storm", [f"c{i}"]))
        return _rec._pending_count(), dict(_rec.stats())

    total, stats = asyncio.run(_go())
    assert total == 8, f"cap not enforced across a single lineage: {total}"
    assert stats["pending_evicted"] == 17


def test_eviction_drops_the_oldest_attempt_not_the_whole_lineage(
    _rec, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evicting a list would discard newer generations to make room for one."""
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_PENDING_MAX", "8")

    async def _go():
        for op in ("a", "b"):
            for i in range(5):
                await _rec._admit_pending(_gen(f"op-{op}", [f"{op}{i}"]))
        return {k: [g.candidates[0]["candidate_hash"] for g in v]
                for k, v in _rec._pending.items()}

    state = asyncio.run(_go())
    # 10 admitted, cap 8 -> the two OLDEST attempts of the oldest op go.
    assert state["op-a"] == ["a2", "a3", "a4"]
    assert state["op-b"] == ["b0", "b1", "b2", "b3", "b4"]


def test_ttl_ages_each_attempt_on_its_own_clock(_rec) -> None:
    """A fresh retry must not be flushed because an earlier attempt is old.

    Expiring a whole lineage on its oldest member would flush a retry
    seconds old that is still likely to get a real verdict; expiring on
    its newest would pin stale attempts for as long as the op keeps
    retrying. Both are wrong; each attempt gets its own clock.
    """
    import time as _t

    async def _go():
        old = _gen("op-6", ["stale"])
        old.created_monotonic = _t.monotonic() - 10_000.0
        fresh = _gen("op-6", ["fresh"])
        await _rec._admit_pending(old)
        await _rec._admit_pending(fresh)
        await _rec._expire_pending()
        return (
            [g.candidates[0]["candidate_hash"] for g in _rec._pending.get("op-6", [])],
            _rows(_rec, "op-6"),
        )

    still_pending, written = asyncio.run(_go())
    assert still_pending == ["fresh"], "the fresh retry was flushed with the stale one"
    assert [r["metadata"]["candidate_hash"] for r in written] == ["stale"]
    # An outcome never seen is not a label.
    assert written[0]["outcome"] == "unknown"
    assert written[0]["metadata"]["should_train"] is False


def test_expiring_the_last_attempt_drops_the_op_key(_rec) -> None:
    """An emptied lineage must not linger as a dead key -- that IS the leak."""
    import time as _t

    async def _go():
        g = _gen("op-7", ["only"])
        g.created_monotonic = _t.monotonic() - 10_000.0
        await _rec._admit_pending(g)
        await _rec._expire_pending()
        return "op-7" in _rec._pending, _rec._pending_count()

    still_there, count = asyncio.run(_go())
    assert not still_there
    assert count == 0


def test_teardown_flush_walks_lineages(_rec) -> None:
    """``aclose`` iterates values, which are LISTS now.

    Treating them as generations would set an attribute on a list and
    raise into the fail-open -- turning the final flush back into the
    silent no-op it was before it had a caller at all.
    """

    async def _go():
        for i in range(3):
            await _rec._admit_pending(_gen("op-8", [f"t{i}"]))
        await _rec.aclose(timeout_s=5.0)
        return _rows(_rec, "op-8")

    rows = asyncio.run(_go())
    assert [r["metadata"]["attempt_index"] for r in rows] == [0, 1, 2]
