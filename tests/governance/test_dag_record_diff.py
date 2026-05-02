"""Q2 Slice 6 — dag_record_diff substrate regression suite.

Covers:

  §1   closed taxonomies (DiffOutcome 5-value, ChangeKind 3-value)
  §2   identity diff → OK with empty changes
  §3   added / removed / modified leaf classification
  §4   nested mapping diff with JSON-pointer-style paths
  §5   list element diffing by index
  §6   type mismatch (mapping vs list at same key) classified
  §7   empty / None inputs → EMPTY outcome
  §8   non-Mapping top-level → INVALID
  §9   bounded computation (depth + leaf caps); TRUNCATED outcome
  §10  truncated diff still returns structured payload
  §11  to_dict round-trip + JSON serialization
  §12  defensive: never-raises on garbage values
  §13  AST authority pins (pure stdlib only)
  §14  handle_dag_diff handler wraps substrate cleanly
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification.dag_record_diff import (
    DAG_RECORD_DIFF_SCHEMA_VERSION,
    ChangeKind,
    DiffOutcome,
    FieldChange,
    RecordDiff,
    compute_record_diff,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "verification"
    / "dag_record_diff.py"
)


# ============================================================================
# §1 — Closed taxonomies
# ============================================================================


class TestClosedTaxonomies:
    def test_diff_outcome_has_five_values(self):
        assert {o.value for o in DiffOutcome} == {
            "ok", "empty", "truncated", "invalid", "failed",
        }

    def test_change_kind_has_three_values(self):
        assert {k.value for k in ChangeKind} == {
            "added", "removed", "modified",
        }

    def test_schema_version_canonical(self):
        assert DAG_RECORD_DIFF_SCHEMA_VERSION == "dag_record_diff.1"


# ============================================================================
# §2 — Identity
# ============================================================================


class TestIdentity:
    def test_identical_records_yield_zero_changes(self):
        rec = {"phase": "plan", "op_id": "op-1", "data": {"x": 1}}
        d = compute_record_diff(record_a=rec, record_b=dict(rec))
        assert d.outcome is DiffOutcome.OK
        assert d.changes == ()
        assert d.fields_changed == 0
        assert d.fields_total > 0

    def test_record_ids_passed_through(self):
        d = compute_record_diff(
            record_a={"x": 1}, record_b={"x": 1},
            record_id_a="r-a", record_id_b="r-b",
        )
        assert d.record_id_a == "r-a"
        assert d.record_id_b == "r-b"


# ============================================================================
# §3 — Leaf-level classification
# ============================================================================


class TestLeafClassification:
    def test_modified_value(self):
        d = compute_record_diff(
            record_a={"phase": "plan"},
            record_b={"phase": "generate"},
        )
        assert d.outcome is DiffOutcome.OK
        kinds = {c.kind for c in d.changes}
        assert kinds == {ChangeKind.MODIFIED}
        change = d.changes[0]
        assert change.path == ("phase",)
        assert "plan" in change.value_a_repr
        assert "generate" in change.value_b_repr

    def test_added_key(self):
        d = compute_record_diff(
            record_a={"x": 1},
            record_b={"x": 1, "y": 2},
        )
        added = [c for c in d.changes if c.kind is ChangeKind.ADDED]
        assert len(added) == 1
        assert added[0].path == ("y",)
        assert "2" in added[0].value_b_repr
        assert added[0].value_a_repr == ""

    def test_removed_key(self):
        d = compute_record_diff(
            record_a={"x": 1, "y": 2},
            record_b={"x": 1},
        )
        removed = [c for c in d.changes if c.kind is ChangeKind.REMOVED]
        assert len(removed) == 1
        assert removed[0].path == ("y",)
        assert "2" in removed[0].value_a_repr
        assert removed[0].value_b_repr == ""

    def test_mixed_changes(self):
        a = {"keep": 1, "modify": "old", "remove": "x"}
        b = {"keep": 1, "modify": "new", "add": "y"}
        d = compute_record_diff(record_a=a, record_b=b)
        kinds = sorted([c.kind.value for c in d.changes])
        assert kinds == ["added", "modified", "removed"]


# ============================================================================
# §4 — Nested paths
# ============================================================================


class TestNestedPaths:
    def test_nested_modification_carries_full_path(self):
        a = {"outer": {"middle": {"inner": "old"}}}
        b = {"outer": {"middle": {"inner": "new"}}}
        d = compute_record_diff(record_a=a, record_b=b)
        assert len(d.changes) == 1
        assert d.changes[0].path == ("outer", "middle", "inner")
        assert d.changes[0].kind is ChangeKind.MODIFIED

    def test_paths_are_string_segments_only(self):
        d = compute_record_diff(
            record_a={"a": {"b": 1}},
            record_b={"a": {"b": 2}},
        )
        for seg in d.changes[0].path:
            assert isinstance(seg, str)


# ============================================================================
# §5 — Lists
# ============================================================================


class TestLists:
    def test_list_element_modified_by_index(self):
        d = compute_record_diff(
            record_a={"results": ["a", "b", "c"]},
            record_b={"results": ["a", "X", "c"]},
        )
        assert len(d.changes) == 1
        assert d.changes[0].path == ("results", "1")
        assert d.changes[0].kind is ChangeKind.MODIFIED

    def test_list_element_added_at_end(self):
        d = compute_record_diff(
            record_a={"results": ["a", "b"]},
            record_b={"results": ["a", "b", "c"]},
        )
        adds = [c for c in d.changes if c.kind is ChangeKind.ADDED]
        assert len(adds) == 1
        assert adds[0].path == ("results", "2")

    def test_list_element_removed_from_end(self):
        d = compute_record_diff(
            record_a={"results": ["a", "b", "c"]},
            record_b={"results": ["a", "b"]},
        )
        rems = [c for c in d.changes if c.kind is ChangeKind.REMOVED]
        assert len(rems) == 1
        assert rems[0].path == ("results", "2")


# ============================================================================
# §6 — Type mismatch
# ============================================================================


class TestTypeMismatch:
    def test_mapping_vs_list_at_same_key_modified(self):
        d = compute_record_diff(
            record_a={"x": {"k": 1}},
            record_b={"x": [1, 2]},
        )
        # Top-level diff sees x's value type changed → MODIFIED
        assert d.outcome is DiffOutcome.OK
        assert any(
            c.kind is ChangeKind.MODIFIED and c.path == ("x",)
            for c in d.changes
        )

    def test_scalar_vs_mapping_at_same_key(self):
        d = compute_record_diff(
            record_a={"x": "scalar"},
            record_b={"x": {"k": 1}},
        )
        assert any(
            c.kind is ChangeKind.MODIFIED and c.path == ("x",)
            for c in d.changes
        )


# ============================================================================
# §7+8 — Empty / None / non-Mapping
# ============================================================================


class TestEmptyAndInvalid:
    def test_both_empty_dicts(self):
        d = compute_record_diff(record_a={}, record_b={})
        assert d.outcome is DiffOutcome.EMPTY
        assert d.changes == ()

    def test_both_none(self):
        d = compute_record_diff(record_a=None, record_b=None)
        assert d.outcome is DiffOutcome.EMPTY

    def test_a_none_b_populated_yields_added(self):
        # None → empty mapping, all keys added
        d = compute_record_diff(record_a=None, record_b={"x": 1})
        assert d.outcome is DiffOutcome.OK
        assert any(c.kind is ChangeKind.ADDED for c in d.changes)

    def test_a_string_b_dict_invalid(self):
        d = compute_record_diff(record_a="oops", record_b={})
        assert d.outcome is DiffOutcome.INVALID
        assert "non-Mapping" in d.detail

    def test_a_list_b_dict_invalid(self):
        d = compute_record_diff(record_a=[1, 2, 3], record_b={})
        assert d.outcome is DiffOutcome.INVALID


# ============================================================================
# §9+10 — Bounded computation
# ============================================================================


class TestBoundedComputation:
    def test_depth_cap_treats_subtree_as_opaque(self):
        # Build a deeply nested structure beyond the cap
        deep_a: dict = {}
        deep_b: dict = {}
        cur_a, cur_b = deep_a, deep_b
        for i in range(20):
            cur_a["next"] = {"i": i}
            cur_b["next"] = {"i": i + 100}  # always different
            cur_a = cur_a["next"]
            cur_b = cur_b["next"]
        d = compute_record_diff(
            record_a=deep_a, record_b=deep_b, max_depth=3,
        )
        # Substrate must NOT crash; should classify as MODIFIED at
        # the depth boundary.
        assert d.outcome in (DiffOutcome.OK, DiffOutcome.TRUNCATED)
        assert d.fields_changed > 0

    def test_leaf_cap_yields_truncated(self):
        # Force truncation by exceeding the max_leaves cap
        a = {f"k{i}": f"v_a_{i}" for i in range(50)}
        b = {f"k{i}": f"v_b_{i}" for i in range(50)}
        d = compute_record_diff(
            record_a=a, record_b=b, max_leaves=10,
        )
        assert d.outcome is DiffOutcome.TRUNCATED
        # changes capped at the limit
        assert len(d.changes) == 10
        assert d.fields_changed == 10
        assert "truncated" in d.detail.lower() or "narrow scope" in d.detail.lower()

    def test_below_cap_yields_ok(self):
        a = {"k1": 1, "k2": 2}
        b = {"k1": 1, "k2": 99}
        d = compute_record_diff(
            record_a=a, record_b=b, max_leaves=10,
        )
        assert d.outcome is DiffOutcome.OK


# ============================================================================
# §11 — Serialization
# ============================================================================


class TestSerialization:
    def test_to_dict_round_trip_through_json(self):
        d = compute_record_diff(
            record_a={"phase": "plan", "x": 1},
            record_b={"phase": "generate", "x": 2, "y": 3},
            record_id_a="r-a", record_id_b="r-b",
        )
        s = json.dumps(d.to_dict())
        loaded = json.loads(s)
        assert loaded["outcome"] == "ok"
        assert loaded["record_id_a"] == "r-a"
        assert loaded["record_id_b"] == "r-b"
        assert "schema_version" in loaded
        assert loaded["schema_version"] == "dag_record_diff.1"

    def test_field_change_to_dict(self):
        fc = FieldChange(
            path=("a", "b"), kind=ChangeKind.MODIFIED,
            value_a_repr="1", value_b_repr="2",
        )
        d = fc.to_dict()
        assert d["path"] == ["a", "b"]
        assert d["kind"] == "modified"
        json.dumps(d)  # must serialize


# ============================================================================
# §12 — Defensive: never raises
# ============================================================================


class TestDefensive:
    def test_unrepr_value_handled_via_safe_repr(self):
        class _BadRepr:
            def __repr__(self):
                raise RuntimeError("simulated repr failure")
        # Don't crash even if a leaf's repr blows up
        d = compute_record_diff(
            record_a={"x": _BadRepr()}, record_b={"x": "ok"},
        )
        # Either MODIFIED is emitted with a fallback repr OR the
        # outcome is OK with a safe representation; what we assert
        # is that we got a structured RecordDiff back, not a raise.
        assert isinstance(d, RecordDiff)
        assert d.outcome in (
            DiffOutcome.OK, DiffOutcome.FAILED,
        )

    def test_int_inputs_yield_invalid_not_raise(self):
        d = compute_record_diff(record_a=42, record_b=42)
        assert isinstance(d, RecordDiff)
        # 42 is non-Mapping → INVALID
        assert d.outcome is DiffOutcome.INVALID


# ============================================================================
# §13 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
    "causality_dag", "dag_navigation", "decision_runtime",
)
_SUBPROC_TOKENS = ("subprocess" + ".", "os." + "system", "popen")
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.", "Path(", "pathlib",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_authority_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_pure_stdlib_only(self, source):
        """Slice 6 substrate is stdlib-only — zero governance
        imports. Pinned: any governance.* import at top level OR
        function-level violates the substrate isolation."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "governance" not in module, (
                    f"substrate must be stdlib-only, found "
                    f"governance import: {module}"
                )

    def test_no_filesystem_io(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )


# ============================================================================
# §14 — handle_dag_diff handler
# ============================================================================


class TestHandlerWrap:
    def test_handler_returns_error_dict_when_records_missing(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_DAG_NAVIGATION_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DAG_QUERY_ENABLED", "true")
        from backend.core.ouroboros.governance.verification.dag_navigation import (
            handle_dag_diff,
        )
        out = handle_dag_diff("r-missing-a", "r-missing-b", session_id="bt-x")
        assert out.get("error") is True
        assert out.get("reason_code") == "dag_navigation.not_found"
        assert "missing" in out

    def test_handler_returns_disabled_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_DAG_NAVIGATION_ENABLED", "false")
        from backend.core.ouroboros.governance.verification.dag_navigation import (
            handle_dag_diff,
        )
        out = handle_dag_diff("r-a", "r-b", session_id="bt-x")
        assert out.get("error") is True
        assert out.get("reason_code") == "dag_navigation.disabled"
