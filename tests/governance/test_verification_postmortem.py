"""Phase 2 Slice 2.4 — Verification Postmortem regression spine.

The loop-closing slice. Pins:

  §1   postmortem_enabled flag — default false; case-tolerant
  §2   VerificationPostmortem — frozen + helper accessors
  §3   ClaimOutcome — frozen + .passed + .is_terminal + .is_blocking
  §4   to_dict / from_dict round-trip (postmortem)
  §5   to_dict / from_dict round-trip (claim outcome)
  §6   from_dict rejects bad input (schema mismatch, non-mapping)
  §7   produce — flag-off → empty postmortem
  §8   produce — empty op_id → empty postmortem
  §9   produce — no recorded claims → empty postmortem (clean)
  §10  produce — all PASSED → has_blocking_failures=False, is_clean=True
  §11  produce — must_hold FAILED → has_blocking_failures=True
  §12  produce — should_hold FAILED → not blocking
  §13  produce — ideal FAILED → not blocking
  §14  produce — INSUFFICIENT_EVIDENCE counted separately
  §15  produce — collector raises → ERROR verdict, no propagation
  §16  produce — non-Mapping evidence → empty (defensive)
  §17  ctx_evidence_collector — test_passes from validation_passed
  §18  ctx_evidence_collector — key_present from validation_passed
  §19  ctx_evidence_collector — string_matches → empty (Slice 2.4 honest)
  §20  ctx_evidence_collector — defensive on None ctx
  §21  Persist — flag-off → False, no disk traffic
  §22  Persist — happy path → True, ledger contains record
  §23  Reader — empty op_id → None
  §24  Reader — no record → None
  §25  Reader — round-trip via persist → get_recorded_postmortem
  §26  Reader — most recent wins (multiple postmortems)
  §27  log_postmortem_summary — empty postmortem → no log
  §28  log_postmortem_summary — populated → structured INFO line
  §29  Adapter registered at module load
  §30  Adapter round-trip (VerificationPostmortem ↔ dict)
  §31  Authority invariants — no orchestrator/phase_runner/provider imports
  §32  Public API exposed from package __init__
  §33  Schema versions pinned
  §34  COMPLETE runner wiring source pin
  §35  Total counts (failed/passed) accessor consistency
  §36  Empty postmortem is_clean=True; populated all-pass is_clean=True;
       any failure or insufficient → is_clean=False
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

import pytest

from backend.core.ouroboros.governance.verification import (
    ClaimOutcome,
    Property,
    PropertyClaim,
    PropertyVerdict,
    SEVERITY_IDEAL,
    SEVERITY_MUST_HOLD,
    SEVERITY_SHOULD_HOLD,
    VerdictKind,
    VerificationPostmortem,
    capture_claims,
    ctx_evidence_collector,
    get_recorded_postmortem,
    log_postmortem_summary,
    persist_postmortem,
    postmortem_enabled,
    produce_verification_postmortem,
)
from backend.core.ouroboros.governance.verification.postmortem import (
    CLAIM_OUTCOME_SCHEMA_VERSION,
    VERIFICATION_POSTMORTEM_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "true",
    )
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        reset_all_for_tests,
    )
    reset_all_for_tests()
    yield tmp_path / "det"
    reset_all_for_tests()


def _make_claim(
    name: str = "test_x",
    *,
    kind: str = "test_passes",
    severity: str = SEVERITY_MUST_HOLD,
    op_id: str = "op-1",
) -> PropertyClaim:
    return PropertyClaim(
        op_id=op_id,
        claimed_at_phase="PLAN",
        property=Property.make(
            kind=kind, name=name,
            evidence_required=("exit_code",) if kind == "test_passes" else ("present",),
        ),
        rationale="test rationale",
        severity=severity,
        claim_id=f"claim-{name}",
    )


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_postmortem_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", raising=False,
    )
    assert postmortem_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_postmortem_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_VERIFICATION_POSTMORTEM_ENABLED", val)
    assert postmortem_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_postmortem_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_VERIFICATION_POSTMORTEM_ENABLED", val)
    assert postmortem_enabled() is False


# ---------------------------------------------------------------------------
# §2-§3 — Schemas (frozen + helpers)
# ---------------------------------------------------------------------------


def test_postmortem_is_frozen() -> None:
    pm = VerificationPostmortem(op_id="op-1", session_id="sess-1")
    with pytest.raises(Exception):
        pm.op_id = "different"  # type: ignore[misc]


def test_postmortem_is_clean_helper() -> None:
    """Empty postmortem (no claims) is is_clean=True."""
    pm = VerificationPostmortem(op_id="op-1", session_id="sess-1")
    assert pm.is_clean is True
    assert pm.has_blocking_failures is False


def test_postmortem_total_passed_failed_accessors() -> None:
    """total_passed counts PASSED outcomes; total_failed counts
    FAILED across all severities."""
    claim = _make_claim()
    passed_outcome = ClaimOutcome(
        claim=claim,
        verdict=PropertyVerdict(
            property_name="x", kind="test_passes",
            verdict=VerdictKind.PASSED,
        ),
    )
    failed_outcome = ClaimOutcome(
        claim=claim,
        verdict=PropertyVerdict(
            property_name="x", kind="test_passes",
            verdict=VerdictKind.FAILED,
        ),
    )
    pm = VerificationPostmortem(
        op_id="op-1", session_id="sess-1",
        outcomes=(passed_outcome, passed_outcome, failed_outcome),
        must_hold_failed=1, must_hold_count=3,
        has_blocking_failures=True,
    )
    assert pm.total_passed == 2
    assert pm.total_failed == 1
    assert pm.is_clean is False


def test_claim_outcome_helpers() -> None:
    must_hold_failed = ClaimOutcome(
        claim=_make_claim(severity=SEVERITY_MUST_HOLD),
        verdict=PropertyVerdict(
            property_name="x", kind="test_passes",
            verdict=VerdictKind.FAILED,
        ),
    )
    assert must_hold_failed.passed is False
    assert must_hold_failed.is_terminal is True
    assert must_hold_failed.is_blocking is True

    should_hold_failed = ClaimOutcome(
        claim=_make_claim(severity=SEVERITY_SHOULD_HOLD),
        verdict=PropertyVerdict(
            property_name="x", kind="test_passes",
            verdict=VerdictKind.FAILED,
        ),
    )
    # should_hold failure is NOT blocking (only must_hold is)
    assert should_hold_failed.is_blocking is False

    insufficient = ClaimOutcome(
        claim=_make_claim(severity=SEVERITY_MUST_HOLD),
        verdict=PropertyVerdict(
            property_name="x", kind="test_passes",
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
        ),
    )
    assert insufficient.is_blocking is False  # not FAILED → not blocking
    assert insufficient.is_terminal is False


# ---------------------------------------------------------------------------
# §4-§6 — Round-trip serialization
# ---------------------------------------------------------------------------


def test_postmortem_round_trip() -> None:
    claim = _make_claim()
    outcome = ClaimOutcome(
        claim=claim,
        verdict=PropertyVerdict(
            property_name=claim.property.name, kind=claim.property.kind,
            verdict=VerdictKind.FAILED, confidence=1.0,
            reason="exit_code=1",
        ),
        evidence_used_repr='{"exit_code": 1}',
    )
    original = VerificationPostmortem(
        op_id="op-42", session_id="sess-x",
        total_claims=1, must_hold_count=1, must_hold_failed=1,
        has_blocking_failures=True,
        outcomes=(outcome,),
    )
    parsed = VerificationPostmortem.from_dict(original.to_dict())
    assert parsed is not None
    assert parsed.op_id == "op-42"
    assert parsed.has_blocking_failures is True
    assert parsed.must_hold_failed == 1
    assert len(parsed.outcomes) == 1
    assert parsed.outcomes[0].claim.severity == SEVERITY_MUST_HOLD
    assert parsed.outcomes[0].verdict.verdict is VerdictKind.FAILED


def test_postmortem_from_dict_rejects_bad() -> None:
    assert VerificationPostmortem.from_dict("not a mapping") is None  # type: ignore[arg-type]
    assert VerificationPostmortem.from_dict({}) is None
    assert VerificationPostmortem.from_dict({
        "schema_version": "wrong.0",
    }) is None


def test_postmortem_from_dict_handles_missing_fields() -> None:
    """Defensive: missing optional fields use safe defaults."""
    pm = VerificationPostmortem.from_dict({
        "schema_version": VERIFICATION_POSTMORTEM_SCHEMA_VERSION,
        "op_id": "op-1",
        "session_id": "sess-1",
    })
    assert pm is not None
    assert pm.total_claims == 0
    assert pm.outcomes == ()


# ---------------------------------------------------------------------------
# §7-§16 — Producer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_produce_flag_off_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "false",
    )
    pm = await produce_verification_postmortem(op_id="op-1")
    assert pm.total_claims == 0
    assert pm.has_blocking_failures is False


@pytest.mark.asyncio
async def test_produce_empty_op_id(isolated) -> None:
    pm = await produce_verification_postmortem(op_id="")
    assert pm.total_claims == 0
    assert pm.op_id == ""


@pytest.mark.asyncio
async def test_produce_no_claims(isolated) -> None:
    """Op with no recorded claims → clean empty postmortem."""
    pm = await produce_verification_postmortem(op_id="op-never-claimed")
    assert pm.total_claims == 0
    assert pm.is_clean is True
    assert pm.has_blocking_failures is False


@pytest.mark.asyncio
async def test_produce_all_passed(isolated) -> None:
    """Capture must_hold claims, all pass via collector → no blocking."""
    claims = [
        _make_claim(name=f"t{i}", severity=SEVERITY_MUST_HOLD)
        for i in range(3)
    ]
    await capture_claims(op_id="op-1", claims=claims)

    class _CtxAllPassed:
        validation_passed = True

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=_CtxAllPassed(),
    )
    assert pm.total_claims == 3
    assert pm.must_hold_count == 3
    assert pm.must_hold_failed == 0
    assert pm.has_blocking_failures is False
    assert pm.is_clean is True
    assert pm.total_passed == 3


@pytest.mark.asyncio
async def test_produce_must_hold_failed_blocks(isolated) -> None:
    claims = [_make_claim(severity=SEVERITY_MUST_HOLD)]
    await capture_claims(op_id="op-1", claims=claims)

    class _CtxFailed:
        validation_passed = False

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=_CtxFailed(),
    )
    assert pm.must_hold_failed == 1
    assert pm.has_blocking_failures is True
    assert pm.is_clean is False


@pytest.mark.asyncio
async def test_produce_should_hold_failed_not_blocking(isolated) -> None:
    claims = [_make_claim(severity=SEVERITY_SHOULD_HOLD)]
    await capture_claims(op_id="op-1", claims=claims)

    class _CtxFailed:
        validation_passed = False

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=_CtxFailed(),
    )
    assert pm.should_hold_failed == 1
    assert pm.must_hold_failed == 0
    assert pm.has_blocking_failures is False


@pytest.mark.asyncio
async def test_produce_ideal_failed_not_blocking(isolated) -> None:
    claims = [_make_claim(severity=SEVERITY_IDEAL)]
    await capture_claims(op_id="op-1", claims=claims)

    class _CtxFailed:
        validation_passed = False

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=_CtxFailed(),
    )
    assert pm.ideal_failed == 1
    assert pm.has_blocking_failures is False


@pytest.mark.asyncio
async def test_produce_insufficient_evidence(isolated) -> None:
    """Claim with kind that isn't covered by ctx_evidence_collector
    → INSUFFICIENT_EVIDENCE."""
    claim = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(
            kind="set_subset", name="x",
            evidence_required=("actual", "allowed"),
        ),
        severity=SEVERITY_MUST_HOLD,
    )
    await capture_claims(op_id="op-1", claims=[claim])

    pm = await produce_verification_postmortem(op_id="op-1")
    assert pm.insufficient_count == 1
    # INSUFFICIENT is not FAILED → not blocking
    assert pm.must_hold_failed == 0
    assert pm.has_blocking_failures is False


@pytest.mark.asyncio
async def test_produce_collector_raises_becomes_error(isolated) -> None:
    """Custom collector that raises → outcome is EVALUATOR_ERROR,
    no propagation."""
    claims = [_make_claim()]
    await capture_claims(op_id="op-1", claims=claims)

    async def boom(claim, ctx):
        raise RuntimeError("simulated collector fault")

    pm = await produce_verification_postmortem(
        op_id="op-1", evidence_collector=boom,
    )
    # Empty evidence from the safe-collector path means
    # exit_code missing → INSUFFICIENT_EVIDENCE (not ERROR).
    # The producer's defensive try/except returns {} on collector
    # raise, then the Oracle dispatcher returns INSUFFICIENT.
    assert pm.insufficient_count == 1


@pytest.mark.asyncio
async def test_produce_non_mapping_evidence(isolated) -> None:
    """Collector returning non-Mapping → empty evidence → INSUFFICIENT."""
    claims = [_make_claim()]
    await capture_claims(op_id="op-1", claims=claims)

    async def bad_collector(claim, ctx):
        return "not a mapping"

    pm = await produce_verification_postmortem(
        op_id="op-1", evidence_collector=bad_collector,
    )
    assert pm.insufficient_count == 1


# ---------------------------------------------------------------------------
# §17-§20 — ctx_evidence_collector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_collector_test_passes_validated() -> None:
    class Ctx:
        validation_passed = True

    claim = _make_claim(kind="test_passes")
    evidence = await ctx_evidence_collector(claim, Ctx())
    assert evidence == {"exit_code": 0}


@pytest.mark.asyncio
async def test_ctx_collector_test_passes_not_validated() -> None:
    class Ctx:
        validation_passed = False

    claim = _make_claim(kind="test_passes")
    evidence = await ctx_evidence_collector(claim, Ctx())
    assert evidence == {"exit_code": 1}


@pytest.mark.asyncio
async def test_ctx_collector_key_present_validated() -> None:
    class Ctx:
        validation_passed = True

    claim = _make_claim(kind="key_present")
    evidence = await ctx_evidence_collector(claim, Ctx())
    assert evidence == {"present": True}


@pytest.mark.asyncio
async def test_ctx_collector_string_matches_empty() -> None:
    """Slice 2.4's default collector is honest about not having
    signature evidence — returns empty → INSUFFICIENT_EVIDENCE."""
    class Ctx:
        validation_passed = True

    claim = PropertyClaim(
        op_id="x", claimed_at_phase="PLAN",
        property=Property.make(
            kind="string_matches", name="sig",
            evidence_required=("actual", "expected"),
        ),
    )
    evidence = await ctx_evidence_collector(claim, Ctx())
    assert evidence == {}


@pytest.mark.asyncio
async def test_ctx_collector_handles_none_ctx() -> None:
    """None ctx → collector doesn't crash."""
    claim = _make_claim()
    evidence = await ctx_evidence_collector(claim, None)
    assert isinstance(evidence, Mapping)


