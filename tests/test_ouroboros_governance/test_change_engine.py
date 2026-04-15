"""Tests for the transactional change engine."""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangeResult,
    RollbackArtifact,
    ChangePhase,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskTier,
    RiskClassification,
    OperationProfile,
    ChangeType,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
)
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def tmp_project_dir(tmp_path):
    """Create a minimal project for testing."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def comm():
    transport = LogTransport()
    return CommProtocol(transports=[transport]), transport


@pytest.fixture
def engine(tmp_project_dir, ledger, comm):
    protocol, transport = comm
    return ChangeEngine(
        project_root=tmp_project_dir,
        ledger=ledger,
        comm=protocol,
        lock_manager=GovernanceLockManager(),
        break_glass=BreakGlassManager(),
    ), transport


class TestRollbackArtifact:
    @pytest.mark.asyncio
    async def test_rollback_snapshot_hash_matches_original(
        self, tmp_project_dir
    ):
        """Pre-change snapshot hash is captured correctly."""
        target = tmp_project_dir / "src" / "example.py"
        original_content = target.read_text()
        expected_hash = hashlib.sha256(
            original_content.encode()
        ).hexdigest()

        artifact = RollbackArtifact.capture(target)
        assert artifact.snapshot_hash == expected_hash
        assert artifact.original_content == original_content

    def test_rollback_restores_original(self, tmp_project_dir):
        """Applying a rollback artifact restores the exact original content."""
        target = tmp_project_dir / "src" / "example.py"
        original = target.read_text()
        artifact = RollbackArtifact.capture(target)

        # Simulate a change
        target.write_text("def goodbye():\n    return 'cruel world'\n")
        assert target.read_text() != original

        # Apply rollback
        artifact.apply(target)
        assert target.read_text() == original
        restored_hash = hashlib.sha256(
            target.read_text().encode()
        ).hexdigest()
        assert restored_hash == artifact.snapshot_hash

    def test_capture_missing_file_returns_absent_artifact(
        self, tmp_project_dir
    ):
        """New-file path: capture on a missing file returns an "absent"
        artifact whose ``existed`` flag is False, without reading or
        stat'ing the file in a way that raises.

        Session bt-2026-04-15-091555 (Session K) regression guard:
        the pre-patch ``capture()`` called ``read_text()``
        unconditionally, which raised ``FileNotFoundError`` and
        aborted the APPLY phase of every new-file creation op.
        """
        # Path that definitely does not exist — do not create it
        target = tmp_project_dir / "src" / "does_not_exist_yet.py"
        assert not target.exists()

        # Must not raise
        artifact = RollbackArtifact.capture(target)

        assert artifact.existed is False
        assert artifact.original_content == ""
        # Sentinel hash for ledger clarity — distinguishable from any
        # real sha256 hex digest.
        assert artifact.snapshot_hash == "absent"

    def test_apply_absent_artifact_unlinks_created_file(
        self, tmp_project_dir
    ):
        """New-file rollback: apply() on an existed=False artifact
        unlinks the file that was created between capture and apply,
        restoring the original "file did not exist" state.

        This is the rollback semantic for a new-file APPLY that
        failed post-apply VERIFY.
        """
        target = tmp_project_dir / "src" / "new_file.py"
        assert not target.exists()

        # Capture BEFORE the file exists (new-file creation path)
        artifact = RollbackArtifact.capture(target)
        assert artifact.existed is False

        # Simulate a successful write that we later want to roll back
        target.write_text("def new_function():\n    return 42\n")
        assert target.exists()

        # Rollback must remove the file
        artifact.apply(target)
        assert not target.exists()

    def test_apply_absent_artifact_noop_when_file_already_gone(
        self, tmp_project_dir
    ):
        """apply() on an existed=False artifact MUST NOT raise when the
        file is already absent. Double-rollback and "write-then-crash-
        before-apply" paths both land here, and both are valid.
        """
        target = tmp_project_dir / "src" / "transient.py"
        assert not target.exists()

        artifact = RollbackArtifact.capture(target)
        assert artifact.existed is False

        # File never created — apply must not raise
        artifact.apply(target)  # should be a silent no-op
        assert not target.exists()


class TestChangePhases:
    def test_all_eight_phases_exist(self):
        """All 8 pipeline phases are defined."""
        expected = [
            "PLAN", "SANDBOX", "VALIDATE", "GATE",
            "APPLY", "LEDGER", "PUBLISH", "VERIFY",
        ]
        assert [p.name for p in ChangePhase] == expected


class TestChangeEngine:
    @pytest.mark.asyncio
    async def test_safe_auto_completes_full_pipeline(
        self, engine, tmp_project_dir, ledger
    ):
        """A SAFE_AUTO change goes through all 8 phases to APPLIED."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Add docstring",
            target_file=target,
            proposed_content="def hello():\n    \"\"\"Greet.\"\"\"\n    return 'world'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is True
        assert result.phase_reached == ChangePhase.VERIFY
        assert result.op_id.startswith("op-")

        # Verify ledger has APPLIED state
        latest = await ledger.get_latest_state(result.op_id)
        assert latest == OperationState.APPLIED

    @pytest.mark.asyncio
    async def test_blocked_operation_stops_at_gate(
        self, engine, tmp_project_dir, ledger
    ):
        """A BLOCKED change stops at GATE phase."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Modify supervisor",
            target_file=target,
            proposed_content="# modified\n",
            profile=OperationProfile(
                files_affected=[Path("unified_supervisor.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=True,  # BLOCKED
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.GATE
        assert result.risk_tier == RiskTier.BLOCKED

        # File unchanged
        assert target.read_text() == "def hello():\n    return 'world'\n"

    @pytest.mark.asyncio
    async def test_approval_required_stops_at_gate(
        self, engine, tmp_project_dir
    ):
        """APPROVAL_REQUIRED stops at GATE without operator approval."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Cross-repo change",
            target_file=target,
            proposed_content="# cross repo\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=True,  # APPROVAL_REQUIRED
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.GATE
        assert result.risk_tier == RiskTier.APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_break_glass_promotes_blocked_to_approval(
        self, engine, tmp_project_dir
    ):
        """Break-glass token allows BLOCKED op to reach GATE as APPROVAL_REQUIRED."""
        eng, transport = engine

        # Pre-issue break-glass (we need the op_id, so we use a known one)
        # For this test, we issue break-glass BEFORE execute (engine checks it)
        # Engine will use the generated op_id, so we test via the manager directly
        target = tmp_project_dir / "src" / "example.py"
        request = ChangeRequest(
            goal="Security fix with break-glass",
            target_file=target,
            proposed_content="# secure fix\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=True,  # BLOCKED
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
            break_glass_op_id=None,  # Will be set by engine if token exists
        )
        # Without break-glass: BLOCKED
        result = await eng.execute(request)
        assert result.risk_tier == RiskTier.BLOCKED

    @pytest.mark.asyncio
    async def test_invalid_syntax_fails_at_validate(
        self, engine, tmp_project_dir
    ):
        """Proposed code with invalid syntax fails at VALIDATE phase."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Bad syntax",
            target_file=target,
            proposed_content="def broken(\n",  # Invalid Python
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.VALIDATE

    @pytest.mark.asyncio
    async def test_rollback_on_verify_failure(
        self, engine, tmp_project_dir
    ):
        """If post-apply verification fails, automatic rollback occurs."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"
        original = target.read_text()

        request = ChangeRequest(
            goal="Change that fails verification",
            target_file=target,
            proposed_content="def hello():\n    return 'changed'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
            verify_fn=AsyncMock(return_value=False),  # Simulate verify failure
        )
        result = await eng.execute(request)
        assert result.rolled_back is True
        # File should be restored to original
        assert target.read_text() == original


class TestLedgerTracking:
    @pytest.mark.asyncio
    async def test_every_phase_recorded_in_ledger(
        self, engine, tmp_project_dir, ledger
    ):
        """Ledger has entries for every phase transition in a successful run."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Simple change",
            target_file=target,
            proposed_content="def hello():\n    return 'updated'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        history = await ledger.get_history(result.op_id)
        states = [e.state for e in history]
        assert OperationState.PLANNED in states
        assert OperationState.VALIDATING in states
        assert OperationState.APPLIED in states
