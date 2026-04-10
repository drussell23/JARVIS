"""Tests for dependency_file_gate — Iron Gate 3 (dependency file integrity).

The exact regression: battle test bt-2026-04-10-184157 applied a Claude
patch to requirements.txt that renamed ``anthropic`` → ``anthropichttp``
and ``rapidfuzz`` → ``rapidfu``. These are pure ASCII, so the ASCII
strict gate passed them. They are near-identical to the source, so the
similarity gate (had it not crashed) would have flagged them as
plagiarism, which is the WRONG signal. The new gate catches them by
detecting removed-then-re-added near-identical names.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.dependency_file_gate import (
    check_candidate,
    check_requirements_integrity,
    is_dependency_file,
    _canonical_name,
    _levenshtein,
    _parse_requirements,
    _suspicious_rename,
)


# ---------------------------------------------------------------------------
# Unit: helpers
# ---------------------------------------------------------------------------


def test_canonical_name_pep503() -> None:
    assert _canonical_name("Anthropic") == "anthropic"
    assert _canonical_name("python-dotenv") == "python-dotenv"
    assert _canonical_name("python_dotenv") == "python-dotenv"
    assert _canonical_name("Python.Dotenv") == "python-dotenv"
    assert _canonical_name("rapidfuzz") == "rapidfuzz"


def test_levenshtein_basic() -> None:
    assert _levenshtein("anthropic", "anthropic") == 0
    assert _levenshtein("anthropic", "anthropichttp") == 4  # append "http"
    assert _levenshtein("rapidfuzz", "rapidfu") == 2        # delete "zz"
    assert _levenshtein("", "abc") == 3
    assert _levenshtein("a", "") == 1


def test_suspicious_rename_positive_cases() -> None:
    """The exact bt-2026-04-10-184157 corruptions."""
    assert _suspicious_rename("anthropic", "anthropichttp") is True
    assert _suspicious_rename("rapidfuzz", "rapidfu") is True
    assert _suspicious_rename("rapidfuzz", "rapidfuzzy") is True
    assert _suspicious_rename("requests", "reqest") is True   # edit dist 1
    assert _suspicious_rename("httpx", "httx") is True        # edit dist 1


def test_suspicious_rename_negative_cases() -> None:
    """Legitimate package swaps should NOT trip the heuristic."""
    assert _suspicious_rename("tensorflow", "jax") is False
    assert _suspicious_rename("boto3", "google-cloud-storage") is False
    assert _suspicious_rename("flask", "fastapi") is False


def test_is_dependency_file() -> None:
    assert is_dependency_file("requirements.txt") is True
    assert is_dependency_file("dev-requirements.txt") is True
    assert is_dependency_file("requirements-dev.txt") is True
    assert is_dependency_file("path/to/requirements.txt") is True
    assert is_dependency_file("setup.py") is False
    assert is_dependency_file("") is False
    assert is_dependency_file("requirements.yaml") is False


def test_parse_requirements_handles_comments_and_versions() -> None:
    content = """\
# header comment
anthropic==0.75.0
rapidfuzz>=3.0.0  # inline comment
httpx==0.28.1

python-dotenv==1.2.1
"""
    parsed = _parse_requirements(content)
    assert set(parsed) == {"anthropic", "rapidfuzz", "httpx", "python-dotenv"}


def test_parse_requirements_skips_option_lines() -> None:
    content = """\
