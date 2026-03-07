"""Tests for the Ouroboros Sandbox Improvement Loop.

Verifies that the SandboxLoop:
- NEVER modifies production files (sandbox isolation invariant)
- Emits the correct communication phases via CommProtocol
- Records state transitions in the OperationLedger
- Accepts syntactically valid Python candidates
- Rejects candidates with syntax errors
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.risk_engine import RiskEngine
from backend.core.ouroboros.governance.sandbox_loop import (
    SandboxConfig,
    SandboxLoop,
    SandboxResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal project with backend/example.py."""
    pkg = tmp_path / "backend"
    pkg.mkdir()
    example = pkg / "example.py"
    example.write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


@pytest.fixture
def transport() -> LogTransport:
    """Return a fresh LogTransport for capturing comm messages."""
    return LogTransport()


@pytest.fixture
def sandbox(tmp_project: Path, tmp_path: Path, transport: LogTransport) -> SandboxLoop:
    """Return a SandboxLoop wired with all governance components."""
    config = SandboxConfig(
        worktree_base=tmp_path / "worktrees",
        ledger_dir=tmp_path / "ledger",
    )
    comm = CommProtocol(transports=[transport])
    risk_engine = RiskEngine()
    ledger = OperationLedger(storage_dir=config.ledger_dir)
    return SandboxLoop(
        project_root=tmp_project,
        config=config,
        comm=comm,
        risk_engine=risk_engine,
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# TestSandboxIsolation
# ---------------------------------------------------------------------------


class TestSandboxIsolation:
    """Verify sandbox never touches production files."""

    @pytest.mark.asyncio
    async def test_production_files_unchanged(
        self, sandbox: SandboxLoop, tmp_project: Path
    ) -> None:
        """Mock _generate_candidates to return modified code; original must stay intact."""
        original_file = tmp_project / "backend" / "example.py"
        original_content = original_file.read_text()

        candidate = {
            "code": "def add(a, b):\n    return a + b + 1  # improved\n",
            "description": "off-by-one improvement",
        }

        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            result = await sandbox.run(
                goal="Improve add function",
                target_file="backend/example.py",
            )

        # Production file MUST be unchanged
        assert original_file.read_text() == original_content
        # Operation completed
        assert isinstance(result, SandboxResult)
        assert result.op_id is not None

    @pytest.mark.asyncio
    async def test_emits_comm_phases(
        self, sandbox: SandboxLoop, transport: LogTransport
    ) -> None:
        """Running the loop must emit at least INTENT and DECISION messages."""
        candidate = {
            "code": "def add(a, b):\n    return a + b\n",
            "description": "identity candidate",
        }

        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            await sandbox.run(
                goal="Test comm phases",
                target_file="backend/example.py",
            )

        msg_types = [m.msg_type for m in transport.messages]
        assert MessageType.INTENT in msg_types
        assert MessageType.DECISION in msg_types

    @pytest.mark.asyncio
    async def test_ledger_records_state(
        self, sandbox: SandboxLoop, tmp_path: Path
    ) -> None:
        """Running the loop must write at least 2 ledger entries (PLANNED + final)."""
        candidate = {
            "code": "def add(a, b):\n    return a + b\n",
            "description": "noop candidate",
        }

        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            result = await sandbox.run(
                goal="Test ledger",
                target_file="backend/example.py",
            )

        # Retrieve ledger history for this op_id
        ledger = OperationLedger(storage_dir=tmp_path / "ledger")
        history = await ledger.get_history(result.op_id)
        assert len(history) >= 2


# ---------------------------------------------------------------------------
# TestSandboxValidation
# ---------------------------------------------------------------------------


class TestSandboxValidation:
    """Verify AST-based candidate validation in sandbox."""

    @pytest.mark.asyncio
    async def test_valid_candidate_passes(self, sandbox: SandboxLoop) -> None:
        """Syntactically valid Python candidate should pass validation."""
        candidate = {
            "code": "def add(a, b):\n    return a + b + 1\n",
            "description": "valid improvement",
        }

        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[candidate],
        ):
            result = await sandbox.run(
                goal="Improve add",
                target_file="backend/example.py",
            )

        assert result.success is True
        assert result.best_candidate is not None
        assert result.candidates_generated == 1

    @pytest.mark.asyncio
    async def test_invalid_syntax_rejected(self, sandbox: SandboxLoop) -> None:
        """Candidate with syntax error should be rejected."""
        bad_candidate = {
            "code": "def broken(:\n    pass\n",
            "description": "broken syntax",
        }

        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[bad_candidate],
        ):
            result = await sandbox.run(
                goal="Break things",
                target_file="backend/example.py",
            )

        assert result.success is False
        assert result.best_candidate is None
        assert result.candidates_generated == 1
