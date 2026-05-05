"""Tests for review_coordinator (Gap #4 Slice 3) + orchestrator hook
regression check.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.battle_test.diff_archive import (
    DiffArchive,
    DiffOutcome,
    VerifyOutcome,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.review_branch_manager import (
    ReviewBranchManager,
    ReviewState,
)
from backend.core.ouroboros.governance.review_coordinator import (
    CoordinatedReview,
    MASTER_FLAG_ENV_VAR,
    REVIEW_COORDINATOR_SCHEMA_VERSION,
    ReviewCoordinator,
    ReviewDecision,
    TIMEOUT_ENV_VAR,
    is_master_flag_enabled,
    read_timeout_s,
    reset_default_coordinator_for_tests,
)


# ===========================================================================
# Fixtures
# ===========================================================================


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "README.md").write_text("# baseline\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture(autouse=True)
def clean_env_and_singletons(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    reset_default_coordinator_for_tests()
    yield
    reset_default_archive_for_tests()
    reset_default_coordinator_for_tests()


@pytest.fixture
def coordinator(repo: Path) -> ReviewCoordinator:
    archive = DiffArchive(capacity=50)
    manager = ReviewBranchManager(repo, git_timeout_s=10.0)
    return ReviewCoordinator(archive=archive, branch_manager=manager)


# ===========================================================================
# Schema + flags
# ===========================================================================


def test_schema_version_pinned():
    assert REVIEW_COORDINATOR_SCHEMA_VERSION == "review_coordinator.v1"


def test_master_flag_default_off():
    assert is_master_flag_enabled() is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("garbage", False),
])
def test_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_master_flag_enabled() is expected


def test_default_timeout_is_300():
    assert read_timeout_s() == 300.0


@pytest.mark.parametrize("raw,expected", [
    ("0", 0.0),
    ("60", 60.0),
    ("0.5", 0.5),
    ("garbage", 300.0),  # fallback
    ("-5", 300.0),       # negative → fallback
])
def test_timeout_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(TIMEOUT_ENV_VAR, raw)
    assert read_timeout_s() == expected


# ===========================================================================
# ReviewDecision closed taxonomy
# ===========================================================================


def test_review_decision_closed_taxonomy():
    assert {m.value for m in ReviewDecision} == {
        "accepted", "rejected", "expired", "skipped", "failed",
    }


@pytest.mark.parametrize("decision,proceeds", [
    (ReviewDecision.ACCEPTED, True),
    (ReviewDecision.SKIPPED, True),
    (ReviewDecision.REJECTED, False),
    (ReviewDecision.EXPIRED, False),
    (ReviewDecision.FAILED, False),
])
def test_review_decision_implies_apply(
    decision: ReviewDecision, proceeds: bool,
):
    assert decision.implies_apply is proceeds


# ===========================================================================
# coordinate_review — short-circuits
# ===========================================================================


def test_coordinate_skipped_when_master_flag_off(
    coordinator: ReviewCoordinator,
):
    """Without the master flag, coordinator returns SKIPPED — caller
    falls through to legacy auto-apply."""
    result = asyncio.get_event_loop().run_until_complete(
        coordinator.coordinate_review(
            "op-x", [("foo.py", "x\n")],
            risk_tier="notify_apply",
        )
    )
    assert result.decision is ReviewDecision.SKIPPED
    assert result.archive_ref == ""
    assert result.branch_name is None


def test_coordinate_skipped_when_timeout_zero(
    monkeypatch, coordinator: ReviewCoordinator,
):
    """Timeout=0 is the operator's opt-in to legacy auto-apply."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "0")
    result = asyncio.get_event_loop().run_until_complete(
        coordinator.coordinate_review(
            "op-x", [("foo.py", "x\n")],
            risk_tier="notify_apply",
        )
    )
    assert result.decision is ReviewDecision.SKIPPED