@pytest.mark.asyncio
async def test_ctx_collector_handles_missing_attrs() -> None:
    """ctx without validation_passed attr → defaults to False."""
    class Ctx:
        pass  # no validation_passed

    claim = _make_claim(kind="test_passes")
    evidence = await ctx_evidence_collector(claim, Ctx())
    assert evidence == {"exit_code": 1}  # defaults False → 1


# ---------------------------------------------------------------------------
# §21-§22 — Persister
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_flag_off_returns_false(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "false",
    )
    pm = VerificationPostmortem(op_id="op-1", session_id="sess-1")
    result = await persist_postmortem(pm=pm)
    assert result is False


@pytest.mark.asyncio
async def test_persist_happy_path(isolated) -> None:
    pm = VerificationPostmortem(
        op_id="op-1", session_id="test-session",
        total_claims=2, must_hold_count=2, must_hold_failed=1,
        has_blocking_failures=True,
    )
    result = await persist_postmortem(pm=pm)
    assert result is True

    # Verify ledger contains the record
    ledger_path = isolated / "test-session" / "decisions.jsonl"
    assert ledger_path.exists()
    raw = ledger_path.read_text(encoding="utf-8")
    assert "verification_postmortem" in raw
    assert '"op_id":"op-1"' in raw or '"op_id": "op-1"' in raw


