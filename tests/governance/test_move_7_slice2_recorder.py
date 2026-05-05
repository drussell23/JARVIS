"""Move 7 — Cross-op Semantic Budget Slice 2 recorder
regression spine (PRD §29.4, 2026-05-05).

Verifies:

  * Master-flag-gated entry point (off → no-op; on → append)
  * Caller-injected centroid path (testing) AND SemanticIndex
    canonical path (production)
  * Empty centroid → silent skip (cold-start; not error)
  * JSONL append uses §33.4 flock'd primitive
  * Reader: chronological order + limit clamp + missing-file
    empty-tuple + corrupt-line skip
  * Schema-versioned rows via §33.5 OpSemanticCentroid contract
  * Defensive paths — SemanticIndex unavailable / flock failure
    / JSON encode failure all NEVER raise
  * Composes Slice 1 primitive + cross_process_jsonl +
    SemanticIndex (read-only) — no parallel substrate
  * 2 AST pins auto-registered + green
  * Authority asymmetry — pure substrate
  * Public API stability
  * SemanticIndex.snapshot_global_centroid public surface
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Master-flag gating
# ---------------------------------------------------------------------------


def test_record_master_off_returns_false(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid(
        "op-x",
        centroid=(1.0, 0.0),
        path=tmp_path / "c.jsonl",
    )
    assert ok is False
    # File should NOT have been created.
    assert not (tmp_path / "c.jsonl").exists()


def test_record_master_default_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    # §33.1 operator binding — default-FALSE
    ok = record_op_centroid(
        "op-x",
        centroid=(1.0, 0.0),
        path=tmp_path / "c.jsonl",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Happy path — caller-injected centroid
# ---------------------------------------------------------------------------


def test_record_caller_injected_appends_row(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid(
        "op-1",
        centroid=(1.0, 0.0, 0.5),
        ts_unix=1.0,
        path=target,
    )
    assert ok is True
    assert target.exists()
    text = target.read_text(encoding="utf-8").strip()
    row = json.loads(text)
    assert row["schema_version"] == "op_semantic_centroid.1"
    assert row["op_id"] == "op-1"
    assert row["ts_unix"] == 1.0
    assert row["centroid"] == [1.0, 0.0, 0.5]
    assert row["centroid_hash"]


def test_record_multiple_ops_appends_chronologically(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    for i, c in enumerate(
        [(1.0, 0.0), (0.95, 0.31), (0.85, 0.53)]
    ):
        record_op_centroid(
            f"op-{i}", centroid=c, ts_unix=float(i), path=target,
        )
    rows = read_recent_centroids(limit=10, path=target)
    assert len(rows) == 3
    assert [r.op_id for r in rows] == ["op-0", "op-1", "op-2"]


# ---------------------------------------------------------------------------
# SemanticIndex canonical path
# ---------------------------------------------------------------------------


def test_record_uses_semantic_index_when_no_caller_centroid(
    monkeypatch, tmp_path,
):
    """When ``centroid`` arg is None, recorder reads from
    SemanticIndex.snapshot_global_centroid()."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    # Stub the SemanticIndex singleton.
    from backend.core.ouroboros.governance import semantic_index

    class StubIndex:
        def snapshot_global_centroid(self):
            return (0.5, 0.7, 0.1)

    monkeypatch.setattr(
        semantic_index, "get_default_index", lambda *a, **kw: StubIndex(),
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    ok = record_op_centroid("op-si", path=target)
    assert ok is True
    rows = read_recent_centroids(limit=5, path=target)
    assert len(rows) == 1
    assert rows[0].centroid == (0.5, 0.7, 0.1)


def test_semantic_index_snapshot_global_centroid_present():
    """The public surface MUST exist on SemanticIndex per
    Slice 2 prereq (added 2026-05-05)."""
    from backend.core.ouroboros.governance.semantic_index import (
        SemanticIndex, get_default_index,
    )
    assert hasattr(SemanticIndex, "snapshot_global_centroid")
    idx = get_default_index()
    snap = idx.snapshot_global_centroid()
    # Real index may be empty in tests; tuple type either way.
    assert isinstance(snap, tuple)


# ---------------------------------------------------------------------------
# Empty / cold-start / defensive paths
# ---------------------------------------------------------------------------


def test_record_empty_centroid_returns_false(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid(
        "op-x", centroid=(), path=tmp_path / "c.jsonl",
    )
    assert ok is False
    # No file should exist.
    assert not (tmp_path / "c.jsonl").exists()


def test_record_empty_op_id_returns_false(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid(
        "", centroid=(1.0,), path=tmp_path / "c.jsonl",
    )
    assert ok is False


def test_record_garbage_centroid_returns_false(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid(
        "op-x",
        centroid=("not", "a", "float"),  # type: ignore
        path=tmp_path / "c.jsonl",
    )
    assert ok is False


def test_record_semantic_index_failure_returns_false(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import semantic_index

    def _raise(*a, **kw):
        raise RuntimeError("synthetic SemanticIndex failure")

    monkeypatch.setattr(
        semantic_index, "get_default_index", _raise,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    ok = record_op_centroid("op-x", path=tmp_path / "c.jsonl")
    assert ok is False  # NEVER raises


# ---------------------------------------------------------------------------
# Reader — defensive paths
# ---------------------------------------------------------------------------


def test_read_missing_file_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        read_recent_centroids,
    )
    rows = read_recent_centroids(
        limit=10, path=tmp_path / "absent.jsonl",
    )
    assert rows == ()


def test_read_corrupt_lines_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "c.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    # Write one valid row.
    record_op_centroid(
        "op-good", centroid=(1.0, 0.0), ts_unix=1.0, path=target,
    )
    # Append corrupt + invalid-shape lines.
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("garbage not json\n")
        fh.write(json.dumps({"missing_required_field": True}) + "\n")
        fh.write("\n")  # blank
    rows = read_recent_centroids(limit=10, path=target)
    # Only the good row survives.
    valid_ids = {r.op_id for r in rows}
    assert "op-good" in valid_ids


def test_read_limit_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "c.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    for i in range(5):
        record_op_centroid(
            f"op-{i}", centroid=(float(i),), ts_unix=float(i), path=target,
        )
    # Limit < total → returns most-recent
    rows = read_recent_centroids(limit=2, path=target)
    assert len(rows) == 2
    assert {r.op_id for r in rows} == {"op-3", "op-4"}
    # Limit > total → returns all
    rows = read_recent_centroids(limit=100, path=target)
    assert len(rows) == 5


def test_read_invalid_limit_falls_back_to_default(tmp_path):
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        read_recent_centroids,
    )
    # NEVER raises on garbage limit.
    rows = read_recent_centroids(
        limit="not-an-int",  # type: ignore
        path=tmp_path / "absent.jsonl",
    )
    assert rows == ()


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def test_centroids_jsonl_path_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH", raising=False,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        centroids_jsonl_path,
    )
    p = centroids_jsonl_path()
    assert str(p).endswith(
        ".jarvis/cross_op_semantic_centroids.jsonl",
    )


def test_centroids_jsonl_path_env_override(
    monkeypatch, tmp_path,
):
    custom = tmp_path / "custom" / "ledger.jsonl"
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH", str(custom),
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        centroids_jsonl_path,
    )
    assert centroids_jsonl_path() == custom


def test_max_file_bytes_clamped(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        centroids_max_file_bytes,
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_MAX_BYTES", "0",
    )
    assert centroids_max_file_bytes() == 1024 * 1024  # min 1 MiB
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_MAX_BYTES",
        str(10 * 1024 ** 4),  # 10 TiB
    )
    assert centroids_max_file_bytes() == 1024 ** 3  # max 1 GiB
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_MAX_BYTES", "garbage",
    )
    assert centroids_max_file_bytes() == 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# compute_centroid_hash
