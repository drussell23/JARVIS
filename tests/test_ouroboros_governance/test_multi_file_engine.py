# tests/test_ouroboros_governance/test_multi_file_engine.py
"""Tests for multi-file atomic change engine."""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
    MultiFileChangeResult,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeRequest,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.lock_manager import GovernanceLockManager
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def project(tmp_path):
    """Create a project with multiple files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo():\n    return 1\n")
    (src / "bar.py").write_text("def bar():\n    return 2\n")
    (src / "baz.py").write_text("def baz():\n    return 3\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def engine(project, ledger):
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    return MultiFileChangeEngine(
        project_root=project,
        ledger=ledger,
        comm=comm,
        lock_manager=GovernanceLockManager(),
        break_glass=BreakGlassManager(),
    ), transport


def _safe_profile(*files):
    return OperationProfile(
        files_affected=[Path(f) for f in files],
        change_type=ChangeType.MODIFY,
        blast_radius=len(files),
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
    )


class TestAtomicMultiFile:
    @pytest.mark.asyncio
    async def test_all_files_applied_on_success(self, engine, project):
        """All files are modified when all changes succeed."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Update all files",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        assert result.success is True
        assert (project / "src" / "foo.py").read_text() == "def foo():\n    return 10\n"
        assert (project / "src" / "bar.py").read_text() == "def bar():\n    return 20\n"

    @pytest.mark.asyncio
    async def test_all_files_rolled_back_on_verify_failure(self, engine, project):
        """All files are restored when post-apply verification fails."""
        eng, _ = engine
        original_foo = (project / "src" / "foo.py").read_text()
        original_bar = (project / "src" / "bar.py").read_text()

        request = MultiFileChangeRequest(
            goal="Change that fails verify",
            files={
                project / "src" / "foo.py": "def foo():\n    return 100\n",
                project / "src" / "bar.py": "def bar():\n    return 200\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.rolled_back is True
        assert (project / "src" / "foo.py").read_text() == original_foo
        assert (project / "src" / "bar.py").read_text() == original_bar

    @pytest.mark.asyncio
    async def test_invalid_syntax_in_one_file_blocks_all(self, engine, project):
        """If any file has invalid syntax, no files are applied."""
        eng, _ = engine
        original_foo = (project / "src" / "foo.py").read_text()
        original_bar = (project / "src" / "bar.py").read_text()

        request = MultiFileChangeRequest(
            goal="One bad file",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",  # Valid
                project / "src" / "bar.py": "def bar(\n",  # Invalid syntax
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.VALIDATE
        # Neither file should be modified
        assert (project / "src" / "foo.py").read_text() == original_foo
        assert (project / "src" / "bar.py").read_text() == original_bar

    @pytest.mark.asyncio
    async def test_blocked_profile_stops_at_gate(self, engine, project):
        """BLOCKED risk tier stops at GATE, no files modified."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Touches supervisor",
            files={
                project / "src" / "foo.py": "# modified\n",
            },
            profile=OperationProfile(
                files_affected=[Path("unified_supervisor.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=True,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.risk_tier == RiskTier.BLOCKED


class TestLedgerTracking:
    @pytest.mark.asyncio
    async def test_ledger_records_file_list(self, engine, project, ledger):
        """Ledger PLANNED entry includes the list of all files."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Multi-file change",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
        )
        result = await eng.execute(request)
        history = await ledger.get_history(result.op_id)
        planned = [e for e in history if e.state == OperationState.PLANNED][0]
        assert "files" in planned.data
        assert len(planned.data["files"]) == 2

    @pytest.mark.asyncio
    async def test_rollback_recorded_in_ledger(self, engine, project, ledger):
        """ROLLED_BACK state recorded when verify fails."""
        eng, _ = engine
        request = MultiFileChangeRequest(
            goal="Fails verify",
            files={
                project / "src" / "foo.py": "def foo():\n    return 999\n",
            },
            profile=_safe_profile("src/foo.py"),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await eng.execute(request)
        latest = await ledger.get_latest_state(result.op_id)
        assert latest == OperationState.ROLLED_BACK


class TestCommProtocol:
    @pytest.mark.asyncio
    async def test_all_message_types_emitted(self, engine, project):
        """Multi-file operation emits INTENT, HEARTBEAT, DECISION, POSTMORTEM."""
        eng, transport = engine
        request = MultiFileChangeRequest(
            goal="Emit all messages",
            files={
                project / "src" / "foo.py": "def foo():\n    return 42\n",
            },
            profile=_safe_profile("src/foo.py"),
        )
        result = await eng.execute(request)
        assert result.success is True
        types = {m.msg_type for m in transport.messages}
        assert MessageType.INTENT in types
        assert MessageType.HEARTBEAT in types
        assert MessageType.DECISION in types
        assert MessageType.POSTMORTEM in types


class TestRollbackIntegrity:
    @pytest.mark.asyncio
    async def test_rollback_hashes_match_originals(self, engine, project):
        """Each file's rollback hash matches its pre-change hash."""
        eng, _ = engine
        foo_hash = hashlib.sha256(
            (project / "src" / "foo.py").read_text().encode()
        ).hexdigest()
        bar_hash = hashlib.sha256(
            (project / "src" / "bar.py").read_text().encode()
        ).hexdigest()

        request = MultiFileChangeRequest(
            goal="Hash integrity",
            files={
                project / "src" / "foo.py": "def foo():\n    return 10\n",
                project / "src" / "bar.py": "def bar():\n    return 20\n",
            },
            profile=_safe_profile("src/foo.py", "src/bar.py"),
            verify_fn=AsyncMock(return_value=False),  # Force rollback
        )
        result = await eng.execute(request)

        # After rollback, files should match original hashes
        assert hashlib.sha256(
            (project / "src" / "foo.py").read_text().encode()
        ).hexdigest() == foo_hash
        assert hashlib.sha256(
            (project / "src" / "bar.py").read_text().encode()
        ).hexdigest() == bar_hash