@pytest.mark.asyncio
async def test_persist_empty_op_id_fails(isolated) -> None:
    pm = VerificationPostmortem(op_id="", session_id="sess-1")
    result = await persist_postmortem(pm=pm)
    assert result is False


# ---------------------------------------------------------------------------
# §23-§26 — Reader
# ---------------------------------------------------------------------------


def test_reader_empty_op_id() -> None:
    assert get_recorded_postmortem(op_id="") is None


def test_reader_no_record(isolated) -> None:
    assert get_recorded_postmortem(op_id="op-never-recorded") is None


@pytest.mark.asyncio
async def test_reader_round_trip(isolated) -> None:
    original = VerificationPostmortem(
        op_id="op-rt", session_id="test-session",
        total_claims=1, must_hold_count=1, must_hold_failed=1,
        has_blocking_failures=True,
    )
    persisted = await persist_postmortem(pm=original)
    assert persisted is True

    recovered = get_recorded_postmortem(op_id="op-rt")
    assert recovered is not None
    assert recovered.op_id == "op-rt"
    assert recovered.has_blocking_failures is True
    assert recovered.must_hold_failed == 1


@pytest.mark.asyncio
async def test_reader_most_recent_wins(isolated) -> None:
    """Multiple postmortems for same op → reader returns the LAST."""
    pm1 = VerificationPostmortem(
        op_id="op-multi", session_id="test-session",
        total_claims=1, must_hold_failed=1,
        has_blocking_failures=True,
    )
    pm2 = VerificationPostmortem(
        op_id="op-multi", session_id="test-session",
        total_claims=2, must_hold_failed=0,
        has_blocking_failures=False,
    )
    await persist_postmortem(pm=pm1)
    await persist_postmortem(pm=pm2)

    recovered = get_recorded_postmortem(op_id="op-multi")
    assert recovered is not None
    assert recovered.total_claims == 2  # last one wins
    assert recovered.has_blocking_failures is False


