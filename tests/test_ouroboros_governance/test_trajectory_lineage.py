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


# --------------------------------------------------------------------------
# Lineage purification: draw kinds, the validation guard, hash dedupe
# --------------------------------------------------------------------------
#
# Soak bt-2026-09-03-012434 (soak 17): 29 of 43 rows were L2 repair
# re-generations recorded as sibling attempts of the draw they repaired --
# attempt patterns [0,1,2,1], [0,1,1] -- so the "twins" the harvest paired
# were the same accepted candidate written twice, 1.0000 alike. A repair
# answers a DIFFERENT prompt (the failing tests) and is never a sibling of
# the primary draw; a duplicate hash is one row however it was produced.


def test_draw_kind_is_derived_from_the_seam_not_guessed() -> None:
    assert tr.derive_draw_kind(is_repair=True, sampling=None) == tr.DRAW_REPAIR
    assert tr.derive_draw_kind(is_repair=False, sampling=None) == tr.DRAW_PRIMARY
    assert tr.derive_draw_kind(
        is_repair=False, sampling={"top_p": 0.9, "seed": 7}) == tr.DRAW_SIBLING
    # An explicit tag wins; a repair with sampling is still a repair.
    assert tr.derive_draw_kind(
        is_repair=False, sampling=None, explicit=tr.DRAW_SIBLING) == tr.DRAW_SIBLING
    assert tr.derive_draw_kind(
        is_repair=True, sampling={"top_p": 0.9}) == tr.DRAW_REPAIR


def test_genuine_draws_are_primary_sibling_and_legacy_unknown() -> None:
    assert tr.is_genuine_draw({"draw_kind": tr.DRAW_PRIMARY})
    assert tr.is_genuine_draw({"draw_kind": tr.DRAW_SIBLING})
    assert tr.is_genuine_draw({}), "a pre-discriminator row must not vanish"
    assert not tr.is_genuine_draw({"draw_kind": tr.DRAW_REPAIR})
    assert not tr.is_genuine_draw({"draw_kind": tr.DRAW_RETRY})


def test_rows_carry_the_discriminator_and_the_sampling_point(_rec) -> None:
    async def _go():
        await _rec._admit_pending(_gen(
            "op-k1", ["p"], draw_kind=tr.DRAW_PRIMARY, temperature=0.7))
        await _rec._admit_pending(_gen(
            "op-k1", ["s"], draw_kind=tr.DRAW_SIBLING, temperature=1.1,
            sampling={"top_p": 0.95, "top_k": 40, "seed": 12}))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k1", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-k1")
    assert [r["metadata"]["draw_kind"] for r in rows] == [tr.DRAW_PRIMARY, tr.DRAW_SIBLING]
    assert [r["metadata"]["temperature"] for r in rows] == [0.7, 1.1]
    assert rows[1]["metadata"]["sampling"] == {"top_p": 0.95, "top_k": 40, "seed": 12}
    assert rows[0]["metadata"]["sampling"] == {}


