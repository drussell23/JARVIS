"""Phase 9 Slice 5 — lineage waiver predicate + ledger integration
+ UX warning regression spine.

Pins per operator binding 2026-05-05:

  * Single, named, tested predicate at module level
  * Module-level constant ``LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX``
    pinned to exact bytes; AST regression catches drift
  * ``endswith`` matching (NEVER ``in``) — operator-mandated
    tightness; AST regression catches loosening
  * Predicate restricts to ``outcome=='runner'`` literal — never
    widens to other outcome classes; AST regression catches widening
  * Sole-path enforcement: the suffix string literal MUST NOT appear
    outside this module (other than in the harness emit site +
    test fixtures)
  * Ledger ``progress()`` integration: legacy rows surface in
    ``runner_legacy_downgrade`` bucket; canonical ``runner`` count
    excludes them; ``is_eligible()`` flips from blocked → unblocked
  * Real-artifact regression: validates against the actual
    ledger contents — proves Slice 4 green-soak's 1 clean session
    now flips JARVIS_DECISION_TRACE_LEDGER_ENABLED from
    [RUNNER-BLOCKED] to [PENDING] (clean=1/3 runner=0,
    runner_legacy_downgrade=2)
  * UX footgun: queue/ready prints warning when
    JARVIS_GRADUATION_LEDGER_ENABLED is unset

Verifies (24 tests).
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.graduation.lineage_waiver import (
    LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX,
    is_legacy_contract_downgrade,
    register_shipped_invariants,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Module constant
# ---------------------------------------------------------------------------


def test_legacy_suffix_constant_value():
    """The suffix MUST be the exact bytes the harness emits in
    notes after the contract downgrade path. Drift breaks the
    waiver silently."""
    assert (
        LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX
        == "contract_predicate_downgraded_clean"
    )


# ---------------------------------------------------------------------------
# Predicate behavior — positive matches
# ---------------------------------------------------------------------------


def test_predicate_canonical_note_matches():
    """The exact note shape the harness emits."""
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes=(
            "complete_no_runner_failures|"
            "contract_predicate_downgraded_clean"
        ),
    ) is True


def test_predicate_bare_suffix_matches():
    """When notes is just the suffix (no preamble)."""
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes="contract_predicate_downgraded_clean",
    ) is True


# ---------------------------------------------------------------------------
# Predicate behavior — negative matches (tightness contract)
# ---------------------------------------------------------------------------


def test_predicate_clean_outcome_never_waived():
    """CLEAN rows MUST NEVER be waived — even if their notes
    happen to end with the suffix (impossible in practice but
    pinned for safety)."""
    assert is_legacy_contract_downgrade(
        outcome="clean",
        notes="contract_predicate_downgraded_clean",
    ) is False


def test_predicate_infra_outcome_never_waived():
    assert is_legacy_contract_downgrade(
        outcome="infra",
        notes="contract_predicate_downgraded_clean",
    ) is False


def test_predicate_migration_outcome_never_waived():
    assert is_legacy_contract_downgrade(
        outcome="migration",
        notes="contract_predicate_downgraded_clean",
    ) is False


def test_predicate_real_runner_failure_not_waived():
    """Real runner-class failures (Venom / orchestrator / etc.)
    have notes that do NOT end with the legacy suffix. They MUST
    continue to block eligibility."""
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes="phase_runner_error|iron_gate_violation",
    ) is False


def test_predicate_substring_match_does_not_fire():
    """Operator-mandated tightness: the suffix appearing
    mid-string MUST NOT trigger the waiver — only end-of-notes
    match counts."""
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes=(
            "contract_predicate_downgraded_clean|"
            "real_runner_error_appended"
        ),
    ) is False


def test_predicate_empty_notes_not_waived():
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes="",
    ) is False


# ---------------------------------------------------------------------------
# Defensive — NEVER raises
# ---------------------------------------------------------------------------


def test_predicate_handles_none_outcome():
    assert is_legacy_contract_downgrade(
        outcome=None,  # type: ignore
        notes="contract_predicate_downgraded_clean",
    ) is False


def test_predicate_handles_none_notes():
    assert is_legacy_contract_downgrade(
        outcome="runner",
        notes=None,  # type: ignore
    ) is False


def test_predicate_handles_int_inputs():
    assert is_legacy_contract_downgrade(
        outcome=42,  # type: ignore
        notes=42,  # type: ignore
    ) is False


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "lineage_waiver_constant_value_pinned",
        "lineage_waiver_uses_endswith_not_in",
        "lineage_waiver_outcome_check_pinned",
    }


def test_all_pins_validate_clean():
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation"
        / "lineage_waiver.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_constant_pin_fires_on_drift():
    bad_source = '''
LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX: str = "different_suffix"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "constant_value_pinned" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "contract_predicate_downgraded_clean" in v
        for v in violations
    )


def test_endswith_pin_fires_on_in_operator():
    """Operator-mandated tightness regression: if a future
    refactor swaps endswith for `in`, the pin fires."""
    bad_source = '''
LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX = "contract_predicate_downgraded_clean"

def is_legacy_contract_downgrade(*, outcome, notes):
    if outcome != "runner":
        return False
    return LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX in notes
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "endswith_not_in" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "endswith" in v or "tightness" in v
        for v in violations
    )


def test_outcome_check_pin_fires_on_widening():
    """If a future refactor widens to other outcome classes,
    the pin fires."""
    bad_source = '''
def is_legacy_contract_downgrade(*, outcome, notes):
    if outcome not in ("runner", "infra"):
        return False
    return notes.endswith("contract_predicate_downgraded_clean")
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "outcome_check_pinned" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "runner" in v.lower() for v in violations
    )


# ---------------------------------------------------------------------------
# Ledger progress() integration
# ---------------------------------------------------------------------------


def test_progress_filters_legacy_runners(tmp_path, monkeypatch):
    """End-to-end: a flag with ONLY legacy contract-downgrade
    'runner' rows should report runner=0 (not blocked) and
    runner_legacy_downgrade=N (audit-visible)."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        GraduationLedger, SessionRecord, SessionOutcome,
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    ledger_path = tmp_path / "test_ledger.jsonl"
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH", str(ledger_path),
    )
    ledger = GraduationLedger(path=ledger_path)
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    # 2 legacy runner rows + 1 real clean row.
    legacy_note = (
        "complete_no_runner_failures|"
        "contract_predicate_downgraded_clean"
    )
    ledger.record_session(
        flag_name=flag, session_id="legacy-1",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        notes=legacy_note,
    )
    ledger.record_session(
        flag_name=flag, session_id="legacy-2",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        notes=legacy_note,
    )
    ledger.record_session(
        flag_name=flag, session_id="clean-1",
        outcome=SessionOutcome.CLEAN,
        recorded_by="test",
        notes="complete_no_runner_failures",
    )
    progress = ledger.progress(flag)
    assert progress["runner"] == 0, (
        "legacy contract-downgrade rows MUST NOT count as runner"
    )
    assert progress["runner_legacy_downgrade"] == 2, (
        "legacy rows MUST be visible in audit bucket"
    )
    assert progress["clean"] == 1