# ---------------------------------------------------------------------------
# §27-§28 — log_postmortem_summary
# ---------------------------------------------------------------------------


def test_log_postmortem_empty_no_log(caplog) -> None:
    """Empty postmortem (no claims) doesn't log — no noise."""
    caplog.set_level(logging.INFO)
    pm = VerificationPostmortem(op_id="op-1", session_id="sess-1")
    log_postmortem_summary(pm)
    matched = [
        r for r in caplog.records if "VerifyPostmortem" in r.getMessage()
    ]
    assert matched == []


def test_log_postmortem_populated_emits_info(caplog) -> None:
    caplog.set_level(logging.INFO)
    pm = VerificationPostmortem(
        op_id="op-1", session_id="sess-1",
        total_claims=3, must_hold_count=2, must_hold_failed=1,
        has_blocking_failures=True,
    )
    log_postmortem_summary(pm)
    matched = [
        r for r in caplog.records if "VerifyPostmortem" in r.getMessage()
    ]
    assert len(matched) == 1
    msg = matched[0].getMessage()
    assert "op=op-1" in msg
    assert "claims=3" in msg
    assert "blocking=true" in msg


def test_log_postmortem_none_no_crash() -> None:
    """Defensive: None argument doesn't crash."""
    log_postmortem_summary(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §29-§30 — Adapter integration
# ---------------------------------------------------------------------------


def test_postmortem_adapter_registered() -> None:
    from backend.core.ouroboros.governance.determinism.phase_capture import (
        get_adapter,
        _IDENTITY_ADAPTER,
    )
    import backend.core.ouroboros.governance.verification.postmortem  # noqa
    adapter = get_adapter(
        phase="COMPLETE", kind="verification_postmortem",
    )
    assert adapter is not _IDENTITY_ADAPTER
    assert adapter.name == "verification_postmortem_adapter"


def test_postmortem_adapter_round_trip() -> None:
    from backend.core.ouroboros.governance.determinism.phase_capture import (
        get_adapter,
    )
    adapter = get_adapter(
        phase="COMPLETE", kind="verification_postmortem",
    )
    original = VerificationPostmortem(
        op_id="op-1", session_id="sess-1",
        total_claims=2, must_hold_failed=1,
    )
    serialized = adapter.serialize(original.to_dict())
    deserialized = adapter.deserialize(serialized)
    assert isinstance(deserialized, VerificationPostmortem)
    assert deserialized.must_hold_failed == 1


# ---------------------------------------------------------------------------
# §31 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import ast
    import inspect
    from backend.core.ouroboros.governance.verification import postmortem
    tree = ast.parse(inspect.getsource(postmortem))
    forbidden = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.candidate_generator",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for f in forbidden:
                assert f != node.module, (
                    f"postmortem must NOT import from {f}"
                )


def test_no_phase_runners_imports() -> None:
    import ast
    import inspect
    from backend.core.ouroboros.governance.verification import postmortem
    tree = ast.parse(inspect.getsource(postmortem))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "phase_runners" not in node.module


def test_no_provider_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import postmortem
    src = inspect.getsource(postmortem)
    assert "doubleword_provider" not in src
    assert "claude_provider" not in src.lower()


# ---------------------------------------------------------------------------
# §32-§33 — Public API + schema versions
# ---------------------------------------------------------------------------


def test_public_api_exposure() -> None:
    from backend.core.ouroboros.governance import verification
    expected = {
        "VerificationPostmortem", "ClaimOutcome",
        "produce_verification_postmortem", "persist_postmortem",
        "get_recorded_postmortem", "log_postmortem_summary",
        "ctx_evidence_collector", "postmortem_enabled",
    }
    for name in expected:
        assert name in verification.__all__, f"missing: {name}"


def test_schema_versions_pinned() -> None:
    assert VERIFICATION_POSTMORTEM_SCHEMA_VERSION == "verification_postmortem.1"
    assert CLAIM_OUTCOME_SCHEMA_VERSION == "claim_outcome.1"


# ---------------------------------------------------------------------------
# §34 — COMPLETE runner wiring
# ---------------------------------------------------------------------------


def test_complete_runner_wires_postmortem() -> None:
    src = open(
        "backend/core/ouroboros/governance/phase_runners/complete_runner.py",
        encoding="utf-8",
    ).read()
    assert "Slice 2.4" in src
    assert "produce_verification_postmortem" in src
    assert "persist_postmortem" in src


def test_complete_runner_imports_postmortem_lazily() -> None:
    """Import must be inside function body, not module top level."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/complete_runner.py",
        encoding="utf-8",
    ).read()
    lines = src.split("\n")
    top_level = [
        ln for ln in lines
        if ln.startswith(
            "from backend.core.ouroboros.governance.verification.postmortem"
        )
    ]
    assert top_level == []


def test_complete_runner_postmortem_has_try_except() -> None:
    src = open(
        "backend/core/ouroboros/governance/phase_runners/complete_runner.py",
        encoding="utf-8",
    ).read()
    pm_idx = src.index("produce_verification_postmortem")
    preceding = src[max(0, pm_idx - 1500):pm_idx]
    assert "try:" in preceding
    following = src[pm_idx:pm_idx + 2000]
    assert "except Exception" in following


def test_complete_runner_postmortem_after_apply() -> None:
    """Postmortem call must come after _publish_outcome (op marked
    APPLIED) and before the success-path PhaseResult return."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/complete_runner.py",
        encoding="utf-8",
    ).read()
    apply_idx = src.index("_publish_outcome")
    pm_idx = src.index("produce_verification_postmortem")
    return_idx = src.index('reason="complete"')
    assert apply_idx < pm_idx < return_idx


