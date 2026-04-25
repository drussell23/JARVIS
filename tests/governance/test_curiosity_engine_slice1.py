"""Wave 2 (4) Slice 1 — CuriosityBudget primitive tests.

Pins the contract per ``project_w2_4_curiosity_scope.md`` Slice 1:

A. Env knob defaults — master default `false`; sub-flags force-disabled
   under master-off composition (single hot-revert env per operator binding).
B. Charge composition — master / invalid / posture / quota / cost cap, in order.
C. Counter increments only on Allowed.
D. ContextVar default-None + propagates through asyncio.create_task.
E. Ledger schema curiosity.1 round-trip + persistence.
F. snapshot() shape for postmortem.

All tests offline. NO Rule 14 widening, NO SSE, NO graduation pins —
those are Slices 2/3/4.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.curiosity_engine import (
    ChargeResult,
    CuriosityBudget,
    CuriosityRecord,
    DenyReason,
    cost_cap_usd,
    curiosity_budget_var,
    curiosity_enabled,
    current_curiosity_budget,
    ledger_persist_enabled,
    posture_allowlist,
    questions_per_session,
)


# ---------------------------------------------------------------------------
# (A) Env knob defaults + master-off composition
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_CURIOSITY_ENABLED defaults to false (Slice 1)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    assert curiosity_enabled() is False


def test_master_explicit_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    assert curiosity_enabled() is True


def test_questions_per_session_default_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default 3 questions per session when master is on."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", raising=False)
    assert questions_per_session() == 3


def test_master_off_force_disables_questions_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master off → questions_per_session forced to 0 regardless of env."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", "100")
    assert questions_per_session() == 0


def test_cost_cap_default_005(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default per-question cost cap $0.05 (operator-binding)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CURIOSITY_COST_CAP_USD", raising=False)
    assert cost_cap_usd() == 0.05


def test_master_off_force_disables_cost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master off → cost cap forced to 0.0 → rejects everything."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CURIOSITY_COST_CAP_USD", "1.00")
    assert cost_cap_usd() == 0.0


def test_posture_allowlist_default_explore_consolidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default allowlist EXPLORE+CONSOLIDATE per operator binding (HARDEN
    excluded by design; MAINTAIN excluded for Slice 1)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CURIOSITY_POSTURE_ALLOWLIST", raising=False)
    al = posture_allowlist()
    assert al == frozenset({"EXPLORE", "CONSOLIDATE"})


def test_master_off_force_disables_posture_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CURIOSITY_POSTURE_ALLOWLIST", "EXPLORE,HARDEN")
    assert posture_allowlist() == frozenset()


def test_ledger_persist_off_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED", "true")
    assert ledger_persist_enabled() is False


# ---------------------------------------------------------------------------
# (B) Charge composition — first deny wins, in documented order
# ---------------------------------------------------------------------------


def test_master_off_denies_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master off → every charge returns Denied(MASTER_OFF), counter
    never increments."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    result = bud.try_charge("Should I refactor X?", est_cost_usd=0.01)
    assert result.allowed is False
    assert result.deny_reason is DenyReason.MASTER_OFF
    assert bud.questions_used == 0
    assert bud.cost_burn_usd == 0.0


def test_invalid_question_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty / non-string question text → INVALID_QUESTION before posture check."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    for bad in ("", "   ", None):
        result = bud.try_charge(bad, est_cost_usd=0.01)  # type: ignore[arg-type]
        assert result.allowed is False
        assert result.deny_reason is DenyReason.INVALID_QUESTION
    assert bud.questions_used == 0


def test_posture_disallowed_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Posture HARDEN (excluded from default allowlist) → POSTURE_DISALLOWED."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="HARDEN")
    result = bud.try_charge("Q?", est_cost_usd=0.01)
    assert result.allowed is False
    assert result.deny_reason is DenyReason.POSTURE_DISALLOWED
    assert "HARDEN" in result.detail
    assert bud.questions_used == 0


def test_questions_quota_exhausted_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 3 successful charges, the 4th returns QUESTIONS_EXHAUSTED."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", "3")
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    for i in range(3):
        result = bud.try_charge(f"Q{i}?", est_cost_usd=0.01)
        assert result.allowed is True, f"charge {i} must be allowed"
    # 4th — denied
    result = bud.try_charge("Q3?", est_cost_usd=0.01)
    assert result.allowed is False
    assert result.deny_reason is DenyReason.QUESTIONS_EXHAUSTED
    assert "used=3/cap=3" in result.detail
    assert bud.questions_used == 3  # not incremented past cap


def test_cost_cap_exceeded_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """est_cost_usd > $0.05 default → COST_EXCEEDED."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    result = bud.try_charge("expensive question?", est_cost_usd=0.10)
    assert result.allowed is False
    assert result.deny_reason is DenyReason.COST_EXCEEDED
    assert "$0.1000" in result.detail
    assert bud.questions_used == 0