def test_coordinate_failed_when_branch_manager_unattached(monkeypatch):
    """If the singleton was constructed without a manager, returns FAILED."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    coord = ReviewCoordinator()  # no manager
    result = asyncio.get_event_loop().run_until_complete(
        coord.coordinate_review(
            "op-x", [("foo.py", "x")],
            risk_tier="notify_apply",
        )
    )
    assert result.decision is ReviewDecision.FAILED
    assert "not attached" in result.error


# ===========================================================================
# coordinate_review — happy-path workflows (with real git plumbing)
# ===========================================================================


async def _wait_for_pending_then(
    coordinator: ReviewCoordinator, op_id: str,
    record_call,
    *, max_wait_s: float = 5.0,
) -> bool:
    """Poll until ``op_id`` is registered as pending (i.e. the branch
    creation completed and ``coordinate_review`` is now waiting), then
    call ``record_call`` (a coordinator.record_accept/reject lambda).

    Returns the bool from record_call, or False if the op never got
    registered within ``max_wait_s``."""
    deadline = asyncio.get_event_loop().time() + max_wait_s
    while asyncio.get_event_loop().time() < deadline:
        if coordinator.archive_ref_for_op(op_id):
            # ref is set → archive.add ran → registration is imminent.
            # Try the record call; if it returns True, success.
            ok = record_call()
            if ok:
                return True
        await asyncio.sleep(0.02)
    return False


def test_coordinate_accepts_when_operator_accepts(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()

    async def _scenario():
        accept_task = asyncio.create_task(
            _wait_for_pending_then(
                coordinator, "op-accept",
                lambda: coordinator.record_accept("op-accept"),
            ),
        )
        review = await coordinator.coordinate_review(
            "op-accept", [("new.py", "ok\n")],
            risk_tier="notify_apply", timeout_s=10.0,
        )
        ok = await accept_task
        assert ok, "record_accept never succeeded"
        return review

    result: CoordinatedReview = loop.run_until_complete(_scenario())
    assert result.decision is ReviewDecision.ACCEPTED
    assert result.archive_ref.startswith("d-")
    assert result.branch_name is not None


def test_coordinate_rejects_when_operator_rejects(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()

    async def _scenario():
        reject_task = asyncio.create_task(
            _wait_for_pending_then(
                coordinator, "op-reject",
                lambda: coordinator.record_reject("op-reject"),
            ),
        )
        review = await coordinator.coordinate_review(
            "op-reject", [("new.py", "ok\n")],
            risk_tier="notify_apply", timeout_s=10.0,
        )
        ok = await reject_task
        assert ok, "record_reject never succeeded"
        return review

    result = loop.run_until_complete(_scenario())
    assert result.decision is ReviewDecision.REJECTED


def test_coordinate_expires_on_timeout(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(
        coordinator.coordinate_review(
            "op-expire", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=0.5,  # short timeout to keep test fast
        )
    )
    assert result.decision is ReviewDecision.EXPIRED
    # Branch should have been deleted via expire().
    branch_record = coordinator.branch_manager.lookup("op-expire")
    assert branch_record is not None
    assert branch_record.state is ReviewState.EXPIRED


def test_coordinate_rejects_when_cancel_check_fires(
    monkeypatch, coordinator: ReviewCoordinator,
):
    """The existing /cancel REPL verb must still work — cancel_check
    callable returning True maps to a synthetic REJECTED."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    cancel_state = [False]

    def _cancel_check():
        return cancel_state[0]

    loop = asyncio.get_event_loop()

    async def _scenario():
        async def _trigger():
            await asyncio.sleep(1.2)  # after ~1 cancel poll
            cancel_state[0] = True

        trigger = asyncio.create_task(_trigger())
        review = await coordinator.coordinate_review(
            "op-cancel", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=5.0,
            cancel_check=_cancel_check,
        )
        await trigger
        return review

    result = loop.run_until_complete(_scenario())
    assert result.decision is ReviewDecision.REJECTED


# ===========================================================================
# Records — op-to-ref mapping + archive integration
# ===========================================================================


def test_archive_ref_for_op_returns_ref_post_coordination(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        coordinator.coordinate_review(
            "op-record", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=0.3,
        )
    )
    ref = coordinator.archive_ref_for_op("op-record")
    assert ref is not None
    assert ref.startswith("d-")
    branch = coordinator.branch_for_op("op-record")
    assert branch is not None


def test_archive_carries_branch_name_post_coordination(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        coordinator.coordinate_review(
            "op-stamp", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=0.3,
        )
    )
    ref = coordinator.archive_ref_for_op("op-stamp")
    archived = coordinator.archive.lookup(ref)
    assert archived.review_branch is not None
    assert archived.review_branch.startswith("ouroboros/preview/")


# ===========================================================================
# mark_applied / mark_verified — post-decision lifecycle
# ===========================================================================


def test_mark_applied_updates_archive(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        coordinator.coordinate_review(
            "op-app", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=0.3,
        )
    )
    updated = coordinator.mark_applied(
        "op-app", DiffOutcome.APPLIED,
    )
    assert updated is not None
    assert updated.apply_outcome is DiffOutcome.APPLIED


def test_mark_verified_updates_archive(
    monkeypatch, coordinator: ReviewCoordinator,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        coordinator.coordinate_review(
            "op-ver", [("new.py", "ok\n")],
            risk_tier="notify_apply",
            timeout_s=0.3,
        )
    )
    coordinator.mark_applied("op-ver", DiffOutcome.APPLIED)
    updated = coordinator.mark_verified("op-ver", VerifyOutcome.PASSED)
    assert updated is not None
    assert updated.verify_outcome is VerifyOutcome.PASSED


def test_mark_applied_unknown_op_returns_none(
    coordinator: ReviewCoordinator,
):
    assert coordinator.mark_applied("never-existed", DiffOutcome.APPLIED) is None


def test_mark_verified_unknown_op_returns_none(
    coordinator: ReviewCoordinator,
):
    assert coordinator.mark_verified("never-existed", VerifyOutcome.PASSED) is None


# ===========================================================================
# record_accept / record_reject — defensive
# ===========================================================================


def test_record_accept_on_unknown_op_returns_false(
    coordinator: ReviewCoordinator,
):
    assert coordinator.record_accept("never-existed") is False
    assert coordinator.record_accept(None) is False


def test_record_reject_on_unknown_op_returns_false(
    coordinator: ReviewCoordinator,
):
    assert coordinator.record_reject("never-existed") is False


# ===========================================================================
# Late-bind branch manager
# ===========================================================================


def test_attach_branch_manager_late_binds(repo: Path):
    coord = ReviewCoordinator()
    assert coord.branch_manager is None
    coord.attach_branch_manager(ReviewBranchManager(repo))
    assert coord.branch_manager is not None


# ===========================================================================
# Orchestrator hook regression — AST grep
# ===========================================================================


def test_orchestrator_notify_apply_block_contains_review_hook():
    """Slice 3 hook MUST be present at the notify_apply site. A future
    refactor that removes it would silently regress the IDE-native
    review flow."""
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read()
    # Slice 3 marker comment + key imports + the coordinator call
    assert "Gap #4 Slice 3" in src
    assert "review_coordinator" in src
    assert "coordinate_review" in src
    assert "ReviewBranchManager" in src
