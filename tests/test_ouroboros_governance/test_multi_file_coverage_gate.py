"""Tests for multi_file_coverage_gate — Iron Gate 5.

Covers the Session O (bt-2026-04-15-175547) failure mode where a
multi-target op returned the legacy single-file schema and only 1 of N
target files landed on disk. The gate rejects any candidate that
covers fewer than all target files via a populated ``files: [...]``
list when the op targets more than one file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.multi_file_coverage_gate import (
    REASON_PREFIX,
    _candidate_paths,
    _normalize_path,
    check_candidate,
    is_enabled,
    render_missing_block,
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
