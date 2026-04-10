"""Tests for StrategicDirectionService git-history direction inference.

Covers the ``_extract_git_themes`` deterministic parser and its integration
into ``load()`` via the ``_git_themes``/digest section pipeline.

The extractor runs ``git log`` in a real temporary repo (no mocking of git)
so it exercises the same subprocess path used in production.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with a handful of conventional commits."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    # Disable signing in case the user's global config requires it.
    _git(tmp_path, "config", "commit.gpgsign", "false")

    (tmp_path / "README.md").write_text("# Seed\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "chore: seed repo")

    commit_messages = [
        "feat(governance): add multi-file candidate support",
        "feat(governance): wire change_engine rollback",
        "fix(sensors): silence proactive_exploration ImportError",
        "feat(sensors): add runtime health checks",
        "refactor(orchestrator): extract _iter_candidate_files helper",
        "docs: update OUROBOROS.md pipeline diagram",
        "test(governance): cover multi-file rollback path",
        "Merge branch 'ouroboros/battle-test/20260408'",
    ]
    for idx, msg in enumerate(commit_messages):
        f = tmp_path / f"f{idx}.txt"
        f.write_text(str(idx))
        _git(tmp_path, "add", f.name)
        _git(tmp_path, "commit", "-q", "-m", msg)
    return tmp_path


class TestExtractGitThemes:
    def test_returns_themes_for_sample_repo(self, sample_repo: Path):
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=50
        )
        # Should produce at least the three theme lines (scopes, types, latest).
        assert len(themes) >= 2
        joined = " ".join(themes)
        assert "Active scopes" in joined
        assert "Commit mix" in joined
        assert "Latest work" in joined

    def test_scope_histogram_counts_correctly(self, sample_repo: Path):
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=50
        )
        scopes_line = next(t for t in themes if t.startswith("Active scopes"))
        # governance appears 3x, sensors 2x, orchestrator 1x
        assert "governance (3)" in scopes_line
        assert "sensors (2)" in scopes_line
        assert "orchestrator (1)" in scopes_line

    def test_type_histogram_counts_feat_and_fix(self, sample_repo: Path):
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=50
        )
        mix_line = next(t for t in themes if t.startswith("Commit mix"))
        # sample_repo has: feat×3, chore×1 (seed), docs×1, fix×1, refactor×1,
        # test×1. The helper caps at top 4 by (-count, name), so with ties at 1
        # we expect feat=3 plus the first 3 tied alphabetically: chore, docs, fix.
        assert "feat=3" in mix_line
        assert "fix=1" in mix_line

    def test_latest_work_shows_most_recent_subjects(self, sample_repo: Path):
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=50
        )
        latest_line = next(t for t in themes if t.startswith("Latest work"))
        # The final commit (non-conventional "Merge branch") should appear first
        # in the recency window (git log returns newest first).
        assert "Merge branch" in latest_line or "Merge" in latest_line

    def test_empty_for_non_git_directory(self, tmp_path: Path):
        # Brand-new tmp_path with no `.git` — should return [] gracefully.
        themes = StrategicDirectionService._extract_git_themes(
            tmp_path, max_commits=50
        )
        assert themes == []

    def test_respects_max_commits_cap(self, sample_repo: Path):
        # Cap to just 2 commits — only the two most recent should feed the histogram.
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=2
        )
        joined = " ".join(themes)
        # With 2 commits, governance count should be 0-1, not 3.
        assert "governance (3)" not in joined

    def test_non_conventional_merge_commits_do_not_crash(self, sample_repo: Path):
        # Sanity: the sample repo already has one "Merge branch" line. Just assert
        # the call returns without raising and produces some themes.
        themes = StrategicDirectionService._extract_git_themes(
            sample_repo, max_commits=50
        )
        assert isinstance(themes, list)


class TestFormatGitThemes:
    def test_empty_themes_return_empty_string(self):
        assert StrategicDirectionService._format_git_themes([]) == ""

    def test_formatted_section_is_markdown_with_header(self):
        formatted = StrategicDirectionService._format_git_themes(
            ["Active scopes: governance (3)", "Commit mix: feat=4"]
        )
        assert "## Recent Development Momentum" in formatted
        assert "- Active scopes: governance (3)" in formatted
        assert "- Commit mix: feat=4" in formatted


class TestLoadIntegration:
    @pytest.mark.asyncio
    async def test_load_populates_git_themes_on_real_repo(
        self, sample_repo: Path, monkeypatch
    ):
        # Explicitly enable the gate (default is true, but be defensive).
        monkeypatch.setenv("JARVIS_STRATEGIC_GIT_HISTORY_ENABLED", "true")
        svc = StrategicDirectionService(sample_repo)
        await svc.load()
        assert svc.is_loaded is True
        # The git themes should be populated.
        assert len(svc._git_themes) >= 2
        # The digest should include the "Recent Development Momentum" section.
        assert "Recent Development Momentum" in svc.digest

    @pytest.mark.asyncio
    async def test_load_skips_git_themes_when_gate_disabled(
        self, sample_repo: Path, monkeypatch
    ):
        # Disable via env — load() should not call the extractor.
        # NB: the gate is read at module import time, so we patch the
        # module-level sentinel directly for this test.
        import backend.core.ouroboros.governance.strategic_direction as sd_mod
        monkeypatch.setattr(sd_mod, "_GIT_HISTORY_ENABLED", False)

        svc = StrategicDirectionService(sample_repo)
        await svc.load()
        assert svc._git_themes == []
        assert "Recent Development Momentum" not in svc.digest
