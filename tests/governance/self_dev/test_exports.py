"""tests/governance/self_dev/test_exports.py"""


def test_vertical_integration_public_api():
    from backend.core.ouroboros.governance import (
        GovernedLoopService,
        GovernedLoopConfig,
        OperationResult,
        ReadyToCommitPayload,
        handle_self_modify,
        handle_approve,
        handle_reject,
        handle_status,
    )
    from backend.core.ouroboros.governance.test_runner import (
        TestRunner, TestResult,
    )
    from backend.core.ouroboros.governance.approval_store import (
        ApprovalStore, ApprovalState, ApprovalRecord,
    )
    assert ReadyToCommitPayload is not None
    assert handle_status is not None
    assert TestRunner is not None
    assert ApprovalStore is not None
