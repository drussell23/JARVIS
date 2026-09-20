"""The difflib comparison belongs in a process, and only primitives may cross.

`structural_similarity` -> `difflib.SequenceMatcher.find_longest_match` was
the top loop blocker remaining after psutil and ONNX were offloaded. difflib
is pure Python and holds the GIL throughout, so a thread would not have
helped -- this is what `cpu_bound=True` exists for.

The IPC contract is the delicate part. Candidate dicts carry `full_content`
that can be hundreds of kilobytes each; serialising a group of them would
cost more than the comparison being offloaded. So fingerprints (compact AST
skeletons) and a float threshold cross the boundary, and the verdict comes
back as `(bool, float)`. Nothing else.
"""
from __future__ import annotations

import asyncio
import inspect
import pickle

import pytest

from backend.core.ouroboros.governance import sibling_entropy as ent

FP_A = "FunctionDef:alpha|Return"
FP_B = "FunctionDef:beta|Assign|Return"


# ---------------------------------------------------------------------------
# The IPC contract
# ---------------------------------------------------------------------------


def test_offload_target_is_module_level_and_picklable():
    """cooperative_fs_io's process path rejects bound methods and closures
    at submission -- correctly, since neither pickles."""
    assert pickle.dumps(ent.redundancy_scan)
    assert inspect.getmodule(ent.redundancy_scan) is ent


def test_only_primitives_cross_the_boundary():
    args = ([FP_A], [FP_B], 0.8)
    assert pickle.dumps(args)
    verdict = ent.redundancy_scan(*args)
    assert isinstance(verdict, tuple)
    assert isinstance(verdict[0], bool)
    assert isinstance(verdict[1], float)


def test_candidate_dicts_are_not_part_of_the_contract():
    """The signature must not invite a caller to pass the heavy objects."""
    params = list(inspect.signature(ent.redundancy_scan).parameters)
    assert params == ["new_fingerprints", "seen", "threshold"]


# ---------------------------------------------------------------------------
# Sync and async must agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new, seen",
    [
        ([FP_A], [FP_A]),               # identical -> redundant
        ([FP_A], [FP_B]),               # different
        ([FP_A, FP_B], [FP_A]),         # one repeat, one new
        ([""], [""]),                   # empty fingerprint is a real answer
        ([FP_A], []),                   # nothing seen yet
        ([], [FP_A]),                   # nothing drawn
    ],
)
async def test_async_matches_sync(new, seen):
    """Two implementations of one question would drift; there is one."""
    assert await ent.is_structurally_redundant_async(new, seen) == \
        ent.is_structurally_redundant(new, seen)


@pytest.mark.asyncio
async def test_identical_draw_is_redundant():
    redundant, peak = await ent.is_structurally_redundant_async([FP_A], [FP_A])
    assert redundant is True
    assert peak == 1.0


@pytest.mark.asyncio
async def test_distinct_draw_is_not_redundant():
    redundant, _peak = await ent.is_structurally_redundant_async(
        ["FunctionDef:x|Return"], ["ClassDef:Totally|Different|Shape|Here"],
    )
    assert redundant is False


@pytest.mark.asyncio
async def test_threshold_is_honoured():
    strict = await ent.is_structurally_redundant_async(
        [FP_A], [FP_B], threshold=0.01,
    )
    lax = await ent.is_structurally_redundant_async(
        [FP_A], [FP_B], threshold=0.99,
    )
    assert strict[0] is True
    assert lax[0] is False


# ---------------------------------------------------------------------------
# Degradation: a verdict is required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offload_failure_still_returns_the_right_verdict(monkeypatch):
    """The loop being busy is not a reason to return the wrong answer."""
    import backend.core.ouroboros.governance.cooperative_fs_io as fsio

    async def _boom(*_a, **_k):
        raise RuntimeError("pool down")

    monkeypatch.setattr(fsio, "offload", _boom)
    assert await ent.is_structurally_redundant_async([FP_A], [FP_A]) == (True, 1.0)


@pytest.mark.asyncio
async def test_offload_error_sentinel_falls_back(monkeypatch):
    import backend.core.ouroboros.governance.cooperative_fs_io as fsio

    async def _declined(*_a, **_k):
        return fsio.OffloadError("declined") if hasattr(fsio, "OffloadError") else None

    monkeypatch.setattr(fsio, "offload", _declined)
    redundant, peak = await ent.is_structurally_redundant_async([FP_A], [FP_A])
    assert (redundant, peak) == (True, 1.0)


@pytest.mark.asyncio
async def test_disabled_entropy_short_circuits(monkeypatch):
    monkeypatch.setattr(ent, "entropy_enabled", lambda: False)
    assert await ent.is_structurally_redundant_async([FP_A], [FP_A]) == (False, 0.0)


@pytest.mark.asyncio
async def test_none_fingerprints_are_filtered_not_compared():
    """`is not None`, not truthiness: "" is the fingerprint of a candidate
    that changes nothing, and two of those are the same answer."""
    redundant, _ = await ent.is_structurally_redundant_async(
        [FP_A], [None, FP_A],
    )
    assert redundant is True


@pytest.mark.asyncio
async def test_no_seen_fingerprints_is_not_redundant():
    assert await ent.is_structurally_redundant_async([FP_A], [None]) == (False, 0.0)


# ---------------------------------------------------------------------------
# The caller that was converted
# ---------------------------------------------------------------------------


def test_only_the_async_caller_was_converted():
    """`trajectory_recorder._group_has_distinct_answers` is sync; converting
    it blindly is the mistake already made once this session with an OS
    thread that was already off the loop."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    rec = (
        root / "backend/core/ouroboros/governance/observability/trajectory_recorder.py"
    ).read_text()
    assert "is_structurally_redundant_async" not in rec

    cg = (root / "backend/core/ouroboros/governance/candidate_generator.py").read_text()
    assert "is_structurally_redundant_async" in cg
    ast.parse(cg)