-r other-requirements.txt
-e git+https://example.com/foo.git#egg=foo
--index-url https://pypi.org/simple
anthropic==0.75.0
"""
    parsed = _parse_requirements(content)
    assert set(parsed) == {"anthropic"}


# ---------------------------------------------------------------------------
# Unit: check_requirements_integrity
# ---------------------------------------------------------------------------


SOURCE = """\
# Project dependencies
aiohttp==3.13.2
anthropic==0.75.0
httpx==0.28.1
rapidfuzz>=3.0.0  # OCR fuzzy matching
requests==2.32.5
"""


def test_integrity_rejects_anthropic_to_anthropichttp_rename() -> None:
    """Exact bt-2026-04-10-184157 regression — MUST fail."""
    bad = SOURCE.replace("anthropic==0.75.0", "anthropichttp==0.75.0")
    result = check_requirements_integrity(bad, SOURCE)
    assert result is not None
    reason, offenders = result
    assert "anthropic -> anthropichttp" in offenders


def test_integrity_rejects_rapidfuzz_to_rapidfu_rename() -> None:
    """Exact bt-2026-04-10-184157 regression — MUST fail."""
    bad = SOURCE.replace("rapidfuzz>=3.0.0", "rapidfu>=3.0.0")
    result = check_requirements_integrity(bad, SOURCE)
    assert result is not None
    _reason, offenders = result
    assert "rapidfuzz -> rapidfu" in offenders


def test_integrity_accepts_pure_addition() -> None:
    """Legitimate additive changes must pass."""
    additive = SOURCE + "newdep==1.0.0\nother-dep==2.0.0\n"
    assert check_requirements_integrity(additive, SOURCE) is None


def test_integrity_accepts_version_bump() -> None:
    """Bumping a version (no name change) must pass."""
    bumped = SOURCE.replace("anthropic==0.75.0", "anthropic==0.80.0")
    assert check_requirements_integrity(bumped, SOURCE) is None


def test_integrity_accepts_comment_changes() -> None:
    """Pure comment edits must pass."""
    commented = SOURCE.replace("# OCR fuzzy matching", "# For intelligent fuzzy text matching")
    assert check_requirements_integrity(commented, SOURCE) is None


def test_integrity_accepts_legitimate_replacement() -> None:
    """Swapping one package for a clearly different one is allowed
    (this is a deletion + addition, not a rename/truncation)."""
    swapped = SOURCE.replace("rapidfuzz>=3.0.0", "thefuzz>=0.20.0")
    # thefuzz vs rapidfuzz — edit distance 6, no prefix match, no substring → allowed
    assert check_requirements_integrity(swapped, SOURCE) is None


def test_integrity_handles_empty_inputs() -> None:
    assert check_requirements_integrity("", SOURCE) is None
    assert check_requirements_integrity(SOURCE, "") is None
    assert check_requirements_integrity("", "") is None


def test_integrity_catches_multiple_renames_at_once() -> None:
    """If the model mangles several names, list all of them."""
    bad = SOURCE.replace(
        "anthropic==0.75.0", "anthropichttp==0.75.0"
    ).replace(
        "rapidfuzz>=3.0.0", "rapidfu>=3.0.0"
    )
    result = check_requirements_integrity(bad, SOURCE)
    assert result is not None
    _reason, offenders = result
    assert len(offenders) == 2
    assert "anthropic -> anthropichttp" in offenders
    assert "rapidfuzz -> rapidfu" in offenders


# ---------------------------------------------------------------------------
# Integration: check_candidate (orchestrator entry point)
# ---------------------------------------------------------------------------


def test_check_candidate_legacy_shape(tmp_path: Path) -> None:
    """Legacy single-file candidate with full_content + file_path."""
    (tmp_path / "requirements.txt").write_text(SOURCE, encoding="utf-8")
    bad = SOURCE.replace("rapidfuzz>=3.0.0", "rapidfu>=3.0.0")
    cand = {"file_path": "requirements.txt", "full_content": bad}
    result = check_candidate(cand, tmp_path)
    assert result is not None
    _reason, offenders = result
    assert "rapidfuzz -> rapidfu" in offenders


def test_check_candidate_multi_file_shape(tmp_path: Path) -> None:
    """Multi-file candidate with files[] list."""
    (tmp_path / "requirements.txt").write_text(SOURCE, encoding="utf-8")
    bad = SOURCE.replace("anthropic==0.75.0", "anthropichttp==0.75.0")
    cand = {
        "files": [
            {"file_path": "some_other.py", "full_content": "print('ok')"},
            {"file_path": "requirements.txt", "full_content": bad},
        ]
    }
    result = check_candidate(cand, tmp_path)
    assert result is not None
    _reason, offenders = result
    assert "anthropic -> anthropichttp" in offenders


def test_check_candidate_skips_non_dependency_files(tmp_path: Path) -> None:
    cand = {"file_path": "backend/main.py", "full_content": "print('hi')\n"}
    assert check_candidate(cand, tmp_path) is None


def test_check_candidate_safe_additive_change(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(SOURCE, encoding="utf-8")
    additive = SOURCE + "newdep==1.0.0\n"
    cand = {"file_path": "requirements.txt", "full_content": additive}
    assert check_candidate(cand, tmp_path) is None


def test_check_candidate_handles_missing_source_file(tmp_path: Path) -> None:
    """No prior file on disk means no baseline — safe."""
    cand = {"file_path": "requirements.txt", "full_content": "foo==1.0.0\n"}
    assert check_candidate(cand, tmp_path) is None


def test_check_candidate_disabled_via_env(tmp_path: Path, monkeypatch) -> None:
    """JARVIS_DEP_FILE_GATE_ENABLED=false bypasses the gate entirely."""
    import importlib
    import backend.core.ouroboros.governance.dependency_file_gate as gate
    monkeypatch.setenv("JARVIS_DEP_FILE_GATE_ENABLED", "false")
    importlib.reload(gate)
    try:
        (tmp_path / "requirements.txt").write_text(SOURCE, encoding="utf-8")
        bad = SOURCE.replace("anthropic==0.75.0", "anthropichttp==0.75.0")
        cand = {"file_path": "requirements.txt", "full_content": bad}
        assert gate.check_candidate(cand, tmp_path) is None
    finally:
        monkeypatch.delenv("JARVIS_DEP_FILE_GATE_ENABLED", raising=False)
        importlib.reload(gate)
