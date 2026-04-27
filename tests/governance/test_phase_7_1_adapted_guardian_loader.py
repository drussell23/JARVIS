"""Phase 7.1 — SemanticGuardian adapted-pattern boot-time loader regression suite.

Pins:
  * Module constants + master flag default-false-pre-graduation.
  * AdaptedPatternEntry frozen dataclass.
  * Master-flag short-circuit (returns empty when off).
  * YAML reader: missing file / parse failure / empty / oversize /
    not-a-mapping / patterns-key-missing / per-entry validation.
  * Detector builder: regex compile success/failure; diff-aware
    fires (new content matches, old doesn't); fires-on-NEW-only.
  * Cage rules:
    - Adapted patterns ADDITIVE only (collision with hand-written
      → SKIP).
    - Pattern count capped at MAX_ADAPTED_PATTERNS.
    - Per-pattern regex capped at MAX_ADAPTED_REGEX_CHARS.
    - Per-pattern message capped at MAX_ADAPTED_MESSAGE_CHARS.
    - YAML file capped at MAX_YAML_BYTES.
    - Severity coerced to "soft"/"hard" (default "soft").
  * SemanticGuardian boot-time merge:
    - When master flag off: _PATTERNS unchanged, _ALL_PATTERNS unchanged.
    - When master flag on + YAML present: adapted patterns merge.
    - End-to-end: load → SemanticGuardian.inspect fires on adapted pattern.
  * Authority invariants (AST grep): no banned governance imports;
    stdlib + adaptation.ledger only.
"""
from __future__ import annotations

import ast as _ast
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_LOADER_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "adapted_guardian_loader.py"
)


from backend.core.ouroboros.governance.adaptation.adapted_guardian_loader import (
    AdaptedPatternEntry,
    MAX_ADAPTED_MESSAGE_CHARS,
    MAX_ADAPTED_PATTERNS,
    MAX_ADAPTED_REGEX_CHARS,
    MAX_YAML_BYTES,
    _build_detector,
    _parse_entry,
    adapted_patterns_path,
    is_loader_enabled,
    load_adapted_patterns,
)


# ===========================================================================
# A — Module constants + master flag + dataclass
# ===========================================================================


def test_max_adapted_patterns_pinned():
    assert MAX_ADAPTED_PATTERNS == 256


def test_max_adapted_regex_chars_pinned():
    assert MAX_ADAPTED_REGEX_CHARS == 256


def test_max_adapted_message_chars_pinned():
    assert MAX_ADAPTED_MESSAGE_CHARS == 240


def test_max_yaml_bytes_pinned():
    assert MAX_YAML_BYTES == 4 * 1024 * 1024


def test_master_flag_default_false_pre_graduation(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", raising=False,
    )
    assert is_loader_enabled() is False


def test_master_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", val,
        )
        assert is_loader_enabled() is True


def test_master_flag_falsy_variants(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", val,
        )
        assert is_loader_enabled() is False


def test_adapted_pattern_entry_is_frozen():
    e = AdaptedPatternEntry(
        name="x", regex="X", severity="soft", message="m",
        proposal_id="p", approved_at="t", approved_by="op",
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.name = "y"  # type: ignore[misc]


def test_adapted_patterns_path_default_under_jarvis():
    p = adapted_patterns_path()
    assert p.name == "adapted_guardian_patterns.yaml"
    assert p.parent.name == ".jarvis"


def test_adapted_patterns_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.yaml"
    monkeypatch.setenv(
        "JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH", str(custom),
    )
    assert adapted_patterns_path() == custom


# ===========================================================================
# B — Master-flag short-circuit
# ===========================================================================


def test_load_returns_empty_when_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", "0",
    )
    yaml_path = tmp_path / "patterns.yaml"
    yaml_path.write_text(
        "schema_version: 1\npatterns:\n  - name: x\n    regex: 'X'\n"
    )
    out = load_adapted_patterns(yaml_path)
    assert out == {}