def test_a_second_primary_on_one_op_is_a_retry_not_a_sibling(_rec) -> None:
    """GENERATE_RETRY re-draws at the legacy point; only ONE primary exists."""

    async def _go():
        await _rec._admit_pending(_gen("op-k2", ["a"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen("op-k2", ["b"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k2", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    kinds = [r["metadata"]["draw_kind"] for r in _rows(_rec, "op-k2")]
    assert kinds == [tr.DRAW_PRIMARY, tr.DRAW_RETRY]


def test_repair_that_re_records_an_earlier_hash_is_pruned(_rec) -> None:
    """The soak-17 twin: L2 re-emits the accepted candidate. Zero new rows."""

    async def _go():
        await _rec._admit_pending(_gen("op-k3", ["same"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen("op-k3", ["same"], draw_kind=tr.DRAW_REPAIR))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k3", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-k3")
    assert len(rows) == 1
    assert rows[0]["metadata"]["draw_kind"] == tr.DRAW_PRIMARY
    assert rows[0]["metadata"]["lineage_size"] == 1
    assert _rec._stats["lineage_pruned"] == 1


def test_repair_that_produced_something_new_is_kept_and_tagged(_rec) -> None:
    """A genuinely different repair is a second answer -- kept, but tagged
    so the harvest can decline to pair it with the primary."""

    async def _go():
        await _rec._admit_pending(_gen("op-k4", ["orig"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen("op-k4", ["fixed"], draw_kind=tr.DRAW_REPAIR))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k4", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-k4")
    assert [r["metadata"]["draw_kind"] for r in rows] == [tr.DRAW_PRIMARY, tr.DRAW_REPAIR]
    assert [r["metadata"]["attempt_index"] for r in rows] == [0, 1]
    assert _rec._stats["lineage_pruned"] == 0


def test_guard_reindexes_survivors_densely(_rec) -> None:
    async def _go():
        await _rec._admit_pending(_gen("op-k5", ["a"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen("op-k5", ["a"], draw_kind=tr.DRAW_REPAIR))
        await _rec._admit_pending(_gen("op-k5", ["c"], draw_kind=tr.DRAW_SIBLING))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k5", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    rows = _rows(_rec, "op-k5")
    assert [r["metadata"]["candidate_hash"] for r in rows] == ["a", "c"]
    assert [r["metadata"]["attempt_index"] for r in rows] == [0, 1]
    assert {r["metadata"]["lineage_size"] for r in rows} == {2}


def test_guard_drops_a_generation_with_no_candidates(_rec) -> None:
    async def _go():
        await _rec._admit_pending(_gen("op-k6", ["a"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen("op-k6", [], draw_kind=tr.DRAW_SIBLING))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k6", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    assert [r["metadata"]["candidate_hash"] for r in _rows(_rec, "op-k6")] == ["a"]
    assert _rec._stats["lineage_pruned"] == 1


def test_guard_never_raises_on_a_malformed_lineage(_rec) -> None:
    """A guard that loses a lineage on its own bug is worse than no guard."""
    g = _gen("op-k7", ["a"], draw_kind=tr.DRAW_REPAIR)
    g.candidates = ("not-a-dict", None, {"candidate_hash": "z"})
    out = _rec._validate_lineage("op-k7", [g])
    assert out == [g]


def test_same_hash_twice_in_genuine_draws_is_written_once(_rec) -> None:
    """Deterministic (op_id, candidate_hash) dedupe at persistence: a
    sibling that collapsed onto the primary bytes exactly is one row."""

    async def _go():
        await _rec._admit_pending(_gen("op-k8", ["dup"], draw_kind=tr.DRAW_PRIMARY))
        await _rec._admit_pending(_gen(
            "op-k8", ["dup"], draw_kind=tr.DRAW_SIBLING, sampling={"seed": 1}))
        await _rec._resolve(tr._OutcomeEvent(
            op_id="op-k8", terminal_phase="COMPLETED", terminal_reason="applied",
        ))

    asyncio.run(_go())
    assert len(_rows(_rec, "op-k8")) == 1
    assert _rec._stats["rows_deduped"] == 1
    assert "op-k8" not in getattr(_rec, "_persisted", {}), "dedupe set must be released"


def test_dedupe_is_per_op_not_global(_rec) -> None:
    async def _go():
        for op in ("op-k9a", "op-k9b"):
            await _rec._admit_pending(_gen(op, ["shared"], draw_kind=tr.DRAW_PRIMARY))
            await _rec._resolve(tr._OutcomeEvent(
                op_id=op, terminal_phase="COMPLETED", terminal_reason="applied",
            ))

    asyncio.run(_go())
    assert len(_rows(_rec, "op-k9a")) == 1 and len(_rows(_rec, "op-k9b")) == 1
    assert _rec._stats["rows_deduped"] == 0