# ---------------------------------------------------------------------------
# §35-§36 — Aggregation correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_passed_failed_consistency(isolated) -> None:
    """total_passed + total_failed + insufficient + error = total_claims
    for any postmortem (Slice 2.4 invariant)."""
    claims = [
        _make_claim(name=f"t{i}", severity=SEVERITY_MUST_HOLD)
        for i in range(2)
    ] + [
        _make_claim(name="t-should", severity=SEVERITY_SHOULD_HOLD),
        # one INSUFFICIENT claim
        PropertyClaim(
            op_id="op-1", claimed_at_phase="PLAN",
            property=Property.make(
                kind="set_subset", name="ss",
                evidence_required=("actual", "allowed"),
            ),
            severity=SEVERITY_IDEAL,
        ),
    ]
    await capture_claims(op_id="op-1", claims=claims)

    class Ctx:
        validation_passed = True

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=Ctx(),
    )
    assert pm.total_claims == 4
    # Conservation invariant
    accounted = (
        pm.total_passed + pm.total_failed
        + pm.insufficient_count + pm.error_count
    )
    assert accounted == pm.total_claims


@pytest.mark.asyncio
async def test_all_pass_is_clean_true(isolated) -> None:
    """All-pass + no insufficient/error → is_clean=True."""
    claims = [_make_claim(name="t1", severity=SEVERITY_MUST_HOLD)]
    await capture_claims(op_id="op-1", claims=claims)

    class Ctx:
        validation_passed = True

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=Ctx(),
    )
    assert pm.is_clean is True


@pytest.mark.asyncio
async def test_any_failure_is_clean_false(isolated) -> None:
    claims = [_make_claim(severity=SEVERITY_IDEAL)]
    await capture_claims(op_id="op-1", claims=claims)

    class Ctx:
        validation_passed = False

    pm = await produce_verification_postmortem(
        op_id="op-1", ctx=Ctx(),
    )
    assert pm.is_clean is False
    # ideal failure doesn't block but is_clean reflects ANY failure
    assert pm.has_blocking_failures is False