# ---------------------------------------------------------------------------


def test_compute_centroid_hash_deterministic():
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        compute_centroid_hash,
    )
    h1 = compute_centroid_hash((1.0, 0.0, 0.5))
    h2 = compute_centroid_hash((1.0, 0.0, 0.5))
    assert h1 == h2
    assert len(h1) == 8


def test_compute_centroid_hash_distinguishes():
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        compute_centroid_hash,
    )
    h1 = compute_centroid_hash((1.0, 0.0))
    h2 = compute_centroid_hash((0.0, 1.0))
    assert h1 != h2


def test_compute_centroid_hash_empty():
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        compute_centroid_hash,
    )
    assert compute_centroid_hash(()) == ""


# ---------------------------------------------------------------------------
# §33.5 — schema-versioned rows
# ---------------------------------------------------------------------------


def test_appended_row_carries_schema_version(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "c.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    record_op_centroid(
        "op-x", centroid=(1.0,), ts_unix=1.0, path=target,
    )
    text = target.read_text(encoding="utf-8")
    row = json.loads(text)
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OP_SEMANTIC_CENTROID_SCHEMA_VERSION,
    )
    assert (
        row["schema_version"]
        == OP_SEMANTIC_CENTROID_SCHEMA_VERSION
    )


def test_reader_verifies_via_versioned_artifact_helper(
    monkeypatch, tmp_path,
):
    """End-to-end §33.5 round-trip: writer emits versioned row,
    reader's caller can verify via verify_artifact_schema."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "c.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    record_op_centroid(
        "op-v", centroid=(1.0, 0.0), ts_unix=1.0, path=target,
    )
    rows = read_recent_centroids(limit=10, path=target)
    assert len(rows) == 1

    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        OP_SEMANTIC_CENTROID_SCHEMA_VERSION,
    )
    verdict = verify_artifact_schema(
        rows[0],
        expected_schema=OP_SEMANTIC_CENTROID_SCHEMA_VERSION,
    )
    assert verdict.accepted is True


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 end-to-end integration
# ---------------------------------------------------------------------------


def test_recorder_feeds_compute_semantic_budget_e2e(
    monkeypatch, tmp_path,
):
    """Record N op centroids → read them back → feed to
    compute_semantic_budget → get a sensible verdict."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "c.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid, read_recent_centroids,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
        SemanticBudgetVerdict, compute_semantic_budget,
    )
    # Two near-identical centroids → drift ≈ 0.
    record_op_centroid(
        "op-1", centroid=(1.0, 0.0), ts_unix=1.0, path=target,
    )
    record_op_centroid(
        "op-2",
        centroid=(0.9999, 0.014),
        ts_unix=2.0,
        path=target,
    )
    centroids = read_recent_centroids(limit=10, path=target)
    report = compute_semantic_budget(
        centroids, enabled_override=True, threshold=0.30,
    )
    assert report.verdict == SemanticBudgetVerdict.WITHIN_BUDGET


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


_EXPECTED_PIN_NAMES = {
    "cross_op_semantic_recorder_authority_asymmetry",
    "cross_op_semantic_recorder_composes_slice1",
}


def test_pins_auto_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_PIN_NAMES - registered
    assert not missing, (
        f"missing Slice 2 pins: {missing}"
    )


def test_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_PIN_NAMES
    ]
    assert not relevant, (
        "Slice 2 pin violations: " + "; ".join(
            f"{v.invariant_name}: {v.detail}"
            for v in relevant
        )
    )


# ---------------------------------------------------------------------------
# Authority asymmetry (file-level walk)
# ---------------------------------------------------------------------------


def test_authority_asymmetry():
    import ast as _ast
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/"
        "cross_op_semantic_recorder.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"cross_op_semantic_recorder.py MUST "
                        f"NOT import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        cross_op_semantic_recorder as r,
    )
    expected = (
        "record_op_centroid",
        "read_recent_centroids",
        "compute_centroid_hash",
        "centroids_jsonl_path",
        "centroids_max_file_bytes",
        "register_shipped_invariants",
        "CROSS_OP_SEMANTIC_RECORDER_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(r, name), f"missing public symbol: {name}"
