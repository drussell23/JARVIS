"""tests/governance/self_dev/test_error_handling.py

Tests for error handling and failure-matrix scenarios:
  1. Rollback restores original content
  2. Rollback recreates a deleted file
  3. Approval timeout produces EXPIRED state
  4. TestRunner subprocess timeout yields passed=False
  5. Failing transport does not block LogTransport delivery
  6. BLOCKED profile does not apply file changes
  7. Concurrent op rejected with reason_code="busy"
  8. Empty provider response yields zero candidates
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.approval_store import (
    ApprovalState,
    ApprovalStore,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    ServiceState,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskTier,
)
from backend.core.ouroboros.governance.test_runner import TestRunner


# ── 1. Rollback restores original content ──────────────────────────

def test_rollback_restores_original_content(tmp_path: Path):
    """RollbackArtifact.capture → modify file → artifact.apply → original restored."""
    target = tmp_path / "example.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")

    artifact = RollbackArtifact.capture(target)

    # Mutate the file
    target.write_text("x = 999\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") != original

    # Rollback
    artifact.apply(target)
    assert target.read_text(encoding="utf-8") == original


# ── 2. Rollback recreates a deleted file ───────────────────────────

def test_rollback_recreates_deleted_file(tmp_path: Path):
    """capture → delete file → apply → file exists with original content."""
    target = tmp_path / "to_delete.py"
    original = "print('hello')\n"
    target.write_text(original, encoding="utf-8")

    artifact = RollbackArtifact.capture(target)

    # Delete the file
    target.unlink()
    assert not target.exists()

    # Rollback recreates it
    artifact.apply(target)
    assert target.exists()
    assert target.read_text(encoding="utf-8") == original


# ── 3. Approval timeout produces EXPIRED ───────────────────────────

def test_approval_timeout_produces_expired(tmp_path: Path):
    """Create PENDING, backdate, expire_stale → expired."""
    store = ApprovalStore(store_path=tmp_path / "approvals" / "pending.json")
    store.create("op-timeout", policy_version="v0.1.0")

    # Backdate the created_at so the record looks stale
    data = json.loads(store._path.read_text(encoding="utf-8"))
    data["op-timeout"]["created_at"] = time.time() - 7200  # 2 hours ago
    store._atomic_write(data)

    expired = store.expire_stale(timeout_seconds=1800.0)
    assert "op-timeout" in expired

    record = store.get("op-timeout")
    assert record is not None
    assert record.state == ApprovalState.EXPIRED


# ── 4. TestRunner subprocess timeout → passed=False ────────────────

def test_test_runner_subprocess_timeout(tmp_path: Path):
    """A slow test with a 2s timeout yields passed=False."""
    # Create a test file that sleeps longer than the timeout
    slow_test = tmp_path / "test_slow.py"
    slow_test.write_text(
        "import time\n\ndef test_sleeps():\n    time.sleep(30)\n",
        encoding="utf-8",
    )

    runner = TestRunner(repo_root=tmp_path, timeout=2.0)
    result = asyncio.get_event_loop().run_until_complete(
        runner.run(test_files=(slow_test,))
    )

    assert result.passed is False


# ── 5. Failing transport continues to LogTransport ─────────────────

def test_notification_channel_failure_continues():
    """Failing transport + LogTransport → LogTransport receives message."""
    good = LogTransport()
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("transport down"))
    comm = CommProtocol(transports=[bad, good])

    asyncio.get_event_loop().run_until_complete(
        comm.emit_intent(
            op_id="op-fail-transport",
            goal="test fault isolation",
            target_files=["a.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
    )

    # The healthy LogTransport should still receive the message
    assert len(good.messages) == 1
    assert good.messages[0].payload["goal"] == "test fault isolation"


# ── 6. BLOCKED profile does not apply file changes ─────────────────

def test_blocked_does_not_apply(tmp_path: Path):
    """OperationProfile with touches_supervisor + touches_security_surface → BLOCKED, file unchanged."""
    target = tmp_path / "supervisor.py"
    original = "# supervisor code\n"
    target.write_text(original, encoding="utf-8")

    profile = OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=True,
        touches_supervisor=True,
        test_scope_confidence=0.9,
    )

    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    engine = ChangeEngine(project_root=tmp_path, ledger=ledger)

    request = ChangeRequest(
        goal="modify supervisor",
        target_file=target,
        proposed_content="# malicious change\n",
        profile=profile,
    )

    result = asyncio.get_event_loop().run_until_complete(engine.execute(request))

    assert result.success is False
    assert result.risk_tier == RiskTier.BLOCKED
    # The file should remain untouched
    assert target.read_text(encoding="utf-8") == original


# ── 7. Concurrent op rejected with reason_code="busy" ─────────────

def test_concurrent_op_rejected():
    """GovernedLoopService with max_concurrent_ops=1 and one active op → submit returns reason_code='busy'."""
    config = GovernedLoopConfig(
        project_root=Path("/tmp/test"),
        max_concurrent_ops=1,
    )

    # Build minimal mock stack and prime_client
    stack = MagicMock()
    prime_client = MagicMock()

    svc = GovernedLoopService(
        stack=stack,
        prime_client=prime_client,
        config=config,
    )

    # Force the service into ACTIVE state and fill _active_ops
    svc._state = ServiceState.ACTIVE
    svc._active_ops.add("op-in-flight")

    ctx = OperationContext.create(
        target_files=("test.py",),
        description="test concurrency gate",
        op_id="op-new",
    )

    result = asyncio.get_event_loop().run_until_complete(
        svc.submit(ctx, trigger_source="test")
    )

    assert result.reason_code == "busy"
    assert result.terminal_phase == OperationPhase.CANCELLED


# ── 8. Empty provider response → zero candidates ──────────────────

def test_empty_provider_response():
    """GenerationResult with empty candidates tuple has length 0."""
    gen_result = GenerationResult(
        candidates=(),
        provider_name="test-provider",
        generation_duration_s=0.5,
    )
    assert len(gen_result.candidates) == 0
