"""Tests for multi_file_coverage_gate — Iron Gate 5.

Covers the Session O (bt-2026-04-15-175547) failure mode where a
multi-target op returned the legacy single-file schema and only 1 of N
target files landed on disk. The gate rejects any candidate that
covers fewer than all target files via a populated ``files: [...]``
list when the op targets more than one file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.multi_file_coverage_gate import (
    REASON_PREFIX,
    _candidate_paths,
    _normalize_path,
    check_candidate,
    is_enabled,
    render_missing_block,
    subset_coverage_enabled,
)


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


class TestIsEnabled:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_MULTI_FILE_ENFORCEMENT", raising=False)
        monkeypatch.delenv("JARVIS_MULTI_FILE_GEN_ENABLED", raising=False)
        assert is_enabled() is True

    @pytest.mark.parametrize("val", ["false", "FALSE", "0", "no", "off"])
    def test_explicit_off(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_MULTI_FILE_ENFORCEMENT", val)
        monkeypatch.delenv("JARVIS_MULTI_FILE_GEN_ENABLED", raising=False)
        assert is_enabled() is False

    def test_off_when_multi_gen_disabled(self, monkeypatch):
        """No point enforcing coverage for a shape the orchestrator
        is refusing to honor at APPLY."""
        monkeypatch.delenv("JARVIS_MULTI_FILE_ENFORCEMENT", raising=False)
        monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "false")
        assert is_enabled() is False


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


class TestNormalizePath:
    def test_strips_dot_slash(self):
        assert _normalize_path("./foo/bar.py") == "foo/bar.py"

    def test_collapses_duplicate_slashes(self):
        assert _normalize_path("foo//bar.py") == "foo/bar.py"

    def test_empty_returns_empty(self):
        assert _normalize_path("") == ""

    def test_absolute_outside_root_kept_absolute(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "x.py"
        result = _normalize_path(str(outside), project_root=root)
        # Path is outside root; _normalize_path keeps it absolute so the
        # coverage comparison against repo-relative target paths fails.
        assert result == str(outside)

    def test_absolute_inside_root_normalized_to_relpath(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        inside = root / "tests" / "foo.py"
        inside.parent.mkdir()
        inside.touch()
        result = _normalize_path(str(inside), project_root=root)
        assert result == "tests/foo.py"


# ---------------------------------------------------------------------------
# Candidate path extraction
# ---------------------------------------------------------------------------


class TestCandidatePaths:
    def test_files_list_wins_when_populated(self):
        candidate = {
            "file_path": "legacy.py",
            "full_content": "x = 1",
            "files": [
                {"file_path": "a.py", "full_content": "a = 1"},
                {"file_path": "b.py", "full_content": "b = 2"},
            ],
        }
        assert _candidate_paths(candidate, project_root=None) == {"a.py", "b.py"}

    def test_legacy_fallback_when_files_absent(self):
        candidate = {"file_path": "only.py", "full_content": "x = 1"}
        assert _candidate_paths(candidate, project_root=None) == {"only.py"}

    def test_empty_files_list_falls_back_to_legacy(self):
        candidate = {"file_path": "fb.py", "full_content": "x = 1", "files": []}
        assert _candidate_paths(candidate, project_root=None) == {"fb.py"}

    def test_files_entries_without_content_are_skipped(self):
        candidate = {
            "files": [
                {"file_path": "good.py", "full_content": "ok"},
                {"file_path": "bad.py"},  # missing full_content
                {"full_content": "no path"},  # missing file_path
                {"file_path": "also_bad.py", "full_content": None},
            ],
        }
        assert _candidate_paths(candidate, project_root=None) == {"good.py"}


# ---------------------------------------------------------------------------
# Core gate logic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_gate(monkeypatch):
    """Keep the gate enabled for every test unless individually overridden."""
    monkeypatch.delenv("JARVIS_MULTI_FILE_ENFORCEMENT", raising=False)
    monkeypatch.delenv("JARVIS_MULTI_FILE_GEN_ENABLED", raising=False)


class TestCheckCandidate:
    def test_single_target_no_op(self):
        """Single-target ops are out of scope — legacy shape is fine."""
        candidate = {"file_path": "only.py", "full_content": "x = 1"}
        assert check_candidate(candidate, ["only.py"]) is None

    def test_zero_targets_no_op(self):
        candidate = {"file_path": "whatever.py", "full_content": "x = 1"}
        assert check_candidate(candidate, []) is None

    def test_multi_target_full_coverage_passes(self):
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "a = 1"},
                {"file_path": "b.py", "full_content": "b = 2"},
                {"file_path": "c.py", "full_content": "c = 3"},
            ],
        }
        assert check_candidate(candidate, ["a.py", "b.py", "c.py"]) is None

    def test_multi_target_legacy_shape_rejected(self):
        """Session O reproduction — legacy single-file shape on a 4-file op."""
        candidate = {
            "file_path": "tests/governance/intake/sensors/"
                         "test_test_failure_sensor_dedup.py",
            "full_content": "def test_x(): pass",
        }
        targets = [
            "tests/governance/intake/sensors/test_test_failure_sensor_dedup.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_ttl.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_isolation.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_marker_refresh.py",
        ]
        result = check_candidate(candidate, targets)
        assert result is not None
        reason, missing = result
        assert reason.startswith(REASON_PREFIX)
        assert "1/4" in reason
        # The one path the candidate did cover is NOT in missing.
        assert targets[0] not in missing
        # The other three are.
        assert len(missing) == 3
        for expected in targets[1:]:
            assert expected in missing

    def test_multi_target_partial_files_list_rejected(self):
        """Candidate has a files list but omits one target."""
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "a = 1"},
                {"file_path": "b.py", "full_content": "b = 2"},
            ],
        }
        result = check_candidate(candidate, ["a.py", "b.py", "c.py"])
        assert result is not None
        reason, missing = result
        assert "2/3" in reason
        assert missing == ["c.py"]

    def test_multi_target_extra_paths_in_files_list_ok(self):
        """If the candidate covers all targets plus extras, that's fine.
        Over-coverage is a different concern (blast radius), not this gate's."""
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "a = 1"},
                {"file_path": "b.py", "full_content": "b = 2"},
                {"file_path": "c.py", "full_content": "c = 3"},
                {"file_path": "bonus.py", "full_content": "# extra"},
            ],
        }
        assert check_candidate(candidate, ["a.py", "b.py", "c.py"]) is None

    def test_dot_slash_targets_normalized(self):
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "a"},
                {"file_path": "b.py", "full_content": "b"},
            ],
        }
        assert check_candidate(candidate, ["./a.py", "./b.py"]) is None

    def test_disabled_gate_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MULTI_FILE_ENFORCEMENT", "false")
        candidate = {"file_path": "a.py", "full_content": "a"}
        result = check_candidate(candidate, ["a.py", "b.py", "c.py"])
        assert result is None

    def test_disabled_via_multi_gen_switch(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "false")
        candidate = {"file_path": "a.py", "full_content": "a"}
        assert check_candidate(candidate, ["a.py", "b.py"]) is None

    def test_absolute_path_inside_project_matches_relpath_target(self, tmp_path):
        """Model returns absolute paths, targets are repo-relative — match
        via _normalize_path resolving abspath against project_root.
        """
        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.py").touch()
        (root / "b.py").touch()
        candidate = {
            "files": [
                {"file_path": str(root / "a.py"), "full_content": "a"},
                {"file_path": str(root / "b.py"), "full_content": "b"},
            ],
        }
        result = check_candidate(candidate, ["a.py", "b.py"], project_root=root)
        assert result is None


