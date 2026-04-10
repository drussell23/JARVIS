"""Tests for OrangePRReviewer — async PR creation for APPROVAL_REQUIRED ops.

The subprocess calls are stubbed at the ``_run_git_sync`` /
``_run_gh_sync`` boundary so the tests never touch the real git or gh
binary, yet the async ``_run_git`` / ``_run_gh`` wrappers (which use
``asyncio.to_thread``) run unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

from backend.core.ouroboros.governance.orange_pr_reviewer import (
    OrangePRReviewer,
    PRReviewResult,
    build_commit_message,
    build_pr_body,
    is_orange_pr_enabled,
)


# ── Pure helpers ─────────────────────────────────────────────────────────


class TestIsOrangePREnabled:
    def test_defaults_off(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ORANGE_PR_ENABLED", raising=False)
        assert is_orange_pr_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_ORANGE_PR_ENABLED", value)
        assert is_orange_pr_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_ORANGE_PR_ENABLED", value)
        assert is_orange_pr_enabled() is False


class TestBuildCommitMessage:
    def test_subject_follows_conventional_commits(self):
        msg = build_commit_message(
            op_id="op-42",
            description="refactor auth middleware for compliance",
            files=[("a.py", "x")],
        )
        subject = msg.splitlines()[0]
        assert subject.startswith("chore(ouroboros-review):")

    def test_body_lists_files(self):
        msg = build_commit_message(
            op_id="op-42",
            description="multi-file refactor",
            files=[("a.py", "x"), ("b.py", "y"), ("c.py", "z")],
        )
        assert "- a.py" in msg
        assert "- b.py" in msg
        assert "- c.py" in msg

    def test_body_truncates_after_20_files(self):
        files: List[Tuple[str, str]] = [(f"f{i}.py", "x") for i in range(25)]
        msg = build_commit_message(
            op_id="op-many", description="big change", files=files
        )
        assert "... and 5 more" in msg

    def test_body_contains_do_not_auto_merge_marker(self):
        msg = build_commit_message(
            op_id="op-42", description="x", files=[("a.py", "y")]
        )
        assert "DO NOT AUTO-MERGE" in msg

    def test_subject_truncates_long_description(self):
        long_desc = "a" * 200
        msg = build_commit_message(
            op_id="op-1", description=long_desc, files=[("a.py", "b")]
        )
        subject = msg.splitlines()[0]
        # Subject stays under ~120 chars total
        assert len(subject) < 120


class TestBuildPRBody:
    def test_renders_markdown_header(self):
        body = build_pr_body(
            op_id="op-1",
            description="test change",
            files=[("a.py", "x")],
        )
        assert "## Ouroboros Review Request" in body
        assert "**Op ID:** `op-1`" in body

    def test_lists_files_as_markdown_bullets(self):
        body = build_pr_body(
            op_id="op-1",
            description="test",
            files=[("a.py", "x"), ("dir/b.py", "y")],
        )
        assert "- `a.py`" in body
        assert "- `dir/b.py`" in body

    def test_serializes_evidence_json_block(self):
        body = build_pr_body(
            op_id="op-1",
            description="test",
            files=[("a.py", "x")],
            evidence={"reason": "large_diff", "lines_changed": 500},
        )
        assert "### Risk evidence" in body
        assert '"reason": "large_diff"' in body
        assert '"lines_changed": 500' in body

    def test_review_checklist_present(self):
        body = build_pr_body("op-1", "d", [("a.py", "x")])
        assert "### Review checklist" in body
        assert "[ ]" in body

    def test_truncates_file_list_at_30(self):
        files = [(f"f{i}.py", "x") for i in range(35)]
        body = build_pr_body("op-1", "d", files)
        assert "... and 5 more" in body


# ── OrangePRReviewer with stubbed subprocess boundary ────────────────────


class _ScriptedReviewer(OrangePRReviewer):
    """OrangePRReviewer whose subprocess calls are scripted.

    ``git_script`` is a dict keyed on the first ``git`` argument
    (e.g. ``"rev-parse"``) whose value is a ``(rc, stdout, stderr)`` tuple.
    ``gh_script`` is the analogous map for ``gh``.
    """

    def __init__(
        self,
        project_root: Path,
        git_script: dict,
        gh_script: dict,
    ) -> None:
        super().__init__(project_root, git_timeout_s=1.0, gh_timeout_s=1.0)
        self.git_script = git_script
        self.gh_script = gh_script
        self.git_calls: List[List[str]] = []
        self.gh_calls: List[List[str]] = []

    def _run_git_sync(self, args):
        self.git_calls.append(list(args))
        key = args[0] if args else ""
        return self.git_script.get(key, (0, "", ""))

    def _run_gh_sync(self, args):
        self.gh_calls.append(list(args))
        key = f"{args[0]} {args[1]}" if len(args) >= 2 else (args[0] if args else "")
        return self.gh_script.get(key, (0, "", ""))


def _candidate_files() -> List[Tuple[str, str]]:
    return [("src/a.py", "a=1\n"), ("src/b.py", "b=2\n")]


class TestCreateReviewPRHappyPath:
    @pytest.mark.asyncio
    async def test_opens_pr_and_returns_result(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={
                "rev-parse": (0, "main", ""),
                "checkout": (0, "", ""),
                "add": (0, "", ""),
                "commit": (0, "", ""),
                "push": (0, "", ""),
            },
            gh_script={
                "pr create": (
                    0, "https://github.com/owner/repo/pull/42", ""
                ),
            },
        )

        result = await reviewer.create_review_pr(
            op_id="op-42",
            description="test change",
            files=_candidate_files(),
        )

        assert result is not None
        assert isinstance(result, PRReviewResult)
        assert result.url == "https://github.com/owner/repo/pull/42"
        assert result.branch.startswith("ouroboros/review/op-42")
        assert result.base_branch == "main"

        # Files were written to disk.
        assert (tmp_path / "src" / "a.py").read_text() == "a=1\n"
        assert (tmp_path / "src" / "b.py").read_text() == "b=2\n"

    @pytest.mark.asyncio
    async def test_restores_base_branch_after_success(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={
                "rev-parse": (0, "main", ""),
                "checkout": (0, "", ""),
                "add": (0, "", ""),
                "commit": (0, "", ""),
                "push": (0, "", ""),
            },
            gh_script={
                "pr create": (0, "https://github.com/owner/repo/pull/1", ""),
            },
        )
        await reviewer.create_review_pr("op-1", "d", _candidate_files())
        # Last git call should be a checkout back to main.
        last_checkout = [c for c in reviewer.git_calls if c and c[0] == "checkout"][-1]
        assert last_checkout[1] == "main"


class TestCreateReviewPRFailurePaths:
    @pytest.mark.asyncio
    async def test_returns_none_when_rev_parse_fails(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={"rev-parse": (1, "", "not a git repo")},
            gh_script={},
        )
        result = await reviewer.create_review_pr(
            "op-1", "d", _candidate_files()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_refuses_detached_head(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={"rev-parse": (0, "HEAD", "")},
            gh_script={},
        )
        result = await reviewer.create_review_pr(
            "op-1", "d", _candidate_files()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_gh_fails(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={
                "rev-parse": (0, "main", ""),
                "checkout": (0, "", ""),
                "add": (0, "", ""),
                "commit": (0, "", ""),
                "push": (0, "", ""),
            },
            gh_script={
                "pr create": (1, "", "gh: not authenticated"),
            },
        )
        result = await reviewer.create_review_pr(
            "op-1", "d", _candidate_files()
        )
        assert result is None
        # Must still have attempted to return to base branch.
        checkouts = [c for c in reviewer.git_calls if c and c[0] == "checkout"]
        assert any(c[1] == "main" for c in checkouts)

    @pytest.mark.asyncio
    async def test_returns_none_when_gh_returns_non_url(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={
                "rev-parse": (0, "main", ""),
                "checkout": (0, "", ""),
                "add": (0, "", ""),
                "commit": (0, "", ""),
                "push": (0, "", ""),
            },
            gh_script={
                "pr create": (0, "Creating pull request for...", ""),
            },
        )
        result = await reviewer.create_review_pr(
            "op-1", "d", _candidate_files()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_files(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={},
            gh_script={},
        )
        result = await reviewer.create_review_pr("op-1", "d", [])
        assert result is None
        assert reviewer.git_calls == []

    @pytest.mark.asyncio
    async def test_returns_none_when_push_fails(self, tmp_path: Path):
        reviewer = _ScriptedReviewer(
            project_root=tmp_path,
            git_script={
                "rev-parse": (0, "main", ""),
                "checkout": (0, "", ""),
                "add": (0, "", ""),
                "commit": (0, "", ""),
                "push": (1, "", "remote rejected"),
            },
            gh_script={},
        )
        result = await reviewer.create_review_pr(
            "op-1", "d", _candidate_files()
        )
        assert result is None
        # gh pr create should NOT have been called if push failed.
        assert reviewer.gh_calls == []