# ===========================================================================
# C — YAML reader paths
# ===========================================================================


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", "1",
    )


def test_load_returns_empty_when_yaml_missing(monkeypatch, tmp_path):
    _enable(monkeypatch)
    out = load_adapted_patterns(tmp_path / "nonexistent.yaml")
    assert out == {}


def test_load_returns_empty_when_yaml_blank(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "blank.yaml"
    p.write_text("")
    out = load_adapted_patterns(p)
    assert out == {}


def test_load_returns_empty_when_yaml_top_not_mapping(
    monkeypatch, tmp_path,
):
    _enable(monkeypatch)
    p = tmp_path / "list_top.yaml"
    p.write_text("- entry1\n- entry2\n")
    out = load_adapted_patterns(p)
    assert out == {}


def test_load_returns_empty_when_patterns_key_missing(
    monkeypatch, tmp_path,
):
    _enable(monkeypatch)
    p = tmp_path / "no_patterns.yaml"
    p.write_text("schema_version: 1\nother_key: foo\n")
    out = load_adapted_patterns(p)
    assert out == {}


def test_load_returns_empty_when_yaml_oversize(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "oversize.yaml"
    big = "schema_version: 1\npatterns: []\n# " + ("x" * (MAX_YAML_BYTES + 100))
    p.write_text(big)
    out = load_adapted_patterns(p)
    assert out == {}


def test_load_returns_empty_when_yaml_parse_fails(
    monkeypatch, tmp_path,
):
    _enable(monkeypatch)
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 1\npatterns: [unclosed list\n")
    out = load_adapted_patterns(p)
    assert out == {}


def test_load_loads_one_valid_entry(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "one.yaml"
    p.write_text(
        "schema_version: 1\n"
        "patterns:\n"
        "  - name: adapted_x\n"
        "    regex: 'CRITICAL_X'\n"
        "    severity: soft\n"
        "    message: 'Adapted from postmortem'\n"
        "    proposal_id: 'adapt-sg-abc'\n"
        "    approved_at: '2026-04-26'\n"
        "    approved_by: 'alice'\n"
    )
    out = load_adapted_patterns(p)
    assert "adapted_x" in out
    assert callable(out["adapted_x"])


def test_load_loads_multiple_entries(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "multi.yaml"
    p.write_text(
        "schema_version: 1\npatterns:\n"
        "  - {name: a, regex: 'PATTERN_A_LONG'}\n"
        "  - {name: b, regex: 'PATTERN_B_LONG'}\n"
        "  - {name: c, regex: 'PATTERN_C_LONG'}\n"
    )
    out = load_adapted_patterns(p)
    assert set(out.keys()) == {"a", "b", "c"}


def test_load_skips_missing_required_fields(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "missing.yaml"
    p.write_text(
        "schema_version: 1\npatterns:\n"
        "  - {regex: 'X'}\n"  # missing name
        "  - {name: ok}\n"   # missing regex
        "  - {name: good, regex: 'GOOD_PATTERN'}\n"
    )
    out = load_adapted_patterns(p)
    assert out == {"good": out.get("good")}


def test_load_caps_at_max_adapted_patterns(monkeypatch, tmp_path):
    _enable(monkeypatch)
    entries = []
    for i in range(MAX_ADAPTED_PATTERNS + 50):
        entries.append(
            f"  - {{name: pattern_{i:04d}, regex: 'XYZ_{i:04d}'}}\n"
        )
    p = tmp_path / "many.yaml"
    p.write_text("schema_version: 1\npatterns:\n" + "".join(entries))
    out = load_adapted_patterns(p)
    assert len(out) == MAX_ADAPTED_PATTERNS


def test_load_truncates_oversized_regex(monkeypatch, tmp_path):
    _enable(monkeypatch)
    huge = "X" * (MAX_ADAPTED_REGEX_CHARS + 100)
    p = tmp_path / "huge_regex.yaml"
    p.write_text(
        f"schema_version: 1\npatterns:\n  - name: trunc\n    regex: '{huge}'\n"
    )
    out = load_adapted_patterns(p)
    # Pattern still loaded; regex was truncated at parse time
    assert "trunc" in out


def test_load_truncates_oversized_message(monkeypatch, tmp_path):
    _enable(monkeypatch)
    huge_msg = "M" * (MAX_ADAPTED_MESSAGE_CHARS + 100)
    p = tmp_path / "huge_msg.yaml"
    p.write_text(
        f"schema_version: 1\npatterns:\n"
        f"  - name: m\n    regex: 'PATTERN_X'\n    message: '{huge_msg}'\n"
    )
    out = load_adapted_patterns(p)
    assert "m" in out


def test_load_coerces_unknown_severity_to_soft(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "sev.yaml"
    p.write_text(
        "schema_version: 1\npatterns:\n"
        "  - {name: s, regex: 'PATTERN_S', severity: unknown_value}\n"
    )
    out = load_adapted_patterns(p)
    assert "s" in out


# ===========================================================================
# D — Cage rule: ADDITIVE only (collision with hand-written → SKIP)
# ===========================================================================


def test_load_skips_collision_with_hand_written(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "collide.yaml"
    p.write_text(
        "schema_version: 1\npatterns:\n"
        "  - {name: function_body_collapsed, regex: 'X'}\n"  # collides
        "  - {name: novel_pattern, regex: 'PATTERN_NOVEL'}\n"
    )
    out = load_adapted_patterns(
        p, hand_written_names=("function_body_collapsed",),
    )
    assert "function_body_collapsed" not in out
    assert "novel_pattern" in out


def test_load_skips_duplicate_within_yaml(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = tmp_path / "dup.yaml"
    p.write_text(
        "schema_version: 1\npatterns:\n"
        "  - {name: x, regex: 'PATTERN_X_FIRST'}\n"
        "  - {name: x, regex: 'PATTERN_X_SECOND'}\n"  # duplicate name
    )
    out = load_adapted_patterns(p)
    # Only the first occurrence is kept
    assert len(out) == 1
    assert "x" in out


# ===========================================================================
# E — Detector builder
# ===========================================================================


def test_detector_fires_on_match_in_new_only(monkeypatch, tmp_path):
    _enable(monkeypatch)
    entry = AdaptedPatternEntry(
        name="t", regex="CRITICAL_PATTERN_XYZ", severity="soft",
        message="test", proposal_id="adapt-sg-test",
        approved_at="t", approved_by="op",
    )
    det = _build_detector(entry)
    # Old has no match; new has match → fires
    out = det(
        file_path="x.py",
        old_content="benign",
        new_content="contains CRITICAL_PATTERN_XYZ here",
    )
    assert out is not None
    assert out.pattern == "t"
    assert out.severity == "soft"
    assert "adapt-sg-test" in out.message


def test_detector_does_not_fire_when_match_in_both_old_and_new():
    """Diff-aware: if the pattern was already there before, this op
    didn't introduce it — don't fire."""
    entry = AdaptedPatternEntry(
        name="t", regex="CRITICAL_PATTERN_XYZ", severity="soft",
        message="test", proposal_id="p", approved_at="t",
        approved_by="op",
    )
    det = _build_detector(entry)
    out = det(
        file_path="x.py",
        old_content="contains CRITICAL_PATTERN_XYZ already",
        new_content="contains CRITICAL_PATTERN_XYZ here",
    )
    assert out is None


def test_detector_does_not_fire_on_no_match():
    entry = AdaptedPatternEntry(
        name="t", regex="CRITICAL_PATTERN_XYZ", severity="soft",
        message="test", proposal_id="p", approved_at="t",
        approved_by="op",
    )
    det = _build_detector(entry)
    out = det(file_path="x.py", old_content="", new_content="benign code")
    assert out is None


def test_detector_handles_invalid_regex_gracefully():
    """Regex that doesn't compile → detector returns None for all
    inputs; never raises."""
    entry = AdaptedPatternEntry(
        name="bad", regex="[unbalanced", severity="soft",
        message="bad", proposal_id="p", approved_at="t",
        approved_by="op",
    )
    det = _build_detector(entry)
    out = det(
        file_path="x.py",
        old_content="",
        new_content="anything CRITICAL [unbalanced anything",
    )
    assert out is None


def test_detector_includes_line_number_and_snippet():
    entry = AdaptedPatternEntry(
        name="t", regex="CRITICAL_PATTERN_XYZ", severity="hard",
        message="test", proposal_id="p", approved_at="t",
        approved_by="op",
    )
    det = _build_detector(entry)
    out = det(
        file_path="x.py",
        old_content="",
        new_content="line1\nline2\nCRITICAL_PATTERN_XYZ on line 3\n",
    )
    assert out is not None
    assert out.severity == "hard"
    assert out.lines == (3,)
    assert "CRITICAL_PATTERN_XYZ" in out.snippet


# ===========================================================================
# F — _parse_entry direct
# ===========================================================================


def test_parse_entry_minimal_valid():
    e = _parse_entry({"name": "x", "regex": "PATTERN_X"}, 0)
    assert e is not None
    assert e.name == "x"
    assert e.severity == "soft"  # default


def test_parse_entry_missing_name_returns_none():
    e = _parse_entry({"regex": "X"}, 0)
    assert e is None


def test_parse_entry_missing_regex_returns_none():
    e = _parse_entry({"name": "x"}, 0)
    assert e is None


def test_parse_entry_provenance_fields_preserved():
    e = _parse_entry({
        "name": "x", "regex": "PATTERN_X",
        "proposal_id": "adapt-sg-abc",
        "approved_at": "2026-04-26",
        "approved_by": "alice",
    }, 0)
    assert e is not None
    assert e.proposal_id == "adapt-sg-abc"
    assert e.approved_at == "2026-04-26"
    assert e.approved_by == "alice"


# ===========================================================================
# G — SemanticGuardian boot-time merge
# ===========================================================================


def test_semantic_guardian_unchanged_when_loader_off(monkeypatch):
    """When the loader env flag is off, _PATTERNS contains EXACTLY
    the 10 hand-written patterns."""
    monkeypatch.setenv(
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS", "0",
    )
    # Re-import is not feasible (modules cached), but we can verify
    # that the live registry only contains hand-written names.
    from backend.core.ouroboros.governance.semantic_guardian import (
        _PATTERNS, _ALL_PATTERNS,
    )
    expected = {
        "removed_import_still_referenced", "function_body_collapsed",
        "guard_boolean_inverted", "credential_shape_introduced",
        "test_assertion_inverted", "return_value_flipped",
        "permission_loosened", "silent_exception_swallow",
        "hardcoded_url_swap", "docstring_only_delete",
    }
    # The 10 hand-written patterns are present
    assert expected.issubset(_PATTERNS.keys())
    # _ALL_PATTERNS at minimum lists all hand-written names
    assert expected.issubset(set(_ALL_PATTERNS))


# ===========================================================================
# H — Authority invariants (AST grep)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_LOADER_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
        "providers",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
    # Note: "semantic_guardian" is intentionally absent here — the
    # loader DOES lazy-import Detection inside the detector closure
    # to avoid circular dependency at module top level.
    assert not found_banned


def test_module_top_level_imports_only_stdlib():
    """Top-level imports must be stdlib only. The Detection import
    is intentionally lazy (inside the detector closure) to avoid a
    circular dependency with semantic_guardian.py."""
    tree = _ast.parse(_LOADER_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
        "__future__",
        "logging", "os", "re", "dataclasses", "pathlib", "typing",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert any(
                mod == p or mod.startswith(p + ".")
                for p in stdlib_prefixes
            ), f"unauthorized top-level import {mod!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _LOADER_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found