# ---------------------------------------------------------------------------
# Retry feedback rendering
# ---------------------------------------------------------------------------


class TestRenderMissingBlock:
    def test_names_missing_paths_in_target_order(self):
        missing = ["c.py", "a.py"]  # arbitrary order
        targets = ["a.py", "b.py", "c.py"]
        block = render_missing_block(missing, targets)
        # Ordered by target_files input order — a.py first, c.py second.
        lines = [ln for ln in block.split("\n") if ln.strip().startswith("-")]
        assert lines == ["  - a.py", "  - c.py"]

    def test_caps_at_sixteen(self):
        missing = [f"f{i}.py" for i in range(30)]
        targets = missing[:]
        block = render_missing_block(missing, targets)
        listed = [ln for ln in block.split("\n") if ln.strip().startswith("-")]
        assert len(listed) == 16

    def test_falls_back_to_missing_when_no_target_match(self):
        """Robust to callers that pass pre-normalized paths without the
        original target list — we still emit something rather than empty.
        """
        missing = ["abs/only.py"]
        targets: list[str] = []
        block = render_missing_block(missing, targets)
        assert "abs/only.py" in block


# ---------------------------------------------------------------------------
# End-to-end: Session O reproduction
# ---------------------------------------------------------------------------


class TestSessionOReproduction:
    """The exact failure mode from bt-2026-04-15-175547, Session O."""

    def test_session_o_legacy_shape_caught_pre_apply(self):
        targets = [
            "tests/governance/intake/sensors/test_test_failure_sensor_dedup.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_ttl.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_isolation.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_marker_refresh.py",
        ]
        # The candidate that landed in Session O — only the first target.
        candidate = {
            "candidate_id": "c1",
            "file_path": targets[0],
            "full_content": "def test_dedup(): pass",
            "rationale": "first test",
        }
        result = check_candidate(candidate, targets)
        assert result is not None
        reason, missing = result
        assert reason.startswith(REASON_PREFIX)
        # The 3 missing files from Session O.
        assert set(missing) == set(targets[1:])

    def test_session_o_fix_accepted(self):
        """The SHAPE the gate wants: files: [...] covering all 4 paths."""
        targets = [
            "tests/governance/intake/sensors/test_test_failure_sensor_dedup.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_ttl.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_isolation.py",
            "tests/governance/intake/sensors/test_test_failure_sensor_marker_refresh.py",
        ]
        candidate = {
            "candidate_id": "c1",
            "files": [
                {"file_path": t, "full_content": f"# content {i}\n"}
                for i, t in enumerate(targets)
            ],
            "rationale": "four-file sensor test suite",
        }
        assert check_candidate(candidate, targets) is None


