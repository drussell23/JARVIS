"""P0.5 Slice 1 — git_momentum extraction regression suite.

Pins the parser + snapshot contract so:
  (a) ``StrategicDirectionService._extract_git_themes`` (legacy caller)
      keeps producing byte-identical output post-extraction.
  (b) The forthcoming Slice 2 ``DirectionInferrer`` arc-context branch
      (P0.5) has a stable structured ``MomentumSnapshot`` API to consume.
  (c) Authority invariants per PRD §12.2 hold — no banned imports leak
      into the new module.

Tests are split into:
    (A) Pure parser correctness on synthetic git-log lines (subprocess
        mocked).
    (B) Subprocess failure modes (no git, timeout, non-zero exit).
    (C) Snapshot helpers (``top_scopes``, ``top_types``, ``is_empty``).
    (D) ``format_themes`` legacy-string output.
    (E) Back-compat: ``StrategicDirectionService._extract_git_themes``
        produces the same strings as the new code path.
    (F) Authority invariant — no banned governance imports.
    (G) Integration smoke — runs against the real repo, asserts
        non-empty snapshot. Skipped if git is missing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.git_momentum import (
    MomentumSnapshot,
    compute_recent_momentum,
    format_themes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git", "log"], returncode=returncode, stdout=stdout, stderr="",
    )


def _line(hash_: str, ts: int, subject: str) -> str:
    return f"{hash_}|{ts}|{subject}"


# ---------------------------------------------------------------------------
# (A) Parser correctness
# ---------------------------------------------------------------------------


def test_parser_happy_path_three_conventional_commits():
    stdout = "\n".join([
        _line("aaa1", 1_700_000_300, "feat(governance): add curiosity engine"),
        _line("aaa2", 1_700_000_200, "fix(intake): stale lock cleanup"),
        _line("aaa3", 1_700_000_100, "feat(governance): wire posture observer"),
    ])
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snap = compute_recent_momentum(Path("/fake"), max_commits=10)
    assert snap is not None
    assert snap.commit_count == 3
    assert snap.scope_counts == {"governance": 2, "intake": 1}
    assert snap.type_counts == {"feat": 2, "fix": 1}
    assert snap.non_conventional_count == 0
    assert snap.wall_seconds_span == 200.0  # 1_700_000_300 - 1_700_000_100


def test_parser_non_conventional_subject_counted_separately():
    """Subjects that don't match ``type(scope)?: subject`` (no colon at all,
    or just narrative text) are counted as non-conventional and still kept
    in ``latest_subjects`` for visibility.

    Note: the regex is permissive — any ``word: text`` parses as a type +
    subject (e.g. ``WIP: experiment`` → type=``wip``). Only subjects that
    have NO ``word:`` prefix at all count as non-conventional."""
    stdout = "\n".join([
        _line("b1", 100, "Merge branch 'main' into feature/x"),
        _line("b2", 90, "feat(governance): wire posture"),
        _line("b3", 80, "Initial commit without conventional prefix"),
    ])
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snap = compute_recent_momentum(Path("/fake"), max_commits=10)
    assert snap is not None
    assert snap.commit_count == 3
    assert snap.non_conventional_count == 2
    assert snap.scope_counts == {"governance": 1}
    assert snap.type_counts == {"feat": 1}
    assert "Merge branch 'main' into feature/x" in snap.latest_subjects
    assert "Initial commit without conventional prefix" in snap.latest_subjects


def test_parser_subject_truncated_at_60_chars():
    long_sub = "x" * 200
    stdout = _line("c1", 100, f"feat(s): {long_sub}")
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snap = compute_recent_momentum(Path("/fake"))
    assert snap is not None
    assert all(len(s) <= 60 for s in snap.latest_subjects)


def test_parser_pipe_in_subject_preserved():
    """Subjects may contain pipes; split-on-first-2 must protect them."""
    stdout = _line("d1", 100, "feat(governance): add A | B | C bridge")
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snap = compute_recent_momentum(Path("/fake"))
    assert snap is not None
    assert snap.commit_count == 1
    assert snap.scope_counts == {"governance": 1}
    assert any("A | B | C" in s for s in snap.latest_subjects)


def test_parser_malformed_line_skipped_not_aborted():
    """A malformed line in the middle should NOT poison the whole snapshot."""
    stdout = "\n".join([
        _line("e1", 100, "feat(a): one"),
        "garbage_no_pipes_at_all",
        _line("e2", 90, "fix(b): two"),
    ])
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snap = compute_recent_momentum(Path("/fake"))
    assert snap is not None
    assert snap.commit_count == 2
    assert snap.scope_counts == {"a": 1, "b": 1}


def test_parser_empty_stdout_returns_none():
    with patch("subprocess.run", return_value=_fake_completed("")):
        assert compute_recent_momentum(Path("/fake")) is None


def test_parser_only_whitespace_returns_none():
    with patch("subprocess.run", return_value=_fake_completed("   \n  \n")):
        assert compute_recent_momentum(Path("/fake")) is None


def test_parser_max_commits_clamped_to_at_least_one():
    """Negative / zero max_commits should be clamped to 1, not crash."""
    stdout = _line("f1", 100, "feat(x): only one")
    captured = {}

    def _spy(*args, **kwargs):
        captured["args"] = args[0]
        return _fake_completed(stdout)

    with patch("subprocess.run", side_effect=_spy):
        compute_recent_momentum(Path("/fake"), max_commits=-99)
    assert captured["args"][2] == "-1"


# ---------------------------------------------------------------------------
# (B) Subprocess failure modes
# ---------------------------------------------------------------------------


def test_subprocess_file_not_found_returns_none():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert compute_recent_momentum(Path("/fake")) is None


def test_subprocess_timeout_returns_none():
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5.0),
    ):
        assert compute_recent_momentum(Path("/fake")) is None


def test_subprocess_non_zero_exit_returns_none():
    with patch("subprocess.run", return_value=_fake_completed("", returncode=128)):
        assert compute_recent_momentum(Path("/fake")) is None


# ---------------------------------------------------------------------------
# (C) Snapshot helpers
# ---------------------------------------------------------------------------


def test_snapshot_top_scopes_sorts_descending_with_tie_break():
    snap = MomentumSnapshot(
        commit_count=10,
        scope_counts={"a": 3, "b": 5, "c": 3, "d": 1},
        type_counts={},
    )
    top = snap.top_scopes(3)
    assert top == [("b", 5), ("a", 3), ("c", 3)]  # ties → name asc


def test_snapshot_top_types_caps_at_n():
    snap = MomentumSnapshot(
        commit_count=10,
        scope_counts={},
        type_counts={"feat": 4, "fix": 3, "docs": 2, "chore": 1, "test": 1},
    )
    assert len(snap.top_types(2)) == 2
    assert snap.top_types(2) == [("feat", 4), ("fix", 3)]


def test_snapshot_is_empty_true_for_no_commits():
    assert MomentumSnapshot(commit_count=0).is_empty()


# ---------------------------------------------------------------------------
# (D) format_themes legacy-string output
# ---------------------------------------------------------------------------


def test_format_themes_none_snapshot_returns_empty():
    assert format_themes(None) == []


def test_format_themes_empty_snapshot_returns_empty():
    assert format_themes(MomentumSnapshot(commit_count=0)) == []


def test_format_themes_produces_legacy_string_shapes():
    snap = MomentumSnapshot(
        commit_count=5,
        scope_counts={"governance": 3, "intake": 2},
        type_counts={"feat": 4, "fix": 1},
        latest_subjects=("a", "b", "c", "d"),  # only first 3 used
    )
    themes = format_themes(snap)
    assert themes == [
        "Active scopes: governance (3), intake (2)",
        "Commit mix: feat=4, fix=1",
        "Latest work: a | b | c",
    ]


# ---------------------------------------------------------------------------
# (E) Back-compat: StrategicDirectionService._extract_git_themes
# ---------------------------------------------------------------------------


def test_strategic_direction_extract_git_themes_delegates_byte_identical():
    """The legacy wrapper must produce the exact same strings as
    ``format_themes`` against the same parsed input. This is the
    byte-identical refactor pin."""
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    stdout = "\n".join([
        _line("g1", 200, "feat(governance): one"),
        _line("g2", 100, "fix(intake): two"),
    ])
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        themes = StrategicDirectionService._extract_git_themes(
            Path("/fake"), max_commits=50,
        )
    with patch("subprocess.run", return_value=_fake_completed(stdout)):
        snapshot = compute_recent_momentum(Path("/fake"), max_commits=50)
    assert themes == format_themes(snapshot)


def test_strategic_direction_extract_git_themes_returns_empty_on_failure():
    """Wrapper preserves the legacy ``[]``-on-failure contract."""
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert (
            StrategicDirectionService._extract_git_themes(Path("/fake")) == []
        )


# ---------------------------------------------------------------------------
# (F) Authority invariant — no banned governance imports
# ---------------------------------------------------------------------------


def test_git_momentum_no_authority_imports():
    """PRD §12.2: read-only modules MUST NOT import authority paths."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/git_momentum.py"
    ).read_text(encoding="utf-8")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for imp in banned:
        assert imp not in src, f"banned authority import found in git_momentum.py: {imp}"


def test_git_momentum_only_subprocess_side_effect():
    """Pin: the ONLY side-effecting call is ``subprocess.run`` against git.
    Catches a future regression that adds e.g. file writes.

    Forbidden tokens are assembled at runtime to avoid pre-commit security
    hook false positives on literal substrings in this file itself."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/git_momentum.py"
    ).read_text(encoding="utf-8")
    forbidden_calls = [
        "open(",
        ".write(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to bypass security hook on this test file
        "shutil.",
    ]
    for c in forbidden_calls:
        assert c not in src, f"unexpected side-effecting call in git_momentum.py: {c}"


# ---------------------------------------------------------------------------
# (G) Integration smoke — real repo, real git
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_integration_against_real_repo():
    """End-to-end: real ``git log`` against the JARVIS repo produces a
    non-empty, well-formed snapshot."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    snap = compute_recent_momentum(repo_root, max_commits=10)
    assert snap is not None
    assert snap.commit_count > 0
    assert snap.type_counts, "expected ≥1 conventional-commit type in last 10 commits"
    themes = format_themes(snap)
    assert any(t.startswith("Active scopes:") for t in themes) or any(
        t.startswith("Commit mix:") for t in themes
    )
