"""Phase 0 Integration Tests — Full Governance Pipeline Acceptance
=================================================================

End-to-end tests that validate the Phase 0 Go/No-Go criteria by exercising
the complete governance pipeline:

    SupervisorOuroborosController
        -> ContractGate
        -> RiskEngine
        -> SandboxLoop
        -> OperationLedger
        -> CommProtocol (LogTransport)

Every test in this module proves a specific Phase 0 acceptance criterion:

1. Full pipeline runs in SANDBOX mode without touching production files.
2. Supervisor-touching operations are unconditionally BLOCKED.
3. Contract gate blocks autonomy on major version mismatch.
4. Risk classification is fully deterministic (1000x replay).
5. Safe mode restricts to read-only with interactive access.
6. Emergency stop cannot be resumed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.contract_gate import (
    ContractGate,
    ContractVersion,
)
from backend.core.ouroboros.governance.ledger import OperationLedger, OperationState
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
    RiskTier,
)
from backend.core.ouroboros.governance.sandbox_loop import SandboxConfig, SandboxLoop
from backend.core.ouroboros.governance.supervisor_controller import (
    AutonomyMode,
    SupervisorOuroborosController,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal project with a single Python source file."""
    pkg = tmp_path / "backend"
    pkg.mkdir()
    example = pkg / "example.py"
    example.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def transport() -> LogTransport:
    """Return a fresh LogTransport for capturing comm messages."""
    return LogTransport()


# ---------------------------------------------------------------------------
# TestPhase0Pipeline
# ---------------------------------------------------------------------------