def test_progress_real_runner_still_blocks(
    tmp_path, monkeypatch,
):
    """Critical safety regression: a REAL runner-class failure
    (notes do NOT end with legacy suffix) MUST still count as
    runner and block eligibility. The waiver MUST NOT weaken
    'no runner failures.'"""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        GraduationLedger, SessionOutcome,
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    ledger_path = tmp_path / "test_ledger.jsonl"
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH", str(ledger_path),
    )
    ledger = GraduationLedger(path=ledger_path)
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    ledger.record_session(
        flag_name=flag, session_id="real-runner-1",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        notes="phase_runner_error|iron_gate_violation",
    )
    progress = ledger.progress(flag)
    assert progress["runner"] == 1, (
        "real runner-class failure MUST count toward runner "
        "block — waiver MUST NOT weaken safety property"
    )
    assert progress["runner_legacy_downgrade"] == 0


def test_eligible_unblocks_after_waiver(tmp_path, monkeypatch):
    """The full eligibility chain: flag with 3 clean + 2 legacy-
    runner rows should be ELIGIBLE per is_eligible() because
    runner=0 (legacy filtered out) and clean>=required."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        GraduationLedger, SessionOutcome,
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    ledger_path = tmp_path / "test_ledger.jsonl"
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH", str(ledger_path),
    )
    ledger = GraduationLedger(path=ledger_path)
    flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    legacy_note = (
        "complete_no_runner_failures|"
        "contract_predicate_downgraded_clean"
    )
    for i in range(2):
        ledger.record_session(
            flag_name=flag,
            session_id=f"legacy-{i}",
            outcome=SessionOutcome.RUNNER,
            recorded_by="test",
            notes=legacy_note,
        )
    for i in range(3):
        ledger.record_session(
            flag_name=flag,
            session_id=f"clean-{i}",
            outcome=SessionOutcome.CLEAN,
            recorded_by="test",
            notes="complete_no_runner_failures",
        )
    assert ledger.is_eligible(flag) is True


def test_zero_progress_includes_audit_bucket():
    """Master-off path returns shape-compatible dict including
    the audit bucket so callers never KeyError."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        _zero_progress,
    )
    p = _zero_progress("JARVIS_DECISION_TRACE_LEDGER_ENABLED")
    assert p["runner_legacy_downgrade"] == 0
    assert "clean" in p
    assert "runner" in p


