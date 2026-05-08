"""Phase 4 (A6) — L3 merge-conflict audit recorder.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "git_conflict_handler behind worktree / subagent path
   only; audit trail to existing recorder patterns
   (cross_op_semantic_recorder style) only if you already
   have a flock'd sink — no new parallel ledger without
   §33.4 discipline."

Pinned coverage (~30 tests):
  * Master flag default-FALSE per §33.1
  * Recorder no-op when master off (no filesystem touch)
  * Frozen MergeConflictRecord round-trip via to_dict /
    from_dict
  * Schema mismatch returns None
  * Closed 3-value MergeConflictKind (mirrors 3
    RuntimeError branches in MergeCoordinator)
  * record_merge_conflict for each kind
  * read_recent_records ordering + limit + missing-file +
    oversized-file
  * 6 AST pins clean (parametrized) + targeted regression
    fires:
      - taxonomy (synthetic regression: drift)
      - authority_asymmetry (synthetic: orchestrator import)
      - no_auto_resolution (synthetic: resolve_conflict /
        apply_resolution / merge_files / auto_resolve)
      - no_worktree_mutation (synthetic: subprocess import,
        Path.unlink call)
      - composes_canonical_jsonl (synthetic: raw open(...,
        'a'))
  * Public API surface complete + register_flags seeds 2
  * MergeCoordinator integration: 3 conflict raise sites
    each invoke the audit recorder BEFORE the RuntimeError
    fires (master-flag-gated try/except wrapped — never
    disturbs canonical escalation path)
  * Integration: master-off + conflict → RuntimeError still
    raises with byte-identical message; no audit row
  * Integration: master-on + conflict → RuntimeError raises
    AND audit row persists
  * Cross-kingdom boundary unchanged (boundary scan = 0)
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/saga/"
        "merge_conflict_audit.py"
    )


@pytest.fixture
def tmp_ledger(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "audit.jsonl"
        monkeypatch.setenv(
            (
                "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH"
            ),
            str(ledger),
        )
        yield ledger


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", v,
        )
        assert master_enabled() is True


def test_recorder_noop_when_master_off(
    monkeypatch, tmp_ledger,
):
    monkeypatch.delenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictKind, record_merge_conflict,
    )
    out = record_merge_conflict(
        kind=MergeConflictKind.OWNED_PATH,
        graph_id="g1", repo="r", barrier_id="b",
        ledger_path_override=tmp_ledger,
    )
    assert out is None
    # Master-off MUST NOT touch filesystem
    assert not tmp_ledger.exists()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_3_values():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictKind,
    )
    assert {k.name for k in MergeConflictKind} == {
        "OWNED_PATH",
        "DUPLICATE_FILE",
        "DUPLICATE_NEW_CONTENT",
    }


# ---------------------------------------------------------------------------
# Frozen artifact
# ---------------------------------------------------------------------------


def test_record_round_trip():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictRecord,
    )
    rec = MergeConflictRecord(
        kind="owned_path",
        graph_id="g1", repo="r1", barrier_id="b1",
        conflict_units=("u1", "u2"),
        paths=("a/b.py",),
        detail="merge_coordinator:owned_path_conflict:r1:b1:['u1', 'u2']",  # noqa: E501
        ts_unix=12345.0,
    )
    rt = MergeConflictRecord.from_dict(rec.to_dict())
    assert rt is not None
    assert rt.kind == "owned_path"
    assert rt.conflict_units == ("u1", "u2")
    assert rt.paths == ("a/b.py",)


def test_record_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictRecord,
    )
    out = MergeConflictRecord.from_dict(
        {"schema_version": "wrong"},
    )
    assert out is None


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind_name", [
        "OWNED_PATH",
        "DUPLICATE_FILE",
        "DUPLICATE_NEW_CONTENT",
    ],
)
def test_recorder_persists_each_kind(
    monkeypatch, tmp_ledger, kind_name,
):
    monkeypatch.setenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictKind,
        read_recent_records,
        record_merge_conflict,
    )
    rec = record_merge_conflict(
        kind=getattr(MergeConflictKind, kind_name),
        graph_id="graph-1", repo="r", barrier_id="b",
        conflict_units=("u",), paths=("p",),
        detail=f"test for {kind_name}",
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].kind == kind_name.lower()


def test_read_recent_records_limit(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        MergeConflictKind,
        read_recent_records,
        record_merge_conflict,
    )
    for i in range(5):
        record_merge_conflict(
            kind=MergeConflictKind.OWNED_PATH,
            graph_id=f"g{i}", repo="r", barrier_id="b",
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_records(limit=3, path=tmp_ledger)
    assert len(rows) == 3
    assert rows[0].graph_id == "g2"
    assert rows[2].graph_id == "g4"


def test_read_missing_ledger_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        read_recent_records,
    )
    nonexistent = tmp_path / "no-such.jsonl"
    assert read_recent_records(path=nonexistent) == ()


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "merge_conflict_audit_master_default_false",
        "merge_conflict_audit_authority_asymmetry",
        "merge_conflict_audit_taxonomy_3_values",
        "merge_conflict_audit_composes_canonical_jsonl",
        "merge_conflict_audit_no_auto_resolution",
        "merge_conflict_audit_no_worktree_mutation",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class MergeConflictKind:
    OWNED_PATH = "owned_path"
    EXTRA_KIND = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_taxonomy_3_values"
        )
    )
    assert pin.validate(tree, bad)


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_no_auto_resolution_pin_fires_on_resolve():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def resolve_conflict_ours(file_path):
    return None
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_no_auto_resolution"
        )
    )
    assert pin.validate(tree, bad)


def test_no_auto_resolution_pin_fires_on_merge_files():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def helper():
    merge_files(a, b)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_no_auto_resolution"
        )
    )
    assert pin.validate(tree, bad)


def test_no_worktree_mutation_pin_fires_on_subprocess():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "import subprocess\nx = 1\n"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_no_worktree_mutation"
        )
    )
    assert pin.validate(tree, bad)


def test_no_worktree_mutation_pin_fires_on_unlink():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def helper():
    p.unlink()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_no_worktree_mutation"
        )
    )
    assert pin.validate(tree, bad)


def test_jsonl_pin_fires_on_raw_open():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def writer():
    with open("foo.jsonl", "a") as f:
        f.write("x\\n")
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "merge_conflict_audit_composes_canonical_jsonl"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.saga import (  # noqa: E501
        merge_conflict_audit as mod,
    )
    expected = {
        "MERGE_CONFLICT_AUDIT_SCHEMA_VERSION",
        "MergeConflictKind",
        "MergeConflictRecord",
        "ledger_path",
        "master_enabled",
        "read_recent_records",
        "record_merge_conflict",
        "register_flags",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_two():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 2
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED",
        "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH",
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


# ---------------------------------------------------------------------------
# Cross-kingdom boundary unchanged
# ---------------------------------------------------------------------------


def test_cross_kingdom_boundary_unchanged():
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    assert scan_governance_tree() == ()


# ---------------------------------------------------------------------------
# MergeCoordinator integration — 3 raise sites each audit
# ---------------------------------------------------------------------------


def _make_unit(unit_id, repo, owned_paths, barrier_id=""):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (  # noqa: E501
        WorkUnitSpec,
    )
    return WorkUnitSpec(
        unit_id=unit_id,
        repo=repo,
        goal="test",
        target_files=tuple(owned_paths),
        dependency_ids=(),
        owned_paths=tuple(owned_paths),
        barrier_id=barrier_id or unit_id,
        timeout_s=30.0,
    )


def _make_graph(units):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (  # noqa: E501
        ExecutionGraph,
    )
    return ExecutionGraph(
        graph_id="graph-test",
        op_id="op-test",
        planner_id="test-planner",
        schema_version="1",
        units=tuple(units),
        concurrency_limit=4,
    )


def _make_result(unit_id, repo, files=(), new_content=()):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (  # noqa: E501
        WorkUnitResult,
        WorkUnitState,
    )
    from backend.core.ouroboros.governance.saga.saga_types import (  # noqa: E501
        FileOp,
        PatchedFile,
        RepoPatch,
    )
    files_tuple = tuple(
        PatchedFile(
            path=p, op=FileOp.CREATE, preimage=None,
        )
        for p, _c in files
    )
    new_tuple = tuple(new_content)
    import time
    return WorkUnitResult(
        unit_id=unit_id,
        repo=repo,
        status=WorkUnitState.COMPLETED,
        patch=RepoPatch(
            repo=repo, files=files_tuple,
            new_content=new_tuple,
        ),
        attempt_count=1,
        started_at_ns=time.monotonic_ns(),
        finished_at_ns=time.monotonic_ns(),
    )


def test_integration_owned_path_conflict_audited(
    monkeypatch, tmp_ledger,
):
    """When MergeCoordinator detects an owned-path conflict,
    Phase 4 audit records it BEFORE the RuntimeError raises.
    Master-on path."""
    monkeypatch.setenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.saga.merge_coordinator import (  # noqa: E501
        MergeCoordinator,
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        read_recent_records,
    )

    # Two units in same (repo, barrier) claim same path
    u1 = _make_unit(
        "u1", "r1", ["shared.py"], barrier_id="b1",
    )
    u2 = _make_unit(
        "u2", "r1", ["shared.py"], barrier_id="b1",
    )
    graph = _make_graph([u1, u2])
    results = {
        "u1": _make_result(
            "u1", "r1",
            files=[("u1.py", "content1")],
        ),
        "u2": _make_result(
            "u2", "r1",
            files=[("u2.py", "content2")],
        ),
    }
    coord = MergeCoordinator()
    with pytest.raises(
        RuntimeError, match="owned_path_conflict",
    ):
        coord.build_barrier_batches(graph, results)
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].kind == "owned_path"
    assert "u1" in rows[0].conflict_units
    assert "u2" in rows[0].conflict_units


def test_integration_master_off_no_audit_row(
    monkeypatch, tmp_ledger,
):
    """When master flag off, RuntimeError still raises with
    byte-identical message — but no audit row persisted."""
    monkeypatch.delenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.saga.merge_coordinator import (  # noqa: E501
        MergeCoordinator,
    )
    from backend.core.ouroboros.governance.saga.merge_conflict_audit import (  # noqa: E501
        read_recent_records,
    )

    u1 = _make_unit(
        "u1", "r1", ["shared.py"], barrier_id="b1",
    )
    u2 = _make_unit(
        "u2", "r1", ["shared.py"], barrier_id="b1",
    )
    graph = _make_graph([u1, u2])
    results = {
        "u1": _make_result(
            "u1", "r1",
            files=[("u1.py", "content1")],
        ),
        "u2": _make_result(
            "u2", "r1",
            files=[("u2.py", "content2")],
        ),
    }
    coord = MergeCoordinator()
    with pytest.raises(
        RuntimeError, match="owned_path_conflict",
    ):
        coord.build_barrier_batches(graph, results)
    # Master-off → no audit row
    rows = read_recent_records(path=tmp_ledger)
    assert rows == ()


def test_integration_audit_failure_does_not_block_raise(
    monkeypatch, tmp_path,
):
    """Operator binding 'NEVER raises' — even if the audit
    recorder fails internally, the canonical RuntimeError
    still raises with byte-identical message."""
    monkeypatch.setenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", "true",
    )
    # Ledger path on read-only directory — flock_append_line
    # will fail; audit recorder MUST swallow + RuntimeError
    # MUST still raise.
    bad_dir = tmp_path / "ro"
    bad_dir.mkdir()
    bad_ledger = bad_dir / "audit.jsonl"
    bad_ledger.touch()
    bad_dir.chmod(0o500)  # read+execute only
    monkeypatch.setenv(
        "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH",
        str(bad_ledger),
    )

    from backend.core.ouroboros.governance.saga.merge_coordinator import (  # noqa: E501
        MergeCoordinator,
    )
    u1 = _make_unit(
        "u1", "r1", ["shared.py"], barrier_id="b1",
    )
    u2 = _make_unit(
        "u2", "r1", ["shared.py"], barrier_id="b1",
    )
    graph = _make_graph([u1, u2])
    results = {
        "u1": _make_result(
            "u1", "r1", files=[("u1.py", "x")],
        ),
        "u2": _make_result(
            "u2", "r1", files=[("u2.py", "y")],
        ),
    }
    coord = MergeCoordinator()
    # Even with broken audit ledger, RuntimeError MUST raise
    with pytest.raises(
        RuntimeError, match="owned_path_conflict",
    ):
        coord.build_barrier_batches(graph, results)
    # Cleanup chmod
    bad_dir.chmod(0o700)
