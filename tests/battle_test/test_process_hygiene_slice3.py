"""Harness Epic Slice 3 — process hygiene + runbook tests.

Pins:

A. The operator runbook exists at the canonical path with required sections.
B. The CI guard script exists, is executable, and exits 0 on the current
   clean tree.
C. The CI guard correctly catches the banned pattern when it appears
   (fixture test using a temp dir + subprocess invocation of the script).
D. The canonical pgrep probe is the documented form (regex-anchored).
E. Cross-references in the runbook point to existing docs/memory files.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (A) Runbook exists with required sections
# ---------------------------------------------------------------------------


def test_runbook_exists_at_canonical_path():
    """The operator runbook lives at docs/operations/battle_test_runbook.md."""
    p = Path("docs/operations/battle_test_runbook.md")
    assert p.is_file(), f"runbook missing at {p}"


def test_runbook_documents_canonical_pgrep_probe():
    """Runbook includes the canonical pgrep probe pattern."""
    src = Path("docs/operations/battle_test_runbook.md").read_text()
    assert r'pgrep -f "python3? scripts/ouroboros_battle_test\.py"' in src


def test_runbook_documents_banned_stdin_guard():
    """Runbook explicitly bans the tail -f /dev/null | python pattern."""
    src = Path("docs/operations/battle_test_runbook.md").read_text()
    # Banned pattern is mentioned + ban is explicit
    assert "tail -f /dev/null" in src
    assert "banned" in src.lower()


def test_runbook_documents_standard_launch_recipe():
    """Standard launch recipe uses --headless (per Slice 3 + ticket C)."""
    src = Path("docs/operations/battle_test_runbook.md").read_text()
    assert "ouroboros_battle_test.py" in src
    assert "--headless" in src


def test_runbook_documents_recovery_procedures():
    """Recovery sections cover exit code 75, stale lock, wedged TTL, and
    missing summary.json."""
    src = Path("docs/operations/battle_test_runbook.md").read_text()
    assert "exit code 75" in src.lower() or "exit 75" in src.lower()
    assert "stale lock" in src.lower()
    assert "wedged" in src.lower()
    assert "summary.json" in src


def test_runbook_cross_links_existing_docs():
    """Cross-references must point to files that actually exist."""
    src = Path("docs/operations/battle_test_runbook.md").read_text()
    # CLAUDE.md should be referenced
    assert "CLAUDE.md" in src
    # Cross-link to the harness epic memory file (memory/ paths exist
    # outside the repo, so we just pin that they're MENTIONED — the
    # content is curated, not git-tracked)
    assert "harness_epic_scope" in src
    assert "battle_test_post_summary_hang" in src


# ---------------------------------------------------------------------------
# (B) CI guard script exists + executable + clean tree exits 0
# ---------------------------------------------------------------------------


def test_guard_script_exists():
    p = Path("scripts/check_no_stdin_guard.sh")
    assert p.is_file(), "guard script missing"


def test_guard_script_is_executable():
    p = Path("scripts/check_no_stdin_guard.sh")
    mode = p.stat().st_mode
    assert mode & stat.S_IXUSR, "guard script must be executable"


def test_guard_script_exits_zero_on_clean_tree():
    """The current tree (post-Slice-3) must pass the guard."""
    result = subprocess.run(
        ["bash", "scripts/check_no_stdin_guard.sh"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"clean tree should pass guard; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (C) CI guard correctly catches violations (fixture test)
# ---------------------------------------------------------------------------


def test_guard_script_catches_banned_pattern_in_docs(tmp_path):
    """Stage a violation in a synthetic docs/scripts tree and verify the
    guard exits non-zero. Uses bash-only fallback (no git repo)."""
    docs = tmp_path / "docs"
    scripts = tmp_path / "scripts"
    docs.mkdir()
    scripts.mkdir()
    (docs / "violation.md").write_text(
        "Run with: tail -f /dev/null | python3 foo.py\n"
    )
    # Copy guard script into the synthetic tree
    guard_src = Path("scripts/check_no_stdin_guard.sh").read_text()
    (scripts / "check_no_stdin_guard.sh").write_text(guard_src)
    (scripts / "check_no_stdin_guard.sh").chmod(0o755)

    # Run from synthetic tree (no git → uses grep fallback path)
    result = subprocess.run(
        ["bash", "scripts/check_no_stdin_guard.sh"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode == 1, (
        f"guard should catch violation; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "tail -f /dev/null" in result.stderr or "tail -f /dev/null" in result.stdout


def test_guard_script_catches_banned_pattern_in_scripts(tmp_path):
    """Same as above but the violation is in scripts/ instead of docs/."""
    docs = tmp_path / "docs"
    scripts = tmp_path / "scripts"
    docs.mkdir()
    scripts.mkdir()
    (scripts / "bad_runner.sh").write_text(
        "#!/usr/bin/env bash\ntail -f /dev/null | python3 foo.py\n"
    )
    guard_src = Path("scripts/check_no_stdin_guard.sh").read_text()
    (scripts / "check_no_stdin_guard.sh").write_text(guard_src)
    (scripts / "check_no_stdin_guard.sh").chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/check_no_stdin_guard.sh"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# (D) Canonical pgrep probe consistency check
#     The runbook + the launcher single-flight check should reference the
#     same pattern. If either drifts, this test catches it.
# ---------------------------------------------------------------------------


def test_canonical_pgrep_pattern_consistent_runbook_vs_launcher():
    """The runbook + the launcher single-flight code use the same pgrep pattern.
    If either drifts, false-positive / false-negative bugs surface."""
    runbook = Path("docs/operations/battle_test_runbook.md").read_text()
    launcher = Path("scripts/ouroboros_battle_test.py").read_text()
    canonical = r'python3? scripts/ouroboros_battle_test\.py'
    assert canonical in runbook, (
        "runbook drifted from canonical pgrep pattern"
    )
    assert canonical in launcher, (
        "launcher single-flight drifted from canonical pgrep pattern"
    )


# ---------------------------------------------------------------------------
# (E) Source-grep that the codebase is currently clean
#     This is the regression pin — if any future commit adds the banned
#     pattern to docs/ or scripts/, this test fails BEFORE the CI guard
#     runs (cheaper feedback for local pytest runs).
# ---------------------------------------------------------------------------


def test_codebase_currently_clean_of_banned_pattern():
    """No banned stdin-guard pattern in docs/ or scripts/ today.

    If this fails, you (or a recent commit) added back the banned
    pattern. Use --headless instead. See
    docs/operations/battle_test_runbook.md.
    """
    # Same exempt set as scripts/check_no_stdin_guard.sh — these files
    # legitimately quote the pattern (one IS the guard regex, one
    # documents the ban).
    EXEMPT_PATHS = {
        "scripts/check_no_stdin_guard.sh",
        "docs/operations/battle_test_runbook.md",
    }
    for scope in (Path("docs"), Path("scripts")):
        for f in scope.rglob("*"):
            if not f.is_file():
                continue
            relpath = str(f.as_posix())
            if relpath in EXEMPT_PATHS:
                continue
            try:
                content = f.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            assert "tail -f /dev/null | python" not in content, (
                f"banned pattern found in {f} — use --headless instead. "
                f"See docs/operations/battle_test_runbook.md"
            )
