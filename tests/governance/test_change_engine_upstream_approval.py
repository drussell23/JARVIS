"""One decision, not two gates.

The GATE phase obtains an approval (a human, or the headless synthetic
approval of a soak) and advances to APPLY; the change engine re-classifies
the same governance file as APPROVAL_REQUIRED and — knowing nothing of the
decision — escalated a second time with no one left to ask. Every sanctioned
production-file goal died as ``change_engine_failed`` (2026-09-08). The
decision now rides the context (``with_approval``) into the ChangeRequest,
and the engine honours it for THIS op only. BLOCKED is never relaxed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.approval_provider import ApprovalResult, ApprovalStatus
from backend.core.ouroboros.governance.change_engine import ChangeEngine, ChangePhase, ChangeRequest
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.risk_engine import ChangeType, OperationProfile, RiskTier


def _engine(tmp_path: Path) -> ChangeEngine:
    return ChangeEngine(project_root=tmp_path, ledger=OperationLedger(storage_dir=tmp_path / "ledger"))


def _target(tmp_path: Path) -> Path:
    """An ordinary production file. (A governance path with no sanctioned
    source is BLOCKED by self-protection, which the decision never relaxes;
    the APPROVAL_REQUIRED tier here comes from the blast-radius rule.)"""
    target = tmp_path / "pkg" / "probe.py"
    target.parent.mkdir(parents=True)
    target.write_text("def tracer_enabled():\n    return True\n", encoding="utf-8")
    return target


def _profile(target: Path) -> OperationProfile:
    return OperationProfile(
        files_affected=[target], change_type=ChangeType.MODIFY, blast_radius=10**6,
        crosses_repo_boundary=False, touches_security_surface=False,
        touches_supervisor=False, test_scope_confidence=1.0,
    )


def _approved(op_id: str, approver: str = "headless: no-tty:stdin") -> ApprovalResult:
    return ApprovalResult(
        status=ApprovalStatus.APPROVED, approver=approver, reason=None,
        decided_at=datetime.now(tz=timezone.utc), request_id=op_id,
    )


NEW = "def tracer_enabled():\n    return False\n"


@pytest.mark.asyncio
async def test_an_approval_required_change_still_escalates_without_a_decision(tmp_path):
    target = _target(tmp_path)
    result = await _engine(tmp_path).execute(ChangeRequest(
        goal="g", target_file=target, proposed_content=NEW, profile=_profile(target), op_id="op-a",
    ))
    assert result.success is False
    assert result.phase_reached is ChangePhase.GATE and result.risk_tier is RiskTier.APPROVAL_REQUIRED
    assert target.read_text(encoding="utf-8") != NEW


@pytest.mark.asyncio
async def test_the_gates_decision_is_honoured_for_this_op(tmp_path):
    target = _target(tmp_path)
    ctx = OperationContext.create(description="g", target_files=(str(target),)).with_approval(_approved("op-b"))
    assert ctx.approval.status == "approved" and ctx.approval.request_id == "op-b"
    result = await _engine(tmp_path).execute(ChangeRequest(
        goal="g", target_file=target, proposed_content=NEW, profile=_profile(target),
        op_id="op-b", approval=ctx.approval,
    ))
    assert result.success is True, (result.phase_reached, result.error)
    assert target.read_text(encoding="utf-8").endswith(NEW), "the engine wrote the approved body (after its provenance header)"


@pytest.mark.asyncio
async def test_a_decision_for_another_op_confers_nothing(tmp_path):
    target = _target(tmp_path)
    stamp = OperationContext.create(description="g", target_files=(str(target),)).with_approval(_approved("op-other")).approval
    result = await _engine(tmp_path).execute(ChangeRequest(
        goal="g", target_file=target, proposed_content=NEW, profile=_profile(target),
        op_id="op-c", approval=stamp,
    ))
    assert result.success is False and result.phase_reached is ChangePhase.GATE


@pytest.mark.asyncio
async def test_a_rejection_or_pending_stamp_confers_nothing(tmp_path):
    target = _target(tmp_path)
    for status in (ApprovalStatus.REJECTED, ApprovalStatus.PENDING, ApprovalStatus.EXPIRED):
        dec = ApprovalResult(status=status, approver="x", reason=None, decided_at=None, request_id="op-d")
        stamp = OperationContext.create(description="g", target_files=(str(target),)).with_approval(dec).approval
        result = await _engine(tmp_path).execute(ChangeRequest(
            goal="g", target_file=target, proposed_content=NEW, profile=_profile(target),
            op_id="op-d", approval=stamp,
        ))
        assert result.success is False, status


def test_both_runners_carry_the_decision_and_name_a_refusal():
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    for mod in (orchestrator, slice4b_runner):
        src = inspect.getsource(mod)
        assert "ctx = ctx.with_approval(decision)" in src, mod.__name__
        assert "change engine refused op=" in src, mod.__name__
    assert "approval=getattr(ctx, \"approval\", None)" in inspect.getsource(orchestrator._build_change_request) \
        if hasattr(orchestrator, "_build_change_request") else True


@pytest.mark.asyncio
async def test_the_applied_row_records_what_apply_wrote(tmp_path):
    """Boot reconcile tells an intact apply from an interrupted one by this
    hash — a landed op was marked "needs manual rollback" without it."""
    import hashlib
    from backend.core.ouroboros.governance.ledger import OperationLedger
    target = _target(tmp_path)
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    engine = ChangeEngine(project_root=tmp_path, ledger=ledger)
    ctx = OperationContext.create(description="g", target_files=(str(target),)).with_approval(_approved("op-h"))
    result = await engine.execute(ChangeRequest(
        goal="g", target_file=target, proposed_content=NEW, profile=_profile(target),
        op_id="op-h", approval=ctx.approval,
    ))
    assert result.success is True
    rows = await ledger.get_history("op-h")
    applied = [r for r in rows if getattr(r.state, "value", r.state) == "applied"]
    assert applied, [getattr(r.state, "value", r.state) for r in rows]
    on_disk = hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert applied[-1].data.get("applied_hash") == on_disk
    assert applied[-1].data.get("rollback_hash") and applied[-1].data["rollback_hash"] != on_disk


def test_boot_reconcile_recognises_an_intact_apply():
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as GLS
    src = inspect.getsource(GLS.GovernedLoopService._reconcile_on_boot)
    assert "boot_recovery_apply_intact" in src
    assert src.index("boot_recovery_apply_intact") < src.index("boot_recovery_needs_manual_rollback")
