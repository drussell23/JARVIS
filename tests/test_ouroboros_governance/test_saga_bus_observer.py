"""Tests for SagaMessageBus passive observer wiring in SagaApplyStrategy.

TDD tests for Task 3: verify that SagaApplyStrategy emits lifecycle events
to an optional SagaMessageBus without affecting execution.

Three test classes:
  - TestBusReceivesLifecycleEvents: apply emits SAGA_CREATED + SAGA_ADVANCED,
    promote emits SAGA_COMPLETED; all payloads carry schema_version=1.0
  - TestBusIsOptional: message_bus=None works fine (no crash)
  - TestBusFailureIsolation: broken bus (raises RuntimeError) doesn't break saga
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessage,
    SagaMessageBus,
    SagaMessageType,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.saga_apply_strategy import (
    SagaApplyStrategy,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> str:
    """Initialize a git repo with one commit on branch 'main'. Returns HEAD SHA."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--no-verify"],
        cwd=str(path), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    (path / ".jarvis").mkdir(exist_ok=True)
    return result.stdout.strip()


@pytest.fixture
def git_repos(tmp_path: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Create two git repos. Returns (repo_roots, base_shas)."""
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime"):
        root = tmp_path / name
        root.mkdir()
        sha = _init_repo(root)
        roots[name] = root
        shas[name] = sha
    return roots, shas


def _make_ctx(
    repo_scope: Tuple[str, ...] = ("jarvis", "prime"),
    repo_snapshots: Tuple[Tuple[str, str], ...] = (),
    op_id: str = "test-op-bus",
) -> OperationContext:
    return OperationContext.create(
        target_files=("test.py",),
        description="Test saga bus observer",
        op_id=op_id,
        repo_scope=repo_scope,
        repo_snapshots=repo_snapshots,
        saga_id=f"saga-{op_id}",
    )


def _make_patch(
    repo: str, file_path: str = "src/test.py", content: str = "# new\n"
) -> RepoPatch:
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
        new_content=((file_path, content.encode()),),
    )


# ---------------------------------------------------------------------------
# TestBusReceivesLifecycleEvents
# ---------------------------------------------------------------------------


class TestBusReceivesLifecycleEvents:
    """Verify that execute/promote emit the expected lifecycle messages."""

    async def test_legacy_apply_emits_created_and_advanced(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """Legacy (non-branch-isolation) apply should emit SAGA_CREATED + SAGA_ADVANCED per repo."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis", "prime"),
            repo_snapshots=(("jarvis", shas["jarvis"]), ("prime", shas["prime"])),
        )
        patch_map = {
            "jarvis": _make_patch("jarvis"),
            "prime": _make_patch("prime"),
        }
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        created_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_CREATED)
        assert len(created_msgs) >= 1

        advanced_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_ADVANCED)
        assert len(advanced_msgs) >= 2  # one per repo

    async def test_bplus_apply_emits_created_and_advanced(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """B+ (branch-isolated) apply should emit SAGA_CREATED + SAGA_ADVANCED per repo."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=True,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis", "prime"),
            repo_snapshots=(("jarvis", shas["jarvis"]), ("prime", shas["prime"])),
        )
        patch_map = {
            "jarvis": _make_patch("jarvis"),
            "prime": _make_patch("prime"),
        }
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        created_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_CREATED)
        assert len(created_msgs) >= 1

        advanced_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_ADVANCED)
        assert len(advanced_msgs) >= 2

    async def test_promote_emits_completed(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """After successful promote_all, a SAGA_COMPLETED event should be emitted."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=True,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis", "prime"),
            repo_snapshots=(("jarvis", shas["jarvis"]), ("prime", shas["prime"])),
        )
        patch_map = {
            "jarvis": _make_patch("jarvis"),
            "prime": _make_patch("prime"),
        }
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        terminal, promoted = await strategy.promote_all(
            apply_order=["jarvis", "prime"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert terminal == SagaTerminalState.SAGA_SUCCEEDED

        completed_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_COMPLETED)
        assert len(completed_msgs) >= 1

    async def test_all_payloads_have_schema_version(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """Every bus message payload must contain schema_version='1.0'."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        await strategy.execute(ctx, patch_map)

        all_msgs = bus.get_messages()
        assert len(all_msgs) > 0, "Expected at least one bus message"
        for msg in all_msgs:
            assert msg.payload.get("schema_version") == "1.0", (
                f"Message {msg.message_type} missing schema_version=1.0: {msg.payload}"
            )

    async def test_payloads_contain_op_id_and_saga_id(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """Every bus message payload must contain op_id and saga_id."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        await strategy.execute(ctx, patch_map)

        all_msgs = bus.get_messages()
        assert len(all_msgs) > 0
        for msg in all_msgs:
            assert "op_id" in msg.payload, f"Missing op_id in {msg.message_type}"
            assert "saga_id" in msg.payload, f"Missing saga_id in {msg.message_type}"


# ---------------------------------------------------------------------------
# TestBusIsOptional
# ---------------------------------------------------------------------------


class TestBusIsOptional:
    """Verify that message_bus=None is perfectly fine."""

    async def test_none_bus_legacy_apply(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """Legacy path with message_bus=None should work without error."""
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
            message_bus=None,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

    async def test_default_bus_is_none(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """If message_bus not provided at all, it defaults to None and works fine."""
        roots, shas = git_repos
        # Omit message_bus kwarg entirely
        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED


# ---------------------------------------------------------------------------
# TestBusFailureIsolation
# ---------------------------------------------------------------------------


class TestBusFailureIsolation:
    """Verify that a broken bus does not break saga execution."""

    async def test_broken_bus_legacy_apply_succeeds(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """If bus.send() raises RuntimeError, the saga should still complete."""
        roots, shas = git_repos

        broken_bus = MagicMock(spec=SagaMessageBus)
        broken_bus.send.side_effect = RuntimeError("Bus is on fire")

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=False,
            message_bus=broken_bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

    async def test_broken_bus_bplus_apply_succeeds(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """B+ path with a broken bus should still succeed."""
        roots, shas = git_repos

        broken_bus = MagicMock(spec=SagaMessageBus)
        broken_bus.send.side_effect = RuntimeError("Bus is on fire")

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=True,
            message_bus=broken_bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

    async def test_broken_bus_promote_succeeds(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """Promote should succeed even when bus.send() raises."""
        roots, shas = git_repos

        broken_bus = MagicMock(spec=SagaMessageBus)
        broken_bus.send.side_effect = RuntimeError("Bus exploded")

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=True,
            message_bus=broken_bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        terminal, promoted = await strategy.promote_all(
            apply_order=["jarvis"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert terminal == SagaTerminalState.SAGA_SUCCEEDED


# ---------------------------------------------------------------------------
# TestDifferentiatedPromoteFailureEvents
# ---------------------------------------------------------------------------


class TestDifferentiatedPromoteFailureEvents:
    """Verify that promote_all emits differentiated TARGET_MOVED and
    ANCESTRY_VIOLATION bus events alongside the catch-all saga_partial_promote."""

    async def test_target_moved_emits_differentiated_event(
        self, git_repos: Tuple[Dict[str, Path], Dict[str, str]]
    ) -> None:
        """When target branch moves, both saga_partial_promote AND target_moved fire."""
        roots, shas = git_repos
        bus = SagaMessageBus()

        strategy = SagaApplyStrategy(
            repo_roots=roots,
            ledger=None,
            branch_isolation=True,
            message_bus=bus,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis", "prime"),
            repo_snapshots=(("jarvis", shas["jarvis"]), ("prime", shas["prime"])),
            op_id="test-target-moved",
        )
        patch_map = {
            "jarvis": _make_patch("jarvis"),
            "prime": _make_patch("prime"),
        }
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Record the saga branch for jarvis so we can re-checkout later
        saga_branch = strategy._saga_branches["jarvis"]

        # Simulate TARGET_MOVED: advance main in jarvis behind the saga's back
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )
        (roots["jarvis"] / "external_change.py").write_text("# external\n")
        subprocess.run(
            ["git", "add", "external_change.py"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "external push", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )

        # Switch back to saga branch so promote_all finds us in the right state
        subprocess.run(
            ["git", "checkout", saga_branch],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )

        terminal, promoted = await strategy.promote_all(
            apply_order=["jarvis", "prime"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert terminal == SagaTerminalState.SAGA_PARTIAL_PROMOTE
        assert "jarvis" not in promoted  # jarvis failed to promote

        # Verify catch-all event fired
        partial_msgs = bus.get_messages(message_type=SagaMessageType.SAGA_PARTIAL_PROMOTE)
        assert len(partial_msgs) >= 1, "Expected saga_partial_promote event"

        # Verify differentiated target_moved event also fired
        moved_msgs = bus.get_messages(message_type=SagaMessageType.TARGET_MOVED)
        assert len(moved_msgs) >= 1, "Expected target_moved event"
        assert "TARGET_MOVED" in moved_msgs[0].payload.get("reason_code", "")
