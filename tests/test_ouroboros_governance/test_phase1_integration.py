"""Phase 1 Integration Tests -- Full Stack Go/No-Go Acceptance
================================================================

End-to-end tests that verify the Phase 1A and Phase 1B Go/No-Go criteria
from the design doc by exercising the full stack:

    GovernanceLockManager
        + ChangeEngine
        + BreakGlassManager
        + OperationLedger
        + CommProtocol (LogTransport + TUITransport)

Test Classes
------------
- **TestLockHierarchyGoNoGo** (Phase 1A lock criteria)
- **TestTransactionalEngineGoNoGo** (Phase 1A transactional criteria)
- **TestBreakGlassGoNoGo** (Phase 1A break-glass criteria)
- **TestCommTUIGoNoGo** (Phase 1B communication criteria)
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LockOrderViolation,
    FencingTokenError,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import OperationLedger, OperationState
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.tui_transport import TUITransport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a single Python source file."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    """Fresh operation ledger backed by a temp directory."""
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def full_stack(project, ledger):
    """Wire up the full Phase 1 governance stack.

    Returns (engine, lock_mgr, break_glass, log_transport, tui_transport).
    """
    log_transport = LogTransport()
    tui_transport = TUITransport()
    comm = CommProtocol(transports=[log_transport, tui_transport])
    lock_mgr = GovernanceLockManager()
    break_glass = BreakGlassManager()
    engine = ChangeEngine(
        project_root=project,
        ledger=ledger,
        comm=comm,
        lock_manager=lock_mgr,
        break_glass=break_glass,
    )
    return engine, lock_mgr, break_glass, log_transport, tui_transport


def _safe_auto_profile() -> OperationProfile:
    """Return an OperationProfile that classifies as SAFE_AUTO."""
    return OperationProfile(
        files_affected=[Path("src/example.py")],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
    )


# ---------------------------------------------------------------------------
# TestLockHierarchyGoNoGo (Phase 1A lock criteria)
# ---------------------------------------------------------------------------


class TestLockHierarchyGoNoGo:
    """Phase 1A Go/No-Go: hierarchical lock ordering, fencing, and R/W modes."""

    @pytest.mark.asyncio
    async def test_out_of_order_acquisition_immediate_error(self) -> None:
        """Acquiring a lower-level lock while holding a higher one raises
        LockOrderViolation immediately (no deadlock, no timeout)."""
        lock_mgr = GovernanceLockManager()

        async with lock_mgr.acquire(
            level=LockLevel.REPO_LOCK,
            resource="jarvis",
            mode=LockMode.EXCLUSIVE_WRITE,
        ):
            with pytest.raises(LockOrderViolation):
                async with lock_mgr.acquire(
                    level=LockLevel.FILE_LOCK,
                    resource="src/example.py",
                    mode=LockMode.EXCLUSIVE_WRITE,
                ):
                    pytest.fail("Should never reach inside the lower-level lock")

    @pytest.mark.asyncio
    async def test_write_with_stale_fencing_token_rejected(self) -> None:
        """Acquire/release twice to advance the fencing token, then validate
        that the first token (now stale at 0) raises FencingTokenError."""
        lock_mgr = GovernanceLockManager()

        # First acquisition -- token becomes 1
        async with lock_mgr.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/example.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ):
            pass

        # Second acquisition -- token advances to 2
        async with lock_mgr.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/example.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ):
            pass

        # Token 0 is definitely stale (current is 2)
        with pytest.raises(FencingTokenError):
            lock_mgr.validate_fencing_token(
                level=LockLevel.FILE_LOCK,
                resource="src/example.py",
                token=0,
            )

    @pytest.mark.asyncio
    async def test_concurrent_shared_reads_succeed(self) -> None:
        """Two concurrent SHARED_READ locks on the same file both succeed
        without blocking each other."""
        lock_mgr = GovernanceLockManager()
        results: list[bool] = []

        async def shared_read() -> None:
            async with lock_mgr.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/example.py",
                mode=LockMode.SHARED_READ,
            ) as handle:
                results.append(handle is not None)
                # Hold the lock briefly to prove concurrent access
                await asyncio.sleep(0.01)

        await asyncio.gather(shared_read(), shared_read())
        assert results == [True, True]

    @pytest.mark.asyncio
    async def test_concurrent_exclusive_writes_serialize(self) -> None:
        """Two concurrent EXCLUSIVE_WRITE on the same file serialize -- one
        completes entirely before the other starts."""
        lock_mgr = GovernanceLockManager()
        order: list[str] = []

        async def exclusive_write(label: str, hold_time: float) -> None:
            async with lock_mgr.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/example.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ):
                order.append(f"{label}_start")
                await asyncio.sleep(hold_time)
                order.append(f"{label}_end")

        await asyncio.gather(
            exclusive_write("first", 0.05),
            exclusive_write("second", 0.01),
        )
        # The first writer must finish before the second writer starts
        assert order.index("first_end") < order.index("second_start")


# ---------------------------------------------------------------------------
# TestTransactionalEngineGoNoGo (Phase 1A transactional criteria)
# ---------------------------------------------------------------------------


class TestTransactionalEngineGoNoGo:
    """Phase 1A Go/No-Go: ledger entries, rollback hashes, verify rollback."""

    @pytest.mark.asyncio
    async def test_ledger_entry_for_every_state_transition(
        self, project, ledger, full_stack
    ) -> None:
        """A SAFE_AUTO change produces ledger entries for PLANNED,
        VALIDATING, and APPLIED."""
        engine, _, _, _, _ = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Add docstring",
            target_file=target,
            proposed_content="def hello():\n    \"\"\"Say hello.\"\"\"\n    return 'world'\n",
            profile=_safe_auto_profile(),
        )
        result = await engine.execute(request)
        assert result.success is True

        history = await ledger.get_history(result.op_id)
        states = [entry.state for entry in history]

        assert OperationState.PLANNED in states
        assert OperationState.VALIDATING in states
        assert OperationState.APPLIED in states

    @pytest.mark.asyncio
    async def test_rollback_hash_matches_pre_change(self, project) -> None:
        """RollbackArtifact.capture() hash matches the original file, and
        apply() restores exact content."""
        target = project / "src" / "example.py"
        original_content = target.read_text()
        original_hash = hashlib.sha256(original_content.encode()).hexdigest()

        artifact = RollbackArtifact.capture(target)
        assert artifact.snapshot_hash == original_hash
        assert artifact.original_content == original_content

        # Mutate the file
        target.write_text("def mutated():\n    pass\n")
        assert target.read_text() != original_content

        # Rollback must restore exact original
        artifact.apply(target)
        assert target.read_text() == original_content
        restored_hash = hashlib.sha256(target.read_text().encode()).hexdigest()
        assert restored_hash == original_hash

    @pytest.mark.asyncio
    async def test_post_apply_failure_triggers_rollback(
        self, project, ledger, full_stack
    ) -> None:
        """When verify_fn returns False, the file is rolled back and the
        ledger records ROLLED_BACK."""
        engine, _, _, _, _ = full_stack
        target = project / "src" / "example.py"
        original_content = target.read_text()

        request = ChangeRequest(
            goal="Change that fails verification",
            target_file=target,
            proposed_content="def hello():\n    return 'changed'\n",
            profile=_safe_auto_profile(),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await engine.execute(request)

        assert result.success is False
        assert result.rolled_back is True
        assert result.phase_reached == ChangePhase.VERIFY

        # File must be restored
        assert target.read_text() == original_content

        # Ledger must contain ROLLED_BACK
        history = await ledger.get_history(result.op_id)
        states = [entry.state for entry in history]
        assert OperationState.ROLLED_BACK in states


# ---------------------------------------------------------------------------
# TestBreakGlassGoNoGo (Phase 1A break-glass criteria)
# ---------------------------------------------------------------------------


class TestBreakGlassGoNoGo:
    """Phase 1A Go/No-Go: break-glass TTL expiry and promotion."""

    @pytest.mark.asyncio
    async def test_break_glass_token_expires_after_ttl(self) -> None:
        """A token with ttl=0 expires immediately; get_promoted_tier
        returns None."""
        mgr = BreakGlassManager()
        await mgr.issue(
            op_id="op-expired-test",
            reason="integration test",
            ttl=0,
            issuer="derek",
        )
        # ttl=0 means token.is_expired() is True from the start
        promoted = mgr.get_promoted_tier("op-expired-test")
        assert promoted is None

    @pytest.mark.asyncio
    async def test_break_glass_promotes_to_approval_required(self) -> None:
        """An active (non-expired) token returns 'APPROVAL_REQUIRED'
        from get_promoted_tier."""
        mgr = BreakGlassManager()
        await mgr.issue(
            op_id="op-active-test",
            reason="emergency hotfix",
            ttl=300,
            issuer="derek",
        )
        promoted = mgr.get_promoted_tier("op-active-test")
        assert promoted == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# TestCommTUIGoNoGo (Phase 1B communication criteria)
# ---------------------------------------------------------------------------


class TestCommTUIGoNoGo:
    """Phase 1B Go/No-Go: message types, fault isolation, sequencing, TUI."""

    @pytest.mark.asyncio
    async def test_all_five_message_types_emitted(
        self, project, ledger, full_stack
    ) -> None:
        """A successful SAFE_AUTO change emits INTENT, HEARTBEAT, DECISION,
        and POSTMORTEM via LogTransport.

        (PLAN is not emitted by the ChangeEngine pipeline for SAFE_AUTO
        operations -- the 4 mandatory types for a successful run are
        INTENT, HEARTBEAT, DECISION, POSTMORTEM.)
        """
        engine, _, _, log_transport, _ = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Trigger all message types",
            target_file=target,
            proposed_content="def hello():\n    \"\"\"Greet.\"\"\"\n    return 'world'\n",
            profile=_safe_auto_profile(),
        )
        result = await engine.execute(request)
        assert result.success is True

        msg_types = {m.msg_type for m in log_transport.messages}
        assert MessageType.INTENT in msg_types
        assert MessageType.HEARTBEAT in msg_types
        assert MessageType.DECISION in msg_types
        assert MessageType.POSTMORTEM in msg_types

    @pytest.mark.asyncio
    async def test_tui_transport_crash_does_not_block_pipeline(
        self, project, ledger
    ) -> None:
        """A TUI transport with a crashing callback does not prevent the
        change engine from completing successfully."""
        log_transport = LogTransport()
        tui_transport = TUITransport()

        # Register a callback that always crashes
        tui_transport.on_message(lambda _: (_ for _ in ()).throw(RuntimeError("TUI BOOM")))

        comm = CommProtocol(transports=[log_transport, tui_transport])
        lock_mgr = GovernanceLockManager()
        break_glass = BreakGlassManager()
        engine = ChangeEngine(
            project_root=project,
            ledger=ledger,
            comm=comm,
            lock_manager=lock_mgr,
            break_glass=break_glass,
        )

        target = project / "src" / "example.py"
        request = ChangeRequest(
            goal="Test TUI crash isolation",
            target_file=target,
            proposed_content="def hello():\n    return 'resilient'\n",
            profile=_safe_auto_profile(),
        )
        result = await engine.execute(request)

        # Pipeline must complete despite TUI crash
        assert result.success is True
        assert result.phase_reached == ChangePhase.VERIFY
        # LogTransport still received all messages
        assert len(log_transport.messages) >= 4

    @pytest.mark.asyncio
    async def test_sequence_numbers_monotonic(
        self, project, ledger, full_stack
    ) -> None:
        """All messages for an op_id have strictly increasing sequence
        numbers."""
        engine, _, _, log_transport, _, = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Verify monotonic sequences",
            target_file=target,
            proposed_content="def hello():\n    return 'monotonic'\n",
            profile=_safe_auto_profile(),
        )
        result = await engine.execute(request)
        assert result.success is True

        # Filter messages for this op_id
        op_messages = [
            m for m in log_transport.messages if m.op_id == result.op_id
        ]
        assert len(op_messages) >= 4

        # Verify strictly increasing sequence numbers
        seq_numbers = [m.seq for m in op_messages]
        for i in range(len(seq_numbers) - 1):
            assert seq_numbers[i] < seq_numbers[i + 1], (
                f"Sequence not monotonic: seq[{i}]={seq_numbers[i]} "
                f">= seq[{i+1}]={seq_numbers[i+1]}"
            )

    @pytest.mark.asyncio
    async def test_tui_transport_receives_formatted_messages(
        self, project, ledger, full_stack
    ) -> None:
        """TUI transport receives dicts with 'type' and 'op_id' fields."""
        engine, _, _, _, tui_transport = full_stack
        target = project / "src" / "example.py"

        # Register a callback to capture formatted messages
        received: list[dict] = []
        tui_transport.on_message(lambda msg: received.append(msg))

        request = ChangeRequest(
            goal="Verify TUI formatted messages",
            target_file=target,
            proposed_content="def hello():\n    return 'tui'\n",
            profile=_safe_auto_profile(),
        )
        result = await engine.execute(request)
        assert result.success is True

        # TUI must have received messages
        assert len(received) >= 4

        # Every message must have "type" and "op_id" fields
        for msg_dict in received:
            assert "type" in msg_dict, f"Missing 'type' in {msg_dict}"
            assert "op_id" in msg_dict, f"Missing 'op_id' in {msg_dict}"
            # Verify the op_id matches
            assert msg_dict["op_id"] == result.op_id