# ---------------------------------------------------------------------------
# Real-artifact regression — proves end-to-end fix
# ---------------------------------------------------------------------------


def test_real_ledger_unblocks_target_flag():
    """Citation-purposes: load the real graduation_ledger.jsonl
    and verify Slice 5 + Slice 4 together flip
    JARVIS_DECISION_TRACE_LEDGER_ENABLED from
    [RUNNER-BLOCKED] to [PENDING]. This is the structural
    proof that the green-soak proof v2 is now actionable."""
    ledger_path = (
        _repo_root() / ".jarvis" / "graduation_ledger.jsonl"
    )
    if not ledger_path.exists():
        pytest.skip("real ledger fixture missing")
    rows = []
    for line in ledger_path.read_text(
        encoding="utf-8",
    ).splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    target_flag = "JARVIS_DECISION_TRACE_LEDGER_ENABLED"
    target_rows = [
        r for r in rows if r.get("flag_name") == target_flag
    ]
    if not target_rows:
        pytest.skip("real ledger has no rows for target flag")
    # Manually compute progress via the predicate to prove the
    # logic without going through the full ledger reader (which
    # has its own setup requirements).
    canonical_runner_count = 0
    legacy_runner_count = 0
    clean_count = 0
    for r in target_rows:
        outcome = r.get("outcome", "")
        notes = r.get("notes", "")
        if outcome == "clean":
            clean_count += 1
        elif outcome == "runner":
            if is_legacy_contract_downgrade(
                outcome=outcome, notes=notes,
            ):
                legacy_runner_count += 1
            else:
                canonical_runner_count += 1
    # The proof: legacy rows ≥ 1 (they exist in the ledger),
    # canonical runner = 0 (no real runner failures), clean ≥ 1
    # (the green-soak proof v2 succeeded).
    assert legacy_runner_count >= 1, (
        "real ledger should have at least 1 legacy "
        "contract-downgrade row from the failed soaks"
    )
    assert canonical_runner_count == 0, (
        f"real ledger has {canonical_runner_count} canonical "
        "runner rows that the waiver cannot filter — flag is "
        "still legitimately blocked"
    )
    assert clean_count >= 1, (
        "real ledger should have ≥1 clean row from green-soak "
        "proof v2"
    )


# ---------------------------------------------------------------------------
# UX footgun — warning when env unset
# ---------------------------------------------------------------------------


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "live_fire_graduation_soak_cli",
        _repo_root() / "scripts/live_fire_graduation_soak.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_warn_when_master_unset(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_GRADUATION_LEDGER_ENABLED", raising=False,
    )
    module = _load_cli_module()
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        module._warn_if_ledger_master_unset()
    output = captured.getvalue()
    assert "JARVIS_GRADUATION_LEDGER_ENABLED is unset" in output
    assert "progress counters will read as zeros" in output


def test_no_warn_when_master_set(monkeypatch):
    """Operator with proper env should NOT see warning noise."""
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_ENABLED", "true",
    )
    module = _load_cli_module()
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        module._warn_if_ledger_master_unset()
    output = captured.getvalue()
    assert "JARVIS_GRADUATION_LEDGER_ENABLED is unset" not in (
        output
    )


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_no_warn_for_all_truthy_values(monkeypatch, truthy):
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_ENABLED", truthy,
    )
    module = _load_cli_module()
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        module._warn_if_ledger_master_unset()
    assert "is unset" not in captured.getvalue()