# ---------------------------------------------------------------------------
# (C) Counter increments only on Allowed
# ---------------------------------------------------------------------------


def test_allowed_charge_increments_counter_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    result = bud.try_charge("Q?", est_cost_usd=0.02)
    assert result.allowed is True
    assert result.question_id is not None
    assert bud.questions_used == 1
    assert bud.cost_burn_usd == pytest.approx(0.02)
    assert bud.questions_remaining == 2  # 3 cap - 1 used


def test_remaining_quota_zero_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """questions_remaining returns 0 when master is off."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    bud = CuriosityBudget(op_id="op-test-001", posture_at_arm="EXPLORE")
    assert bud.questions_remaining == 0


# ---------------------------------------------------------------------------
# (D) ContextVar default-None + propagation
# ---------------------------------------------------------------------------


def test_current_curiosity_budget_default_none():
    """Outside any binding, current_curiosity_budget() returns None."""
    assert current_curiosity_budget() is None


@pytest.mark.asyncio
async def test_contextvar_propagates_through_create_task() -> None:
    """ContextVars survive `asyncio.create_task` boundaries — same pattern
    as W3(7) cancel_token + W2(4) Path 3 plan_exploit override. Critical
    for Slice 2's tool_executor Rule 14 to see the budget from any
    Venom tool task spawned during GENERATE."""
    bud = CuriosityBudget(op_id="op-ctx-test", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    async def _child_reads():
        return current_curiosity_budget()

    got = await asyncio.create_task(_child_reads())
    assert got is bud


# ---------------------------------------------------------------------------
# (E) Ledger schema + persistence
# ---------------------------------------------------------------------------


def test_record_schema_curiosity_1():
    """Schema version is curiosity.1 and the JSONL line round-trips."""
    rec = CuriosityRecord(
        schema_version="curiosity.1",
        question_id="qid-x",
        op_id="op-x",
        posture_at_charge="EXPLORE",
        question_text="Q?",
        est_cost_usd=0.02,
        issued_at_monotonic=0.0,
        issued_at_iso="2026-04-25T03:00:00Z",
        result="allowed",
    )
    line = rec.to_jsonl()
    parsed = json.loads(line)
    assert parsed["schema_version"] == "curiosity.1"
    assert parsed["question_id"] == "qid-x"
    assert parsed["result"] == "allowed"


def test_persist_writes_jsonl_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Ledger writes to <session_dir>/curiosity_ledger.jsonl when persist
    sub-flag is on AND session_dir is set."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED", "true")
    bud = CuriosityBudget(
        op_id="op-persist-001",
        posture_at_arm="EXPLORE",
        session_dir=tmp_path,
    )
    bud.try_charge("Q1?", est_cost_usd=0.01)
    bud.try_charge("Q2?", est_cost_usd=0.02)

    artifact = tmp_path / "curiosity_ledger.jsonl"
    assert artifact.exists()
    lines = [
        json.loads(line)
        for line in artifact.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert all(line["op_id"] == "op-persist-001" for line in lines)
    assert all(line["result"] == "allowed" for line in lines)


def test_persist_writes_denied_records_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Denied charges also persist (operators want to see WHY curiosity
    didn't fire). Pinned so future drift doesn't silently drop denials."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(
        op_id="op-deny-001",
        posture_at_arm="HARDEN",  # excluded — every charge denied
        session_dir=tmp_path,
    )
    bud.try_charge("Q?", est_cost_usd=0.01)

    artifact = tmp_path / "curiosity_ledger.jsonl"
    assert artifact.exists()
    parsed = json.loads(artifact.read_text(encoding="utf-8").strip())
    assert parsed["result"] == "denied:posture_disallowed"


def test_persist_skipped_when_no_session_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """No session_dir → log-only mode (helper falls through cleanly)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(
        op_id="op-nosd-001",
        posture_at_arm="EXPLORE",
        session_dir=None,
    )
    result = bud.try_charge("Q?", est_cost_usd=0.01)
    assert result.allowed is True
    # No artifact written — and no crash
    assert not (tmp_path / "curiosity_ledger.jsonl").exists()


# ---------------------------------------------------------------------------
# (F) snapshot() shape for postmortem
# ---------------------------------------------------------------------------


def test_snapshot_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "true")
    bud = CuriosityBudget(op_id="op-snap-001", posture_at_arm="explore")
    bud.try_charge("Q?", est_cost_usd=0.03)
    snap = bud.snapshot()
    assert snap == {
        "op_id": "op-snap-001",
        "posture_at_arm": "EXPLORE",  # normalized uppercase
        "questions_used": 1,
        "questions_remaining": 2,
        "cost_burn_usd": 0.03,
    }
