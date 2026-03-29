"""Tests for source_crawlers — P0 and P1 tiered crawlers."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.roadmap.source_crawlers import (
    crawl_backlog,
    crawl_claude_md,
    crawl_git_log,
    crawl_memory,
    crawl_plans,
    crawl_specs,
)
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# crawl_specs
# ---------------------------------------------------------------------------

class TestCrawlSpecs:
    def test_crawl_specs_finds_md_files(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        _write_md(specs_dir / "ouroboros-design.md", "# Ouroboros Design\n\nContent here.")
        _write_md(specs_dir / "auth-spec.md", "# Auth Spec\n\nAuth content.")

        frags = crawl_specs(tmp_path)

        assert len(frags) == 2
        source_ids = {f.source_id for f in frags}
        assert "spec:ouroboros-design" in source_ids
        assert "spec:auth-spec" in source_ids

    def test_crawl_specs_empty_dir(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)

        frags = crawl_specs(tmp_path)

        assert frags == []

    def test_crawl_specs_missing_dir(self, tmp_path: Path) -> None:
        frags = crawl_specs(tmp_path)
        assert frags == []

    def test_crawl_specs_fragment_fields(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        _write_md(specs_dir / "my-spec.md", "# My Spec Title\n\nBody content.")

        frags = crawl_specs(tmp_path)

        assert len(frags) == 1
        f = frags[0]
        assert f.source_id == "spec:my-spec"
        assert f.fragment_type == "spec"
        assert f.tier == 0
        assert f.title == "My Spec Title"
        assert "Body content" in f.summary or "My Spec" in f.summary
        assert len(f.content_hash) == 64  # SHA-256 hex
        assert f.fetched_at > 0
        assert f.mtime > 0

    def test_crawl_specs_title_from_heading(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        _write_md(specs_dir / "some-file.md", "# Real Title\n\nstuff")

        frags = crawl_specs(tmp_path)
        assert frags[0].title == "Real Title"

    def test_crawl_specs_title_fallback_to_stem(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        _write_md(specs_dir / "no-heading.md", "just some content without a heading")

        frags = crawl_specs(tmp_path)
        assert frags[0].title == "no-heading"

    def test_crawl_specs_content_hash_changes(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        spec_file = specs_dir / "changing.md"
        spec_file.write_text("# Original\n\noriginal content", encoding="utf-8")

        frags_v1 = crawl_specs(tmp_path)
        hash_v1 = frags_v1[0].content_hash

        spec_file.write_text("# Modified\n\nmodified content", encoding="utf-8")
        frags_v2 = crawl_specs(tmp_path)
        hash_v2 = frags_v2[0].content_hash

        assert hash_v1 != hash_v2

    def test_crawl_specs_non_md_files_ignored(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        (specs_dir / "spec.md").write_text("# Spec", encoding="utf-8")
        (specs_dir / "readme.txt").write_text("ignored", encoding="utf-8")
        (specs_dir / "schema.json").write_text("{}", encoding="utf-8")

        frags = crawl_specs(tmp_path)
        assert len(frags) == 1


# ---------------------------------------------------------------------------
# crawl_plans
# ---------------------------------------------------------------------------

class TestCrawlPlans:
    def test_crawl_plans_finds_md_files(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        plans_dir.mkdir(parents=True)
        _write_md(plans_dir / "q1-plan.md", "# Q1 Plan\n\nWork items.")
        _write_md(plans_dir / "q2-plan.md", "# Q2 Plan\n\nMore work.")

        frags = crawl_plans(tmp_path)

        assert len(frags) == 2
        source_ids = {f.source_id for f in frags}
        assert "plan:q1-plan" in source_ids
        assert "plan:q2-plan" in source_ids

    def test_crawl_plans_fragment_type(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        plans_dir.mkdir(parents=True)
        _write_md(plans_dir / "roadmap.md", "# Roadmap\n\ncontent")

        frags = crawl_plans(tmp_path)
        assert all(f.fragment_type == "plan" for f in frags)
        assert all(f.tier == 0 for f in frags)

    def test_crawl_plans_empty_dir(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        plans_dir.mkdir(parents=True)
        assert crawl_plans(tmp_path) == []

    def test_crawl_plans_missing_dir(self, tmp_path: Path) -> None:
        assert crawl_plans(tmp_path) == []


# ---------------------------------------------------------------------------
# crawl_backlog
# ---------------------------------------------------------------------------

class TestCrawlBacklog:
    def test_crawl_backlog_finds_json(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / ".jarvis"
        backlog_dir.mkdir(parents=True)
        backlog_data = {"items": [{"id": 1, "title": "Fix bug"}]}
        (backlog_dir / "backlog.json").write_text(
            json.dumps(backlog_data), encoding="utf-8"
        )

        frags = crawl_backlog(tmp_path)

        assert len(frags) == 1
        f = frags[0]
        assert f.source_id == "backlog:jarvis"
        assert f.fragment_type == "backlog"
        assert f.tier == 0
        assert len(f.content_hash) == 64

    def test_crawl_backlog_missing_file(self, tmp_path: Path) -> None:
        frags = crawl_backlog(tmp_path)
        assert frags == []

    def test_crawl_backlog_hash_changes_with_content(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / ".jarvis"
        backlog_dir.mkdir(parents=True)
        backlog_file = backlog_dir / "backlog.json"
        backlog_file.write_text('{"items": []}', encoding="utf-8")

        frags_v1 = crawl_backlog(tmp_path)
        h1 = frags_v1[0].content_hash

        backlog_file.write_text('{"items": [{"id": 1}]}', encoding="utf-8")
        frags_v2 = crawl_backlog(tmp_path)
        h2 = frags_v2[0].content_hash

        assert h1 != h2

    def test_crawl_backlog_uri_is_relative_path(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / ".jarvis"
        backlog_dir.mkdir(parents=True)
        (backlog_dir / "backlog.json").write_text('{"items": []}', encoding="utf-8")

        frags = crawl_backlog(tmp_path)
        assert ".jarvis/backlog.json" in frags[0].uri


# ---------------------------------------------------------------------------
# crawl_memory
# ---------------------------------------------------------------------------

class TestCrawlMemory:
    def test_crawl_memory_finds_md_files(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True)
        _write_md(mem_dir / "MEMORY.md", "# Memory\n\nKey facts.")
        _write_md(mem_dir / "audio.md", "# Audio\n\nAudio facts.")

        frags = crawl_memory(tmp_path)

        assert len(frags) == 2
        source_ids = {f.source_id for f in frags}
        assert "memory:MEMORY" in source_ids
        assert "memory:audio" in source_ids

    def test_crawl_memory_fragment_type_and_tier(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True)
        _write_md(mem_dir / "notes.md", "# Notes\n\ncontent")

        frags = crawl_memory(tmp_path)
        assert all(f.fragment_type == "memory" for f in frags)
        assert all(f.tier == 0 for f in frags)

    def test_crawl_memory_missing_dir(self, tmp_path: Path) -> None:
        assert crawl_memory(tmp_path) == []


# ---------------------------------------------------------------------------
# crawl_claude_md
# ---------------------------------------------------------------------------

class TestCrawlClaudeMd:
    def test_crawl_claude_md_reads_claude_md(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Claude Instructions\n\ncontent", encoding="utf-8")

        frags = crawl_claude_md(tmp_path)

        source_ids = {f.source_id for f in frags}
        assert "config:CLAUDE.md" in source_ids

    def test_crawl_claude_md_reads_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n\nagent content", encoding="utf-8")

        frags = crawl_claude_md(tmp_path)

        source_ids = {f.source_id for f in frags}
        assert "config:AGENTS.md" in source_ids

    def test_crawl_claude_md_reads_both(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Claude", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("# Agents", encoding="utf-8")

        frags = crawl_claude_md(tmp_path)
        assert len(frags) == 2

    def test_crawl_claude_md_missing_files(self, tmp_path: Path) -> None:
        frags = crawl_claude_md(tmp_path)
        assert frags == []

    def test_crawl_claude_md_fragment_type(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Claude", encoding="utf-8")
        frags = crawl_claude_md(tmp_path)
        assert all(f.fragment_type == "memory" for f in frags)
        assert all(f.tier == 0 for f in frags)


# ---------------------------------------------------------------------------
# crawl_git_log
# ---------------------------------------------------------------------------

class TestCrawlGitLog:
    def test_crawl_git_log_returns_fragment(self, tmp_path: Path) -> None:
        """Should return a list (possibly empty) but never crash."""
        frags = crawl_git_log(tmp_path)
        assert isinstance(frags, list)
        for f in frags:
            assert isinstance(f, SnapshotFragment)

    def test_crawl_git_log_non_git_dir_returns_empty(self, tmp_path: Path) -> None:
        """A non-git directory must silently return []."""
        frags = crawl_git_log(tmp_path)
        assert frags == []

    def test_crawl_git_log_with_real_repo(self, tmp_path: Path) -> None:
        """Initialize a real git repo with a commit and verify fragment is produced."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )
        test_file = tmp_path / "readme.md"
        test_file.write_text("# hello", encoding="utf-8")
        subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=str(tmp_path))
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )

        frags = crawl_git_log(tmp_path)

        assert len(frags) == 1
        f = frags[0]
        assert f.fragment_type == "commit_log"
        assert f.tier == 1
        assert "git:" in f.source_id
        assert ":bounded" in f.source_id
        assert "initial commit" in f.summary

    def test_crawl_git_log_fragment_fields(self, tmp_path: Path) -> None:
        """Verify fragment field types for a real repo."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )
        (tmp_path / "f.md").write_text("content", encoding="utf-8")
        subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=str(tmp_path))
        subprocess.run(
            ["git", "commit", "-m", "test commit"],
            check=True, capture_output=True, cwd=str(tmp_path),
        )

        frags = crawl_git_log(tmp_path)
        assert len(frags) == 1
        f = frags[0]
        assert len(f.content_hash) == 64
        assert f.fetched_at > 0
        assert isinstance(f.mtime, float)


# ---------------------------------------------------------------------------
# Stability / cross-crawl guarantees
# ---------------------------------------------------------------------------

class TestFragmentStability:
    def test_fragment_source_id_is_stable(self, tmp_path: Path) -> None:
        """Same crawl twice yields same source_ids."""
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        _write_md(specs_dir / "stable-spec.md", "# Stable\n\ncontent")

        ids_run1 = {f.source_id for f in crawl_specs(tmp_path)}
        ids_run2 = {f.source_id for f in crawl_specs(tmp_path)}

        assert ids_run1 == ids_run2

    def test_fragment_source_id_stable_across_backlog(self, tmp_path: Path) -> None:
        backlog_dir = tmp_path / ".jarvis"
        backlog_dir.mkdir(parents=True)
        (backlog_dir / "backlog.json").write_text('{"items": []}', encoding="utf-8")

        ids_run1 = {f.source_id for f in crawl_backlog(tmp_path)}
        ids_run2 = {f.source_id for f in crawl_backlog(tmp_path)}

        assert ids_run1 == ids_run2

    def test_all_returned_objects_are_snapshot_fragments(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        plans_dir = tmp_path / "docs" / "superpowers" / "plans"
        mem_dir = tmp_path / "memory"
        backlog_dir = tmp_path / ".jarvis"
        for d in [specs_dir, plans_dir, mem_dir, backlog_dir]:
            d.mkdir(parents=True)

        _write_md(specs_dir / "s.md", "# S\ncontent")
        _write_md(plans_dir / "p.md", "# P\ncontent")
        _write_md(mem_dir / "m.md", "# M\ncontent")
        (backlog_dir / "backlog.json").write_text('{"items": []}', encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("# C\ncontent", encoding="utf-8")

        all_frags = (
            crawl_specs(tmp_path)
            + crawl_plans(tmp_path)
            + crawl_memory(tmp_path)
            + crawl_backlog(tmp_path)
            + crawl_claude_md(tmp_path)
        )

        assert all(isinstance(f, SnapshotFragment) for f in all_frags)

    def test_summary_max_500_chars(self, tmp_path: Path) -> None:
        specs_dir = tmp_path / "docs" / "superpowers" / "specs"
        specs_dir.mkdir(parents=True)
        long_content = "# Title\n\n" + "x" * 2000
        _write_md(specs_dir / "long.md", long_content)

        frags = crawl_specs(tmp_path)
        assert len(frags[0].summary) <= 500
