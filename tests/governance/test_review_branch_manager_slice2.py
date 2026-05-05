"""Tests for review_branch_manager (Gap #4 Slice 2).

Strategy: real git plumbing exercised against a temp git repo per
test. Each test creates a fresh repo, runs ReviewBranchManager
operations, and asserts on actual git state via direct subprocess
calls. Slow-ish (~50ms/test) but proves the plumbing works.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.review_branch_manager import (
    AcceptOutcome,
    BRANCH_PREFIX,
    CreateOutcome,
    REVIEW_BRANCH_SCHEMA_VERSION,
    RejectOutcome,
    ReviewBranch,
    ReviewBranchManager,
    ReviewState,
    build_branch_name,
    safe_branch_slug,
)


# ===========================================================================
# Helpers — temp repo construction
# ===========================================================================


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``; check=True; capture output."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialize a fresh repo with one baseline commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "README.md").write_text("# baseline\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial commit", cwd=repo)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture
def manager(repo: Path) -> ReviewBranchManager:
    return ReviewBranchManager(repo, git_timeout_s=10.0)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _branch_exists(repo: Path, name: str) -> bool:
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=str(repo), check=False,
    )
    return proc.returncode == 0


# ===========================================================================
# Schema + constants
# ===========================================================================


def test_schema_version_pinned():
    assert REVIEW_BRANCH_SCHEMA_VERSION == "review_branch.v1"


def test_branch_prefix():
    assert BRANCH_PREFIX == "ouroboros/preview/"


def test_review_state_closed_taxonomy():
    assert {m.value for m in ReviewState} == {
        "pending", "accepted", "rejected", "superseded", "expired",
    }


@pytest.mark.parametrize("state,terminal", [
    (ReviewState.PENDING, False),
    (ReviewState.ACCEPTED, True),
    (ReviewState.REJECTED, True),
    (ReviewState.SUPERSEDED, True),
    (ReviewState.EXPIRED, True),
])
def test_review_state_is_terminal(state: ReviewState, terminal: bool):
    assert state.is_terminal is terminal


# ===========================================================================
# Slug + branch name helpers
# ===========================================================================


def test_safe_branch_slug_passes_through_alnum():
    assert safe_branch_slug("op-019d8") == "op-019d8"
    assert safe_branch_slug("a_b_c") == "a_b_c"


def test_safe_branch_slug_replaces_specials():
    assert safe_branch_slug("op/with/slash") == "op-with-slash"
    assert safe_branch_slug("op:colon") == "op-colon"


def test_safe_branch_slug_caps_at_40():
    long = "x" * 100
    assert len(safe_branch_slug(long)) == 40


def test_build_branch_name_uses_prefix():
    assert build_branch_name("op-x").startswith("ouroboros/preview/")


def test_build_branch_name_handles_empty_op_id():
    # Slug becomes empty; branch is "ouroboros/preview/" — that's a
    # naming policy decision (caller's responsibility to filter, but
    # the function never raises).
    assert build_branch_name("") == "ouroboros/preview/"


# ===========================================================================
# Precondition checks
# ===========================================================================


def test_create_blocked_on_dirty_tree(repo: Path, manager: ReviewBranchManager):
    """Manager refuses to create when the working tree has uncommitted
    changes — accept() would fail later."""
    (repo / "README.md").write_text("# DIRTY\n")  # uncommitted edit
    result = _run(manager.create(
        "op-x", [("foo.py", "print('hi')\n")],
        risk_tier="notify_apply",
    ))
    assert result.outcome is CreateOutcome.BLOCKED
    assert result.branch is None
    assert "dirty" in result.error.lower()


def test_create_blocked_on_detached_head(repo: Path, manager: ReviewBranchManager):
    """Detached HEAD has no base branch — refuse (matches OrangePR semantics)."""
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", sha, cwd=repo)  # detached
    result = _run(manager.create(
        "op-x", [("foo.py", "x")],
    ))
    assert result.outcome is CreateOutcome.BLOCKED
    assert "detached" in result.error.lower() or "rev-parse" in result.error.lower()


def test_create_failed_on_empty_op_id(repo: Path, manager: ReviewBranchManager):
    result = _run(manager.create("", [("foo.py", "x")]))
    assert result.outcome is CreateOutcome.FAILED
    assert "empty" in result.error.lower()


def test_create_failed_on_empty_files(repo: Path, manager: ReviewBranchManager):
    result = _run(manager.create("op-x", []))
    assert result.outcome is CreateOutcome.FAILED
    assert "empty" in result.error.lower()


# ===========================================================================
# Create — happy path
# ===========================================================================


def test_create_makes_branch_off_head(repo: Path, manager: ReviewBranchManager):
    base_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    result = _run(manager.create(
        "op-019d8", [("foo.py", "print('hello')\n")],
        risk_tier="notify_apply",
        diff_archive_ref="d-1",
    ))
    assert result.outcome is CreateOutcome.CREATED
    assert isinstance(result.branch, ReviewBranch)
    assert result.branch.state is ReviewState.PENDING
    assert result.branch.base_sha == base_sha
    assert result.branch.tip_sha != base_sha
    assert result.branch.diff_archive_ref == "d-1"
    assert _branch_exists(repo, result.branch.branch_name)


def test_create_does_not_modify_working_tree(repo: Path, manager: ReviewBranchManager):
    """CRITICAL: the non-destructive contract. Working tree, HEAD, and
    index must all be untouched after create()."""
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    branch_before = _git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=repo,
    ).stdout.strip()
    readme_before = (repo / "README.md").read_text()

    _run(manager.create(
        "op-x", [("brand_new.py", "x = 1\n")],
        risk_tier="notify_apply",
    ))

    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    branch_after = _git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=repo,
    ).stdout.strip()
    readme_after = (repo / "README.md").read_text()

    assert head_after == head_before
    assert branch_after == branch_before
    assert readme_after == readme_before
    # The new file MUST NOT exist in the working tree.
    assert not (repo / "brand_new.py").exists()


def test_create_branch_carries_committed_files(
    repo: Path, manager: ReviewBranchManager,
):
    """The candidate file must show up when checking out the branch."""
    _run(manager.create(
        "op-x", [("new_file.py", "content_marker_xyz\n")],
        risk_tier="notify_apply",
    ))
    branch = build_branch_name("op-x")
    # Inspect via cat-file (don't actually checkout — keeps test repo clean)
    result = _git(
        "show", f"{branch}:new_file.py", cwd=repo,
    )
    assert "content_marker_xyz" in result.stdout


def test_create_collision_when_branch_exists(
    repo: Path, manager: ReviewBranchManager,
):
    """If a branch with the same name already exists, refuse."""
    _git(
        "branch", "ouroboros/preview/op-collide", "HEAD", cwd=repo,
    )
    result = _run(manager.create(
        "op-collide", [("x.py", "x")],
        risk_tier="notify_apply",
    ))
    assert result.outcome is CreateOutcome.COLLISION


def test_create_records_lookup_returns_record(
    repo: Path, manager: ReviewBranchManager,
):
    result = _run(manager.create(
        "op-record", [("x.py", "x")],
        risk_tier="notify_apply",
    ))
    fetched = manager.lookup("op-record")
    assert fetched is not None
    assert fetched.branch_name == result.branch.branch_name


def test_create_unknown_op_id_lookup_returns_none(manager: ReviewBranchManager):
    assert manager.lookup("never-existed") is None
    assert manager.lookup("") is None
    assert manager.lookup(None) is None


def test_create_multi_file_candidate(repo: Path, manager: ReviewBranchManager):
    files = [
        ("a.py", "module a\n"),
        ("sub/b.py", "module b\n"),
        ("sub/c.py", "module c\n"),
    ]
    result = _run(manager.create(
        "op-multi", files, risk_tier="notify_apply",
    ))
    assert result.outcome is CreateOutcome.CREATED
    assert result.branch.file_paths == ("a.py", "sub/b.py", "sub/c.py")
    branch = result.branch.branch_name
    for path, _ in files:
        out = _git("show", f"{branch}:{path}", cwd=repo).stdout
        assert "module" in out


# ===========================================================================
# Accept
# ===========================================================================


def test_accept_fast_forwards_head_and_deletes_branch(
    repo: Path, manager: ReviewBranchManager,
):
    """Happy path: HEAD advances to the preview tip; branch is gone."""
    _run(manager.create(
        "op-accept", [("new.py", "ok\n")],
        risk_tier="notify_apply",
    ))
    branch_name = build_branch_name("op-accept")
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    result = _run(manager.accept("op-accept"))
    assert result.outcome is AcceptOutcome.ACCEPTED
    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after != head_before  # advanced
    assert not _branch_exists(repo, branch_name)
    assert (repo / "new.py").exists()  # file now in working tree
    assert manager.lookup("op-accept").state is ReviewState.ACCEPTED


def test_accept_blocked_on_dirty_tree(
    repo: Path, manager: ReviewBranchManager,
):
    """Operator dirtied the tree between create and accept — refuse."""
    _run(manager.create(
        "op-x", [("clean.py", "x\n")],
        risk_tier="notify_apply",
    ))
    (repo / "README.md").write_text("# dirty after create\n")
    result = _run(manager.accept("op-x"))
    assert result.outcome is AcceptOutcome.BLOCKED


def test_accept_not_fast_forward_when_base_moves(
    repo: Path, manager: ReviewBranchManager,
):
    """Operator made a normal commit on main between create and accept —
    no longer a fast-forward."""
    _run(manager.create(
        "op-x", [("review.py", "review\n")],
        risk_tier="notify_apply",
    ))
    # Operator commits something else on main.
    (repo / "OTHER.md").write_text("operator's own change\n")
    _git("add", "OTHER.md", cwd=repo)
    _git("commit", "-m", "operator commit", cwd=repo)

    result = _run(manager.accept("op-x"))
    assert result.outcome is AcceptOutcome.NOT_FAST_FORWARD
    assert manager.lookup("op-x").state is ReviewState.SUPERSEDED


def test_accept_unknown_op_id_returns_failed(manager: ReviewBranchManager):
    result = _run(manager.accept("never-existed"))
    assert result.outcome is AcceptOutcome.FAILED


def test_accept_already_terminal_refuses(
    repo: Path, manager: ReviewBranchManager,
):
    _run(manager.create("op-x", [("x.py", "x")], risk_tier="notify_apply"))
    _run(manager.accept("op-x"))
    again = _run(manager.accept("op-x"))
    assert again.outcome is AcceptOutcome.FAILED
    assert "terminal" in again.error.lower()


# ===========================================================================
# Reject
# ===========================================================================


def test_reject_deletes_branch(repo: Path, manager: ReviewBranchManager):
    _run(manager.create(
        "op-reject", [("x.py", "x")], risk_tier="notify_apply",
    ))
    branch_name = build_branch_name("op-reject")
    assert _branch_exists(repo, branch_name)

    result = _run(manager.reject("op-reject", reason="not what I wanted"))
    assert result.outcome is RejectOutcome.REJECTED
    assert not _branch_exists(repo, branch_name)
    record = manager.lookup("op-reject")
    assert record.state is ReviewState.REJECTED
    assert "not what I wanted" in record.error


def test_reject_unknown_op_id_returns_not_found(manager: ReviewBranchManager):
    result = _run(manager.reject("never-existed"))
    assert result.outcome is RejectOutcome.NOT_FOUND


def test_reject_already_terminal_refuses(
    repo: Path, manager: ReviewBranchManager,
):
    _run(manager.create("op-x", [("x.py", "x")], risk_tier="notify_apply"))
    _run(manager.reject("op-x"))
    again = _run(manager.reject("op-x"))
    assert again.outcome is RejectOutcome.FAILED


def test_reject_does_not_touch_working_tree(
    repo: Path, manager: ReviewBranchManager,
):
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _run(manager.create("op-x", [("new.py", "x\n")], risk_tier="notify_apply"))
    _run(manager.reject("op-x"))
    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after == head_before
    assert not (repo / "new.py").exists()


# ===========================================================================
# Expire (timeout-driven)
# ===========================================================================


def test_expire_marks_state_expired_and_deletes_branch(
    repo: Path, manager: ReviewBranchManager,
):
    _run(manager.create(
        "op-expire", [("x.py", "x")], risk_tier="notify_apply",
    ))
    branch_name = build_branch_name("op-expire")
    result = _run(manager.expire("op-expire"))
    assert result.outcome is RejectOutcome.REJECTED
    assert not _branch_exists(repo, branch_name)
    assert manager.lookup("op-expire").state is ReviewState.EXPIRED


# ===========================================================================
# list_pending / list_all
# ===========================================================================


def test_list_pending_returns_only_pending(
    repo: Path, manager: ReviewBranchManager,
):
    _run(manager.create("op-1", [("a.py", "a")], risk_tier="notify_apply"))
    _run(manager.create("op-2", [("b.py", "b")], risk_tier="notify_apply"))
    _run(manager.create("op-3", [("c.py", "c")], risk_tier="notify_apply"))
    _run(manager.reject("op-2"))

    pending = manager.list_pending()
    assert len(pending) == 2
    op_ids = {r.op_id for r in pending}
    assert op_ids == {"op-1", "op-3"}


def test_list_all_returns_everything(
    repo: Path, manager: ReviewBranchManager,
):
    _run(manager.create("op-1", [("a.py", "a")], risk_tier="notify_apply"))
    _run(manager.create("op-2", [("b.py", "b")], risk_tier="notify_apply"))
    _run(manager.reject("op-2"))
    assert len(manager.list_all()) == 2


# ===========================================================================
# ReviewBranch frozen + projection
# ===========================================================================


def test_review_branch_to_dict_shape(
    repo: Path, manager: ReviewBranchManager,
):
    result = _run(manager.create(
        "op-shape", [("x.py", "x\n")],
        risk_tier="notify_apply",
        diff_archive_ref="d-7",
    ))
    d = result.branch.to_dict()
    assert d["op_id"] == "op-shape"
    assert d["state"] == "pending"
    assert d["risk_tier"] == "notify_apply"
    assert d["diff_archive_ref"] == "d-7"
    assert d["schema_version"] == REVIEW_BRANCH_SCHEMA_VERSION


def test_review_branch_is_frozen(
    repo: Path, manager: ReviewBranchManager,
):
    result = _run(manager.create("op-x", [("x.py", "x")], risk_tier="notify_apply"))
    with pytest.raises(Exception):
        result.branch.state = ReviewState.ACCEPTED  # type: ignore[misc]


# ===========================================================================
# Defensive — bad subprocess handling
# ===========================================================================


def test_subprocess_timeout_returns_failed_not_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If git times out, manager returns FAILED — never raises."""
    manager = ReviewBranchManager(repo, git_timeout_s=10.0)

    # Override the sync shim to simulate timeout.
    def _broken_sync(args, *, env_overrides=None, stdin_bytes=None):
        return 1, "", "git subprocess failed: TimeoutExpired"

    monkeypatch.setattr(manager, "_run_git_sync", _broken_sync)
    result = _run(manager.create("op-x", [("x.py", "x")], risk_tier="notify_apply"))
    assert result.outcome is CreateOutcome.BLOCKED
    assert "rev-parse" in result.error or "timeout" in result.error.lower()
