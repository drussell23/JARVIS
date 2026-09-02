"""The retract seam: a draw the generator DROPS must not reach the corpus.

``record_generation`` fires inside the provider, per call, before the
sibling loop has judged the draw. Measured on soak
`bt-2026-09-01-235803`: every rejected sibling (similarity 1.0000, "adds no
logic", dropped) had already been queued as a pending generation, so it
would have been written at the op's terminal verdict as a row carrying the
SAME ``structure_id`` as the candidate it duplicated -- a row that looks
like half of a preference pair and cannot be one.

The seam is deterministic by construction rather than by timing: the
retract event rides the SAME queue as the generation it names, so it is
processed after that generation was admitted and before the outcome that
would write it. These tests pin that, plus the two ways it must refuse to
guess: a retract naming nothing pending is an orphan (counted, never
applied), and a retract covering only PART of a multi-candidate draw
leaves that draw alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
    TrajectoryRecorder,
    record_retraction,
    reset_recorder_for_tests,
)

_ENV_MASTER = "JARVIS_TRAJECTORY_RECORDER_ENABLED"


@dataclass
class _FakeGenerationResult:
    candidates: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    provider_name: str = "local-prime"
    model_id: str = "qwen3-coder:30b"
    is_noop: bool = False
    prompt_preloaded_files: Tuple[str, ...] = field(default_factory=tuple)
    total_input_tokens: int = 900
    total_output_tokens: int = 120
    cost_usd: float = 0.0


def _candidate(cid: str, body: str) -> Dict[str, Any]:
    return {
        "candidate_id": cid,
        "candidate_hash": f"hash-{cid}",
        "file_path": "backend/mod.py",
        "source_path": "backend/mod.py",
        "full_content": body,
        "rationale": "unit-test candidate",
    }


_A = "def run(x):\n    return x + 1\n"
_B = "def run(x):\n    total = 0\n    for i in range(x):\n        total += i\n    return total\n"
_A_TWIN = "def run(x):\n    return x + 1\n"


@pytest.fixture()
def rec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TrajectoryRecorder:
    monkeypatch.setenv(_ENV_MASTER, "true")
    return reset_recorder_for_tests(path=tmp_path / "experience.jsonl")


def _rows(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _gen(rec: TrajectoryRecorder, op: str, cid: str, body: str) -> None:
    assert rec.record_generation(
        op_id=op, prompt=f"prompt for {op}",
        generation_result=_FakeGenerationResult(candidates=(_candidate(cid, body),)),
    ) is True


# --------------------------------------------------------------------------
# The property
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retracted_draw_never_reaches_the_corpus(rec: TrajectoryRecorder) -> None:
    """Draw 1, a redundant draw 2, a distinct draw 3 -- then the verdict.

    Two rows must land, not three, and they must be the two answers the
    generator actually kept. The twin's hash must not appear at all.
    """
    _gen(rec, "op-1", "c1", _A)
    _gen(rec, "op-1", "c2", _A_TWIN)          # the generator will reject this
    assert rec.record_retraction(
        op_id="op-1", candidate_hashes=("hash-c2",), reason="redundant_redraw",
    ) is True
    _gen(rec, "op-1", "c3", _B)
    assert rec.record_outcome(
        op_id="op-1", terminal_phase="COMPLETED", terminal_reason="applied",
    ) is True
    await rec.drain()

    rows = _rows(rec.path)
    assert [r["metadata"]["candidate_hash"] for r in rows] == ["hash-c1", "hash-c3"]
    # Survivors are re-indexed densely and the group size is the SURVIVING
    # size -- a reader grouping by op_id must see two rows claiming two.
    assert [r["metadata"]["attempt_index"] for r in rows] == [0, 1]
    assert {r["metadata"]["lineage_size"] for r in rows} == {2}
    stats = rec.stats()
    assert stats["generations_retracted"] == 1
    assert stats.get("orphan_retractions", 0) == 0


@pytest.mark.asyncio
async def test_retraction_is_ordered_by_the_queue_not_by_timing(rec: TrajectoryRecorder) -> None:
    """Generation, retraction and outcome are all queued BEFORE any drain.

    If ordering depended on when the drain loop happened to run, this
    could write the twin. It cannot: the queue is FIFO and single.
    """
    _gen(rec, "op-2", "c1", _A)
    _gen(rec, "op-2", "c2", _A_TWIN)
    rec.record_retraction(op_id="op-2", candidate_hashes=("hash-c2",))
    rec.record_outcome(op_id="op-2", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert [r["metadata"]["candidate_hash"] for r in rows] == ["hash-c1"]
    assert rows[0]["metadata"]["lineage_size"] == 1


# --------------------------------------------------------------------------
# The two refusals to guess
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retraction_naming_nothing_pending_is_an_orphan(rec: TrajectoryRecorder) -> None:
    """Counted and named; never applied to the wrong lineage."""
    _gen(rec, "op-3", "c1", _A)
    rec.record_retraction(op_id="op-3", candidate_hashes=("hash-never-drawn",))
    rec.record_outcome(op_id="op-3", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert [r["metadata"]["candidate_hash"] for r in rows] == ["hash-c1"]
    assert rec.stats().get("orphan_retractions", 0) == 1
    assert rec.stats().get("generations_retracted", 0) == 0


@pytest.mark.asyncio
async def test_a_partial_hash_set_does_not_remove_a_multi_candidate_draw(
    rec: TrajectoryRecorder,
) -> None:
    """A draw is one provider call; its hashes are one lineage entry.

    Retracting only some of them describes no draw that exists, so the
    draw stays -- removing it would delete a candidate nobody rejected.
    """
    multi = _FakeGenerationResult(
        candidates=(_candidate("m1", _A), _candidate("m2", _B)),
    )
    assert rec.record_generation(op_id="op-4", prompt="p", generation_result=multi) is True
    rec.record_retraction(op_id="op-4", candidate_hashes=("hash-m1",))
    rec.record_outcome(op_id="op-4", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert sorted(r["metadata"]["candidate_hash"] for r in rows) == ["hash-m1", "hash-m2"]
    assert rec.stats().get("orphan_retractions", 0) == 1


@pytest.mark.asyncio
async def test_retracting_every_draw_leaves_no_lineage_and_no_orphan_outcome_noise(
    rec: TrajectoryRecorder,
) -> None:
    _gen(rec, "op-5", "c1", _A)
    rec.record_retraction(op_id="op-5", candidate_hashes=("hash-c1",))
    rec.record_outcome(op_id="op-5", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    assert _rows(rec.path) == []
    assert rec.stats()["generations_retracted"] == 1


# --------------------------------------------------------------------------
# Edges of the API
# --------------------------------------------------------------------------


def test_default_off_and_empty_inputs_return_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_MASTER, "false")
    r = reset_recorder_for_tests(path=tmp_path / "e.jsonl")
    assert r.record_retraction(op_id="op-x", candidate_hashes=("h",)) is False
    monkeypatch.setenv(_ENV_MASTER, "true")
    r = reset_recorder_for_tests(path=tmp_path / "e.jsonl")
    assert r.record_retraction(op_id="", candidate_hashes=("h",)) is False
    assert r.record_retraction(op_id="op-x", candidate_hashes=()) is False
    assert r.record_retraction(op_id="op-x", candidate_hashes=None) is False
    assert record_retraction(op_id="op-x", candidate_hashes=("", None)) is False


@pytest.mark.asyncio
async def test_the_generator_facing_helper_forwards_candidate_dicts(
    rec: TrajectoryRecorder,
) -> None:
    """`sibling_entropy.retract_draw` is what the sibling loop calls."""
    from backend.core.ouroboros.governance import sibling_entropy as se

    _gen(rec, "op-6", "c1", _A)
    _gen(rec, "op-6", "c2", _A_TWIN)
    assert se.retract_draw("op-6", [_candidate("c2", _A_TWIN)], reason="test") is True
    assert se.retract_draw("op-6", [{"no": "hash"}], reason="test") is False
    assert se.retract_draw("", [_candidate("c2", _A_TWIN)]) is False
    rec.record_outcome(op_id="op-6", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    assert [r["metadata"]["candidate_hash"] for r in _rows(rec.path)] == ["hash-c1"]


# --------------------------------------------------------------------------
# The collision soak 12 exposed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_byte_identical_twin_shares_a_hash_and_must_not_take_the_original(
    rec: TrajectoryRecorder,
) -> None:
    """candidate_hash is content-derived, so an identical twin carries the
    SAME hash as the candidate it duplicates. Measured on soak
    bt-2026-09-02-013719: one retract event removed two generations and the
    kept candidate's verdict then orphaned. A retract names ONE draw -- the
    most recent generation carrying those hashes -- and stops there."""
    first = _FakeGenerationResult(candidates=(_candidate("same", _A),))
    twin = _FakeGenerationResult(candidates=(_candidate("same", _A),))   # same hash-same
    assert rec.record_generation(op_id="op-7", prompt="p", generation_result=first) is True
    assert rec.record_generation(op_id="op-7", prompt="p", generation_result=twin) is True
    rec.record_retraction(op_id="op-7", candidate_hashes=("hash-same",), reason="redundant_redraw:1.0000")
    rec.record_outcome(op_id="op-7", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert len(rows) == 1, "the accepted generation must survive its twin's retraction"
    assert rows[0]["metadata"]["candidate_hash"] == "hash-same"
    assert rows[0]["metadata"]["lineage_size"] == 1
    assert rec.stats()["generations_retracted"] == 1


@pytest.mark.asyncio
async def test_two_retractions_remove_two_twins_newest_first(rec: TrajectoryRecorder) -> None:
    """Three identical draws, two retracts: the original is the survivor."""
    for _ in range(3):
        rec.record_generation(
            op_id="op-8", prompt="p",
            generation_result=_FakeGenerationResult(candidates=(_candidate("same", _A),)),
        )
    rec.record_retraction(op_id="op-8", candidate_hashes=("hash-same",))
    rec.record_retraction(op_id="op-8", candidate_hashes=("hash-same",))
    rec.record_outcome(op_id="op-8", terminal_phase="COMPLETED", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert len(rows) == 1 and rows[0]["metadata"]["attempt_index"] == 0
    assert rec.stats()["generations_retracted"] == 2