class TestPhase0Pipeline:
    """Acceptance tests for the full Phase 0 governance pipeline."""

    # ---- 1. Full pipeline in SANDBOX mode --------------------------------

    @pytest.mark.asyncio
    async def test_full_pipeline_sandbox_mode(
        self, tmp_path: Path, tmp_project: Path, transport: LogTransport
    ) -> None:
        """End-to-end: supervisor -> contract gate -> risk engine -> sandbox
        loop -> ledger -> comm protocol.

        Acceptance criteria:
        - Controller starts in SANDBOX mode.
        - Contract gate passes with compatible versions.
        - Risk engine classifies a safe single-file modify as SAFE_AUTO.
        - Sandbox loop runs without modifying the production file.
        - Ledger records >= 2 entries.
        - Transport captures >= 2 messages.
        """
        # Step 1: SupervisorOuroborosController -> SANDBOX
        controller = SupervisorOuroborosController()
        await controller.start()
        assert controller.mode is AutonomyMode.SANDBOX
        assert controller.sandbox_allowed is True
        assert controller.interactive_allowed is True

        # Step 2: ContractGate -> autonomy_allowed=True (all 2.0.x)
        gate = ContractGate()
        boot_result = await gate.boot_check({
            "jarvis": ContractVersion(2, 0, 0),
            "prime": ContractVersion(2, 0, 1),
            "reactor": ContractVersion(2, 0, 2),
        })
        assert boot_result.autonomy_allowed is True

        # Step 3: RiskEngine -> SAFE_AUTO for a safe single-file modify
        risk_engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("backend/example.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.9,
        )
        classification = risk_engine.classify(profile)
        assert classification.tier is RiskTier.SAFE_AUTO
        assert classification.reason_code == "all_checks_passed"

        # Step 4: SandboxLoop with LogTransport and mocked candidate generation
        config = SandboxConfig(
            worktree_base=tmp_path / "worktrees",
            ledger_dir=tmp_path / "ledger",
        )
        comm = CommProtocol(transports=[transport])
        ledger = OperationLedger(storage_dir=config.ledger_dir)

        sandbox = SandboxLoop(
            project_root=tmp_project,
            config=config,
            comm=comm,
            risk_engine=risk_engine,
            ledger=ledger,
        )

        valid_candidate = {
            "code": "def hello():\n    return 'universe'  # improved\n",
            "description": "broaden scope of greeting",
        }

        # Step 5: Save original file content
        original_file = tmp_project / "backend" / "example.py"
        original_content = original_file.read_text()

        # Step 6: Run the sandbox loop
        with patch.object(
            sandbox,
            "_generate_candidates",
            new_callable=AsyncMock,
            return_value=[valid_candidate],
        ):
            result = await sandbox.run(
                goal="Improve hello function",
                target_file="backend/example.py",
            )

        # Step 7: Production file MUST be unchanged
        assert original_file.read_text() == original_content

        # Step 8: Ledger has >= 2 entries
        history = await ledger.get_history(result.op_id)
        assert len(history) >= 2
        states = [e.state for e in history]
        assert OperationState.PLANNED in states

        # Step 9: Transport has >= 2 messages
        assert len(transport.messages) >= 2
        msg_types = [m.msg_type for m in transport.messages]
        assert MessageType.INTENT in msg_types
        assert MessageType.DECISION in msg_types

        # Verify the loop succeeded
        assert result.success is True
        assert result.best_candidate is not None
        assert result.candidates_generated == 1

    # ---- 2. Supervisor-touching operations are BLOCKED -------------------

    @pytest.mark.asyncio
    async def test_supervisor_blocked_prevents_write(self) -> None:
        """An OperationProfile with touches_supervisor=True must be BLOCKED."""
        engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("unified_supervisor.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=1,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=True,
            test_scope_confidence=1.0,
        )
        classification = engine.classify(profile)
        assert classification.tier is RiskTier.BLOCKED
        assert classification.reason_code == "touches_supervisor"

    # ---- 3. Contract gate blocks autonomy on major mismatch --------------

    @pytest.mark.asyncio
    async def test_contract_gate_blocks_autonomy(self) -> None:
        """Major version mismatch (prime=3.x vs others=2.x) must block
        autonomy while still allowing interactive mode."""
        gate = ContractGate()
        boot_result = await gate.boot_check({
            "jarvis": ContractVersion(2, 0, 0),
            "prime": ContractVersion(3, 0, 0),
            "reactor": ContractVersion(2, 0, 0),
        })
        assert boot_result.autonomy_allowed is False
        assert boot_result.interactive_allowed is True
        assert len(boot_result.incompatible_pairs) > 0

    # ---- 4. Deterministic replay (1000x identical classification) --------

    @pytest.mark.asyncio
    async def test_deterministic_replay(self) -> None:
        """The same OperationProfile classified 1000x must produce
        identical tier + reason_code every single time."""
        engine = RiskEngine()
        profile = OperationProfile(
            files_affected=[Path("backend/utils.py")],
            change_type=ChangeType.MODIFY,
            blast_radius=2,
            crosses_repo_boundary=False,
            touches_security_surface=False,
            touches_supervisor=False,
            test_scope_confidence=0.85,
        )

        baseline = engine.classify(profile)

        for i in range(1000):
            result = engine.classify(profile)
            assert result.tier is baseline.tier, (
                f"Tier mismatch at iteration {i}: "
                f"{result.tier} != {baseline.tier}"
            )
            assert result.reason_code == baseline.reason_code, (
                f"Reason code mismatch at iteration {i}: "
                f"{result.reason_code!r} != {baseline.reason_code!r}"
            )
            assert result.policy_version == baseline.policy_version

    # ---- 5. Safe mode blocks writes but allows interactive ---------------

    @pytest.mark.asyncio
    async def test_safe_mode_blocks_writes(self) -> None:
        """Controller with _safe_mode=True must start in SAFE_MODE:
        writes_allowed=False, interactive_allowed=True."""
        controller = SupervisorOuroborosController()
        controller._safe_mode = True
        await controller.start()

        assert controller.mode is AutonomyMode.SAFE_MODE
        assert controller.writes_allowed is False
        assert controller.interactive_allowed is True

    # ---- 6. Emergency stop blocks resume ---------------------------------

    @pytest.mark.asyncio
    async def test_emergency_stop_blocks_resume(self) -> None:
        """After emergency_stop(), resume() must raise RuntimeError."""
        controller = SupervisorOuroborosController()
        await controller.start()
        assert controller.mode is AutonomyMode.SANDBOX

        await controller.emergency_stop("test emergency")
        assert controller.mode is AutonomyMode.EMERGENCY_STOP

        with pytest.raises(RuntimeError, match="Cannot resume from emergency stop"):
            await controller.resume()

        # Mode must still be EMERGENCY_STOP after the failed resume
        assert controller.mode is AutonomyMode.EMERGENCY_STOP