# ---------------------------------------------------------------------------
# Slice 7 — subset semantics for resolved-attribution scope (Run #17)
# ---------------------------------------------------------------------------

_SOURCE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"
_TEST = "tests/governance/a1_ignition_vector/test_leaf_predicates.py"
_RESOLVED = json.dumps({"attribution": {
    "status": "resolved", "test_locus": _TEST, "method": "direct_import",
}})
_UNRESOLVED = json.dumps({"attribution": {
    "status": "unresolved", "reason": "no_first_party_source_imports",
}})


class TestSubsetCoverageSemantics:
    """Run #17 blocker: attributed scope is PERMISSIVE (either locus may
    be the fix target), not an exhaustive change-set. Resolved
    attribution ⇒ covering >=1 target suffices; everything else keeps
    the strict superset demand byte-identically."""

    def test_resolved_source_only_candidate_passes(self):
        """THE Run #17 repro: correct source-only repair on a 2-file scope."""
        cand = {"file_path": _SOURCE, "full_content": "def clamp01(x):\n    return min(1.0, max(0.0, x))\n"}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_test_only_candidate_passes(self):
        """The test itself may legitimately be the fix target — the
        unresolved-only scope gate (Slice 6 T5) intentionally does not
        fire on resolved attribution, so the coverage gate must not
        re-block it either."""
        cand = {"file_path": _TEST, "full_content": "def test_clamp01():\n    assert True\n"}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_full_coverage_still_passes(self):
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": _TEST, "full_content": "b = 2\n"},
        ]}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_resolved_zero_target_coverage_still_rejected(self):
        """Subset never means 'anything goes' — a candidate covering NO
        target file is still rejected even with resolved attribution."""
        cand = {"file_path": "backend/somewhere/else.py", "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None
        reason, missing = result
        assert reason.startswith(REASON_PREFIX)
        assert set(missing) == {_SOURCE, _TEST}

    def test_unresolved_attribution_keeps_superset_demand(self):
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_UNRESOLVED,
        )
        assert result is not None and result[0].startswith(REASON_PREFIX)

    def test_absent_evidence_keeps_superset_demand(self):
        """Default kwarg — every pre-Slice-7 caller is byte-identical."""
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        assert check_candidate(cand, [_SOURCE, _TEST]) is not None

    def test_malformed_evidence_fail_closed_strict(self):
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json="{not json",
        )
        assert result is not None

    def test_subset_master_off_keeps_superset_demand(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", "false")
        cand = {"file_path": _SOURCE, "full_content": "x = 1\n"}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None


class TestSubsetCoverageEnabled:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", raising=False)
        assert subset_coverage_enabled() is True

    @pytest.mark.parametrize("val", ["false", "FALSE", "0", "no", "off"])
    def test_explicit_off(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED", val)
        assert subset_coverage_enabled() is False


# ---------------------------------------------------------------------------
# Slice 7 — wiring pins (the T5 wired-but-inert lesson, structurally enforced)
# ---------------------------------------------------------------------------

import ast as _ast

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros" / "governance"


def _mf_check_call_nodes(path: Path):
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, _ast.Name) else getattr(fn, "attr", "")
            if name == "_mf_check":
                out.append(node)
    return out


class TestBothGatePathsForwardEvidence:
    """Slice 6 shipped a Critical where the extracted runner (the
    shipping default) lacked the new gate. This pin makes 'one path
    wired, the other inert' a red test forever."""

    @pytest.mark.parametrize("rel", [
        "orchestrator.py",
        "phase_runners/generate_runner.py",
    ])
    def test_call_site_passes_intake_evidence(self, rel):
        calls = _mf_check_call_nodes(_GOV / rel)
        assert calls, f"no _mf_check(...) call found in {rel}"
        for call in calls:
            kwargs = {k.arg for k in call.keywords}
            assert "intake_evidence_json" in kwargs, (
                f"{rel}: _mf_check call does not forward "
                "intake_evidence_json — subset semantics inert on this path"
            )
