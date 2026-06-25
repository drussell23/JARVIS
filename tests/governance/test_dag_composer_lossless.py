"""Tests for the DAGComposer zero-loss merge guarantee (Slice 4b upgrade).

Before the composed fan-out candidate is handed to VALIDATE/GATE, the composer
MUST mathematically PROVE that every parallel unit's patch is present,
byte-preserved, and AST-valid -- zero patches dropped or overwritten during the
map-reduce union. This is a VERIFICATION layer over the existing disjoint UNION
(``compose_fanout_result``) -- it proves what the Collision Matrix promised.

Invariants pinned here:

(1) Count conservation: ``len(input_patches) == len(output_files)``.
(2) Content conservation: every input ``(file_path, sha256)`` appears EXACTLY
    once in the output ledger (no content dropped or silently mutated).
(3) No-overwrite: no two inputs map to the same output file_path (collision
    invariant re-proven over the ACTUAL hashes at compose time).
(4) AST validity: each composed ``.py`` file ``ast.parse`` cleanly; non-Python
    files skip AST but stay hash-conserved.
(5) Fail-CLOSED: ANY violation -> ``ComposeFailure(reason="lossless_proof_failed:<which>")``
    -> the caller falls back to legacy serial. No partial merge reaches the gates.
(6) Telemetry: a structured ``[DAGCompose] lossless proof: inputs=N outputs=M
    sha_match=.. ast_ok=..`` line is emitted.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)
from backend.core.ouroboros.governance import dag_composer
from backend.core.ouroboros.governance.dag_composer import (
    ComposeFailure,
    ComposeFailureReason,
    ComposedCandidate,
    compose_fanout_result,
)


# ---------------------------------------------------------------------------
# Builders (mirror test_dag_composer.py)
# ---------------------------------------------------------------------------


def _spec(unit_id: str, file_path: str, goal: str = "") -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="jarvis",
        goal=goal or f"edit {file_path}",
        target_files=(file_path,),
        owned_paths=(file_path,),
    )


def _graph(specs: List[WorkUnitSpec], op_id: str = "op-deadbeef") -> ExecutionGraph:
    return ExecutionGraph(
        graph_id="graph-test01",
        op_id=op_id,
        planner_id="parallel_dispatch.v1",
        schema_version="wave3_item6_slice2.v1",
        units=tuple(specs),
        concurrency_limit=max(2, len(specs)),
    )


def _result(
    unit_id: str,
    file_path: str,
    content: str,
    *,
    status: WorkUnitState = WorkUnitState.COMPLETED,
    with_patch: bool = True,
) -> WorkUnitResult:
    patch = None
    if with_patch:
        patch = RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
            new_content=((file_path, content.encode("utf-8")),),
        )
    now = time.monotonic_ns()
    return WorkUnitResult(
        unit_id=unit_id,
        repo="jarvis",
        status=status,
        patch=patch,
        attempt_count=1,
        started_at_ns=now,
        finished_at_ns=now,
    )


def _three_disjoint_valid() -> Tuple[ExecutionGraph, Dict[str, WorkUnitResult]]:
    specs = [
        _spec("unit-a", "pkg/a.py", goal="add a"),
        _spec("unit-b", "pkg/b.py", goal="add b"),
        _spec("unit-c", "pkg/c.py", goal="add c"),
    ]
    graph = _graph(specs)
    results = {
        "unit-a": _result("unit-a", "pkg/a.py", "# a\ndef a():\n    return 1\n"),
        "unit-b": _result("unit-b", "pkg/b.py", "# b\ndef b():\n    return 2\n"),
        "unit-c": _result("unit-c", "pkg/c.py", "# c\ndef c():\n    return 3\n"),
    }
    return graph, results


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# (1)/(2)/(3) Happy path: lossless proof PASSES, all hashes match.
# ---------------------------------------------------------------------------


def test_three_disjoint_valid_pass_lossless_proof():
    graph, results = _three_disjoint_valid()
    out = compose_fanout_result(graph, results)

    assert isinstance(out, ComposedCandidate)
    assert out.is_failure is False
    cand = out.candidate
    assert len(cand["files"]) == 3

    # Content conservation -- each composed file hashes back to its source.
    by_path = {e["file_path"]: e["full_content"] for e in cand["files"]}
    assert _sha(by_path["pkg/a.py"]) == _sha("# a\ndef a():\n    return 1\n")
    assert _sha(by_path["pkg/b.py"]) == _sha("# b\ndef b():\n    return 2\n")
    assert _sha(by_path["pkg/c.py"]) == _sha("# c\ndef c():\n    return 3\n")


# ---------------------------------------------------------------------------
# (1) Count conservation: a drop (composer that loses a file) -> FAIL.
# ---------------------------------------------------------------------------


def test_dropped_file_fails_count_conservation(monkeypatch):
    """Synthetic drop -- patch the union builder to silently lose one file.
    The lossless proof MUST catch the count mismatch and fail CLOSED."""
    graph, results = _three_disjoint_valid()

    # Monkeypatch the union builder to silently drop one file post-union while
    # keeping the (3-entry) input ledger intact -> count-conservation must trip.
    orig = dag_composer._compose_ordered_files

    def _lossy(graph_, unit_results_):
        res = orig(graph_, unit_results_)
        if isinstance(res, ComposeFailure):
            return res
        ordered, ledger = res
        return ordered[:-1], ledger  # drop one -> count mismatch

    monkeypatch.setattr(dag_composer, "_compose_ordered_files", _lossy)

    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.LOSSLESS_PROOF_FAILED
    assert "count" in out.detail


# ---------------------------------------------------------------------------
# (2) Content conservation: a mutation (hash != source) -> FAIL.
# ---------------------------------------------------------------------------


def test_mutated_content_fails_content_conservation(monkeypatch):
    """Synthetic mutation -- the composed file content is silently changed so
    its sha256 no longer matches the source unit's patch. The proof MUST catch
    it (no silent mutation reaches the gates)."""
    graph, results = _three_disjoint_valid()
    orig = dag_composer._compose_ordered_files

    def _mutate(graph_, unit_results_):
        res = orig(graph_, unit_results_)
        if isinstance(res, ComposeFailure):
            return res
        ordered, ledger = res
        # Corrupt one composed file's content without touching the ledger.
        ordered[0]["full_content"] = ordered[0]["full_content"] + "# TAMPERED\n"
        return ordered, ledger

    monkeypatch.setattr(dag_composer, "_compose_ordered_files", _mutate)

    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.LOSSLESS_PROOF_FAILED
    assert "content" in out.detail


# ---------------------------------------------------------------------------
# (4) AST validity: a syntactically-broken unit patch -> FAIL.
# ---------------------------------------------------------------------------


def test_syntactically_broken_python_fails_ast_validity():
    graph, results = _three_disjoint_valid()
    # unit-b emits broken Python.
    results["unit-b"] = _result("unit-b", "pkg/b.py", "def b(:\n    pass\n")
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.LOSSLESS_PROOF_FAILED
    assert "ast" in out.detail
    assert out.offending_unit_id == "unit-b" or "pkg/b.py" in out.detail


# ---------------------------------------------------------------------------
# (4) Non-Python files: hash-conserved, AST skipped, PASS.
# ---------------------------------------------------------------------------


def test_non_python_files_skip_ast_but_hash_conserved():
    specs = [
        _spec("unit-a", "docs/readme.md", goal="docs"),
        _spec("unit-b", "config/data.json", goal="config"),
    ]
    graph = _graph(specs)
    results = {
        # Content that would NOT parse as Python -- must not be AST-checked.
        "unit-a": _result("unit-a", "docs/readme.md", "# Title\nnot:python at all{\n"),
        "unit-b": _result("unit-b", "config/data.json", '{"k": [1, 2,}\n'),
    }
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposedCandidate)
    by_path = {e["file_path"]: e["full_content"] for e in out.candidate["files"]}
    assert _sha(by_path["docs/readme.md"]) == _sha("# Title\nnot:python at all{\n")
    assert _sha(by_path["config/data.json"]) == _sha('{"k": [1, 2,}\n')


# ---------------------------------------------------------------------------
# (3) No-overwrite re-proven over actual hashes -> still collision fail.
# ---------------------------------------------------------------------------


def test_same_file_still_fails_closed_collision():
    specs = [
        _spec("unit-a", "pkg/shared.py"),
        _spec("unit-b", "pkg/shared.py"),
    ]
    graph = _graph(specs)
    results = {
        "unit-a": _result("unit-a", "pkg/shared.py", "# a\npass\n"),
        "unit-b": _result("unit-b", "pkg/shared.py", "# b\npass\n"),
    }
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    # The union-time collision guard fires first (before the proof), which is
    # also a fail-CLOSED no-overwrite outcome.
    assert out.reason in (
        ComposeFailureReason.COLLISION_INVARIANT_VIOLATED,
        ComposeFailureReason.LOSSLESS_PROOF_FAILED,
    )


# ---------------------------------------------------------------------------
# (6) Telemetry line emits the right counts.
# ---------------------------------------------------------------------------


def test_lossless_proof_telemetry_line_emitted(caplog):
    graph, results = _three_disjoint_valid()
    with caplog.at_level(logging.INFO, logger="Ouroboros.DAGComposer"):
        out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposedCandidate)
    line = next(
        (r.getMessage() for r in caplog.records if "lossless proof" in r.getMessage()),
        None,
    )
    assert line is not None, "expected a [DAGCompose] lossless proof telemetry line"
    assert "inputs=3" in line
    assert "outputs=3" in line
    assert "sha_match=" in line
    assert "ast_ok=" in line


# ---------------------------------------------------------------------------
# Determinism: the proof does not perturb the composed candidate.
# ---------------------------------------------------------------------------


def test_proof_does_not_change_candidate_output():
    graph, results = _three_disjoint_valid()
    out1 = compose_fanout_result(graph, results)
    out2 = compose_fanout_result(graph, results)
    assert isinstance(out1, ComposedCandidate)
    assert isinstance(out2, ComposedCandidate)
    assert out1.candidate == out2.candidate
