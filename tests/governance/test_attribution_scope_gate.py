"""Slice 6 Task 5 — an op whose evidence carries attribution
status=unresolved MUST NOT auto-apply a candidate that mutates only
test loci: risk escalates to APPROVAL_REQUIRED (mirrors SemanticGuardian
hard-finding escalation at the same site).

The orchestrator wires the pure, fully-unit-tested Task 2 predicate
``unattributed_test_scope_violation`` into the post-VALIDATE risk
decision via the module-level helper ``_attribution_scope_risk_floor``.
This suite drives that helper directly (the exact code the guardian site
runs) and pins it to the guardian invocation site, so it proves BOTH
the escalation mechanics AND that the gate is actually wired (not the
"wired-but-inert" trap). The predicate's own semantics are exhaustively
covered in Task 2's ``test_source_attribution`` suite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.orchestrator import (
    _attribution_scope_risk_floor,
)
from backend.core.ouroboros.governance.phase_runners.gate_runner import GATERunner
from backend.core.ouroboros.governance.risk_engine import RiskTier

# Established parity-test fakes for driving the REAL GATERunner.run()
# (the same harness test_gate_noncandidate_regate.py uses).
from tests.governance.phase_runner import test_gate_runner_parity as _gp


UNRESOLVED_EVIDENCE = json.dumps({
    "attribution": {
        "schema_version": 1,
        "status": "unresolved",
        "test_locus": "tests/test_engine.py",
        "source_loci": [],
        "method": "",
        "reason": "no_first_party_source_imports",
    }
})

RESOLVED_EVIDENCE = json.dumps({
    "attribution": {
        "schema_version": 1,
        "status": "resolved",
        "test_locus": "tests/test_engine.py",
        "source_loci": ["backend/engine.py"],
        "method": "direct_import",
        "reason": "",
    }
})


class _Ctx:
    """Minimal stand-in for OperationContext — the helper only reads
    ``intake_evidence_json`` (op_context.py:1024) and ``op_id``."""

    def __init__(self, evidence_json: str, op_id: str = "op-test-1") -> None:
        self.intake_evidence_json = evidence_json
        self.op_id = op_id


# ---------------------------------------------------------------------------
# Contract 1 — unresolved + test-only candidate escalates to APPROVAL_REQUIRED
# ---------------------------------------------------------------------------


def test_unresolved_test_only_candidate_escalates_to_approval(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.SAFE_AUTO,
    )

    assert tier is RiskTier.APPROVAL_REQUIRED, (
        f"unresolved+test-only must escalate to APPROVAL_REQUIRED, got {tier}"
    )
    assert violation is not None
    assert "attribution_unresolved_test_scope" in violation, (
        f"violation reason must be visible in the message, got: {violation!r}"
    )


def test_notify_apply_also_escalates_to_approval(monkeypatch) -> None:
    """A NOTIFY_APPLY op (stricter than SAFE_AUTO but weaker than
    APPROVAL_REQUIRED) is still escalated — the floor is APPROVAL_REQUIRED."""
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.NOTIFY_APPLY,
    )

    assert tier is RiskTier.APPROVAL_REQUIRED
    assert violation is not None


# ---------------------------------------------------------------------------
# Contract 1b (I2) — ABSOLUTE test-infra candidate escalates once repo_root
# is threaded through (without it the absolute path defeated classification)
# ---------------------------------------------------------------------------


def test_absolute_test_infra_candidate_escalates_with_repo_root(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    ctx = _Ctx(UNRESOLVED_EVIDENCE)
    abs_conftest = "/Users/x/repo/tests/conftest.py"

    # Without repo_root the absolute path slips the gate (the I2 bug).
    tier_no_root, violation_no_root = _attribution_scope_risk_floor(
        ctx, [abs_conftest], RiskTier.SAFE_AUTO,
    )
    assert tier_no_root is RiskTier.SAFE_AUTO
    assert violation_no_root is None

    # With repo_root the candidate normalizes to tests/conftest.py and fires.
    tier, violation = _attribution_scope_risk_floor(
        ctx, [abs_conftest], RiskTier.SAFE_AUTO, repo_root="/Users/x/repo",
    )
    assert tier is RiskTier.APPROVAL_REQUIRED
    assert violation is not None


def test_absolute_source_candidate_no_false_positive_with_repo_root(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    ctx = _Ctx(UNRESOLVED_EVIDENCE)
    tier, violation = _attribution_scope_risk_floor(
        ctx, ["/Users/x/repo/backend/engine.py"], RiskTier.SAFE_AUTO,
        repo_root="/Users/x/repo",
    )
    assert tier is RiskTier.SAFE_AUTO
    assert violation is None


# ---------------------------------------------------------------------------
# Contract 2 — resolved attribution never escalates
# ---------------------------------------------------------------------------


def test_resolved_attribution_never_escalates(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(RESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.SAFE_AUTO,
    )

    assert tier is RiskTier.SAFE_AUTO, "resolved attribution must not escalate"
    assert violation is None


def test_source_mutating_candidate_never_escalates(monkeypatch) -> None:
    """Even with unresolved attribution, a candidate that touches a
    real source locus is NOT the Run-16 blind class → no escalation."""
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py", "backend/engine.py"], RiskTier.SAFE_AUTO,
    )

    assert tier is RiskTier.SAFE_AUTO
    assert violation is None


# ---------------------------------------------------------------------------
# Contract 3 — master switch off → unchanged
# ---------------------------------------------------------------------------


def test_gate_master_switch_off(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", "false")
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.SAFE_AUTO,
    )

    assert tier is RiskTier.SAFE_AUTO, "gate must be inert when master switch off"
    assert violation is None


# ---------------------------------------------------------------------------
# Composition — escalation is stricter-wins, never a downgrade
# ---------------------------------------------------------------------------


def test_escalation_never_downgrades_blocked(monkeypatch) -> None:
    """If the op is already BLOCKED (stricter than APPROVAL_REQUIRED),
    the gate must NOT downgrade it — the floor only escalates upward."""
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.BLOCKED,
    )

    assert tier is RiskTier.BLOCKED, "must never downgrade a stricter tier"
    # The violation is still surfaced (visibility) even though no tier change.
    assert violation is not None


def test_already_approval_required_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    ctx = _Ctx(UNRESOLVED_EVIDENCE)

    tier, violation = _attribution_scope_risk_floor(
        ctx, ["tests/test_engine.py"], RiskTier.APPROVAL_REQUIRED,
    )

    assert tier is RiskTier.APPROVAL_REQUIRED
    assert violation is not None


# ---------------------------------------------------------------------------
# Fail-soft — malformed / absent evidence never raises, never escalates
# ---------------------------------------------------------------------------


def test_malformed_evidence_is_fail_soft(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    for bad in ("", "not json", "{}", None):
        ctx = _Ctx(bad)  # type: ignore[arg-type]
        tier, violation = _attribution_scope_risk_floor(
            ctx, ["tests/test_engine.py"], RiskTier.SAFE_AUTO,
        )
        assert tier is RiskTier.SAFE_AUTO
        assert violation is None


def test_missing_intake_evidence_attr_is_fail_soft(monkeypatch) -> None:
    """A ctx that predates the field (no attribute at all) must not raise."""
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)

    class _Bare:
        op_id = "op-bare"

    tier, violation = _attribution_scope_risk_floor(
        _Bare(), ["tests/test_engine.py"], RiskTier.SAFE_AUTO,
    )
    assert tier is RiskTier.SAFE_AUTO
    assert violation is None


# ---------------------------------------------------------------------------
# Wiring pin — the helper is actually invoked at the guardian site (defeats
# the "wired-but-inert" trap: a gate with zero callers is theater).
# ---------------------------------------------------------------------------


def test_gate_is_wired_at_guardian_site() -> None:
    src = Path(
        "backend/core/ouroboros/governance/orchestrator.py"
    ).read_text(encoding="utf-8")

    # The helper is defined AND called.
    assert "def _attribution_scope_risk_floor(" in src
    assert "_attribution_scope_risk_floor(\n" in src or (
        "_attribution_scope_risk_floor(" in src.split(
            "def _attribution_scope_risk_floor(", 1,
        )[1]
    ), "helper must be invoked, not just defined"

    # The call sits within the SemanticGuardian block (after findings are
    # collected via inspect_batch, before the guardian telemetry log).
    guard_idx = src.index("_guardian_findings = _guardian.inspect_batch")
    call_idx = src.index(
        "risk_tier, _attr_violation = _attribution_scope_risk_floor",
    )
    log_idx = src.index('"[SemanticGuard] op=%s findings=%d')
    assert guard_idx < call_idx < log_idx, (
        "attribution gate must run after guardian findings are collected "
        "and be captured in the [SemanticGuard] risk_after telemetry"
    )

    # The mandated operator-visible log line is present.
    assert "[Attribution] gate: %s op=%s" in src


def test_gate_is_wired_in_extracted_gate_runner() -> None:
    """JARVIS_PHASE_RUNNER_GATE_EXTRACTED is graduated default-TRUE, so
    gate_runner.py is the SHIPPING GATE path — the gate must be wired
    there too (a gate only on the flag-off inline path is theater)."""
    src = Path(
        "backend/core/ouroboros/governance/phase_runners/gate_runner.py"
    ).read_text(encoding="utf-8")

    guard_idx = src.index("_guardian_findings = _guardian.inspect_batch(_pairs)")
    call_idx = src.index(
        "risk_tier, _attr_violation = _attribution_scope_risk_floor",
    )
    log_idx = src.index('"[SemanticGuard] op=%s findings=%d')
    assert guard_idx < call_idx < log_idx, (
        "extracted-path attribution gate must run after guardian findings "
        "are collected and be captured in [SemanticGuard] risk_after"
    )
    assert "[Attribution] gate: %s op=%s" in src


# ---------------------------------------------------------------------------
# Behavioral — the DEFAULT (extracted GATERunner) path. Drives the real
# GATERunner.run() end-to-end via the established parity fakes and proves
# unresolved + test-only escalates to APPROVAL_REQUIRED on the shipping path.
# ---------------------------------------------------------------------------


_TEST_ONLY_CANDIDATE = {
    "candidate_id": "c0",
    "file_path": "tests/test_engine.py",
    "full_content": "def test_x():\n    assert True\n",
}


@pytest.mark.asyncio
async def test_extracted_path_unresolved_test_only_escalates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_TEST_DIR_NAMES", raising=False)
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    object.__setattr__(ctx, "intake_evidence_json", UNRESOLVED_EVIDENCE)
    orch = _gp._orch(tmp_path)

    result = await GATERunner(
        orch, None, dict(_TEST_ONLY_CANDIDATE), RiskTier.SAFE_AUTO,
    ).run(ctx)

    assert result.status == "ok", f"Expected ok from GATE, got: {result!r}"
    risk_after = result.artifacts["risk_tier"]
    assert risk_after is RiskTier.APPROVAL_REQUIRED, (
        "the DEFAULT (extracted GATERunner) path must escalate an "
        f"unresolved+test-only op to APPROVAL_REQUIRED, got {risk_after!r}"
    )


@pytest.mark.asyncio
async def test_extracted_path_resolved_attribution_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_TEST_DIR_NAMES", raising=False)
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    object.__setattr__(ctx, "intake_evidence_json", RESOLVED_EVIDENCE)
    orch = _gp._orch(tmp_path)

    result = await GATERunner(
        orch, None, dict(_TEST_ONLY_CANDIDATE), RiskTier.SAFE_AUTO,
    ).run(ctx)

    assert result.status == "ok"
    risk_after = result.artifacts["risk_tier"]
    assert risk_after is RiskTier.SAFE_AUTO, (
        f"resolved attribution must not escalate on the extracted path, "
        f"got {risk_after!r}"
    )


@pytest.mark.asyncio
async def test_extracted_path_master_switch_off_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", "false")
    monkeypatch.delenv("JARVIS_TEST_DIR_NAMES", raising=False)
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    object.__setattr__(ctx, "intake_evidence_json", UNRESOLVED_EVIDENCE)
    orch = _gp._orch(tmp_path)

    result = await GATERunner(
        orch, None, dict(_TEST_ONLY_CANDIDATE), RiskTier.SAFE_AUTO,
    ).run(ctx)

    assert result.status == "ok"
    risk_after = result.artifacts["risk_tier"]
    assert risk_after is RiskTier.SAFE_AUTO, (
        f"gate must be inert on the extracted path when the master switch "
        f"is off, got {risk_after!r}"
    )
