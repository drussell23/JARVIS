"""tests/governance/self_dev/test_change_engine_opid.py

Verify ChangeEngine accepts and uses external op_id.
"""
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
)


@pytest.fixture
def engine(tmp_path: Path) -> ChangeEngine:
    target = tmp_path / "target.py"
    target.write_text("x = 1\n", encoding="utf-8")
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    return ChangeEngine(project_root=tmp_path, ledger=ledger)


def test_execute_uses_external_op_id(engine: ChangeEngine, tmp_path: Path):
    """When op_id is passed in ChangeRequest, ChangeEngine uses it."""
    target = tmp_path / "target.py"
    profile = OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=1.0,
    )
    request = ChangeRequest(
        goal="test change",
        target_file=target,
        proposed_content="x = 2\n",
        profile=profile,
        op_id="op-external-123",
    )
    result = asyncio.get_event_loop().run_until_complete(
        engine.execute(request)
    )
    assert result.op_id == "op-external-123"


def test_execute_generates_op_id_when_not_provided(
    engine: ChangeEngine, tmp_path: Path,
):
    """When no op_id in ChangeRequest, ChangeEngine generates one."""
    target = tmp_path / "target.py"
    profile = OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=1.0,
    )
    request = ChangeRequest(
        goal="test change",
        target_file=target,
        proposed_content="x = 2\n",
        profile=profile,
    )
    result = asyncio.get_event_loop().run_until_complete(
        engine.execute(request)
    )
    assert result.op_id.startswith("op-")
