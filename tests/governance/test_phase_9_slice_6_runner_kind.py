"""Phase 9 Slice 6 — runner_attributed_kind structured-field
regression spine.

Pins per operator binding 2026-05-05 ("solve root, no shortcuts,
leverage existing"):

  * RunnerAttributedKind closed taxonomy (12 values) bytes-pinned
  * CONCRETE_RUNNER_FAILURE_CLASS_VALUES mirrors live_fire_soak's
    _RUNNER_FAILURE_CLASSES (zero drift)
  * `infer_runner_kind` decision tree mirrors classify_outcome
  * `derive_runner_kind_from_classification` composes
    lineage_waiver.is_legacy_contract_downgrade (single source
    of truth — no parallel suffix scan)
  * SessionRecord round-trips runner_attributed_kind through
    JSON; legacy rows (no field) deserialize as None
  * progress() routes via STRUCTURED field first, suffix back-
    compat shim only when structured field is absent
  * is_blocking_kind / is_legacy_downgrade_kind public selectors
  * AST pins all fire clean; synthetic regression confirms pins
    fire on taxonomy drift
  * Public API stable

Verifies (37 tests).
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_has_12_values():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind,
    )
    assert len(list(RunnerAttributedKind)) == 12


def test_taxonomy_values_bytes_pinned():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind,
    )
    expected = {
        "phase_runner_error", "candidate_validate_error",
        "iron_gate_violation", "semantic_guardian_block",
        "change_engine_error", "verify_regression",
        "l2_repair_error", "fsm_state_corruption",
        "artifact_contract_drift",
        "contract_downgrade_legacy", "default_conservative", "none",
    }
    actual = {k.value for k in RunnerAttributedKind}
    assert actual == expected


def test_concrete_subset_matches_live_fire_soak():
    """Bytes-pinned mirror — zero drift between regex side and
    structural side."""
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        CONCRETE_RUNNER_FAILURE_CLASS_VALUES,
    )
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        _RUNNER_FAILURE_CLASSES,
    )
    assert (
        CONCRETE_RUNNER_FAILURE_CLASS_VALUES == _RUNNER_FAILURE_CLASSES
    )


# ---------------------------------------------------------------------------
# infer_runner_kind decision tree
# ---------------------------------------------------------------------------


def test_infer_returns_none_when_not_runner_attributed():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, infer_runner_kind,
    )
    assert infer_runner_kind(
        runner_attributed=False,
    ) == RunnerAttributedKind.NONE


def test_infer_returns_first_concrete_hit_sorted():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, infer_runner_kind,
    )
    # Multiple hits — must return first by sorted order
    # (deterministic; not iteration-order dependent).
    result = infer_runner_kind(
        runner_attributed=True,
        runner_hits=["verify_regression", "iron_gate_violation"],
    )
    # Sorted: ["iron_gate_violation", "verify_regression"]
    assert result == RunnerAttributedKind.IRON_GATE_VIOLATION


def test_infer_skips_non_concrete_hits():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, infer_runner_kind,
    )
    # Hits not in the concrete set fall through to default
    result = infer_runner_kind(
        runner_attributed=True,
        runner_hits=["unknown_class_xyz"],
    )
    assert result == RunnerAttributedKind.DEFAULT_CONSERVATIVE


def test_infer_default_conservative_on_empty_hits():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, infer_runner_kind,
    )
    assert infer_runner_kind(
        runner_attributed=True, runner_hits=None,
    ) == RunnerAttributedKind.DEFAULT_CONSERVATIVE


def test_infer_never_raises_on_garbage():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        infer_runner_kind,
    )
    # Garbage input should default-conservative, not crash
    result = infer_runner_kind(
        runner_attributed=True,
        runner_hits=[None, 42, object()],  # type: ignore
    )
    # Doesn't raise, returns valid enum
    assert result is not None


# ---------------------------------------------------------------------------
# Selector functions
# ---------------------------------------------------------------------------


def test_is_blocking_kind_excludes_legacy_downgrade():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, is_blocking_kind,
    )
    assert is_blocking_kind(
        RunnerAttributedKind.CONTRACT_DOWNGRADE_LEGACY,
    ) is False


def test_is_blocking_kind_excludes_none():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, is_blocking_kind,
    )
    assert is_blocking_kind(RunnerAttributedKind.NONE) is False


def test_is_blocking_kind_includes_concrete_classes():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, is_blocking_kind,
    )
    assert is_blocking_kind(
        RunnerAttributedKind.IRON_GATE_VIOLATION,
    ) is True
    assert is_blocking_kind(
        RunnerAttributedKind.CHANGE_ENGINE_ERROR,
    ) is True


def test_is_blocking_kind_includes_default_conservative():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, is_blocking_kind,
    )
    assert is_blocking_kind(
        RunnerAttributedKind.DEFAULT_CONSERVATIVE,
    ) is True


def test_is_blocking_kind_returns_false_on_none_input():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        is_blocking_kind,
    )
    assert is_blocking_kind(None) is False


def test_is_legacy_downgrade_kind_only_matches_legacy():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, is_legacy_downgrade_kind,
    )
    assert is_legacy_downgrade_kind(
        RunnerAttributedKind.CONTRACT_DOWNGRADE_LEGACY,
    ) is True
    assert is_legacy_downgrade_kind(
        RunnerAttributedKind.IRON_GATE_VIOLATION,
    ) is False
    assert is_legacy_downgrade_kind(None) is False


# ---------------------------------------------------------------------------
# coerce_kind
# ---------------------------------------------------------------------------


def test_coerce_kind_handles_string_value():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, coerce_kind,
    )
    assert coerce_kind(
        "iron_gate_violation",
    ) == RunnerAttributedKind.IRON_GATE_VIOLATION


def test_coerce_kind_returns_none_on_unknown():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        coerce_kind,
    )
    assert coerce_kind("not_a_real_kind") is None
    assert coerce_kind("") is None
    assert coerce_kind(None) is None


def test_coerce_kind_passes_through_enum():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        RunnerAttributedKind, coerce_kind,
    )
    val = RunnerAttributedKind.VERIFY_REGRESSION
    assert coerce_kind(val) is val


# ---------------------------------------------------------------------------
# derive_runner_kind_from_classification — composition
# ---------------------------------------------------------------------------


def test_derive_returns_none_for_non_runner():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        derive_runner_kind_from_classification,
    )
    assert derive_runner_kind_from_classification(
        summary={"session_outcome": "complete"},
        outcome_str="clean",
        runner_attributed=False,
        class_notes="complete_no_runner_failures",
    ) is None


def test_derive_returns_legacy_on_suffix_match():
    """Composes lineage_waiver.is_legacy_contract_downgrade —
    single source of truth for the suffix lineage."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        derive_runner_kind_from_classification,
    )
    result = derive_runner_kind_from_classification(
        summary={},
        outcome_str="runner",
        runner_attributed=True,
        class_notes=(
            "complete_no_runner_failures|"
            "contract_predicate_downgraded_clean"
        ),
    )
    assert result == "contract_downgrade_legacy"


def test_derive_returns_first_concrete_hit_from_summary():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        derive_runner_kind_from_classification,
    )
    result = derive_runner_kind_from_classification(
        summary={
            "failure_class_counts": {
                "iron_gate_violation": 1,
                "verify_regression": 0,  # zero count — skipped
            },
        },
        outcome_str="runner",
        runner_attributed=True,
        class_notes="runner_classes:['iron_gate_violation']",
    )
    assert result == "iron_gate_violation"


def test_derive_returns_default_when_no_hits():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        derive_runner_kind_from_classification,
    )
    result = derive_runner_kind_from_classification(
        summary={},
        outcome_str="runner",
        runner_attributed=True,
        class_notes="default_runner:outcome=|stop=",
    )
    assert result == "default_conservative"


def test_derive_never_raises():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        derive_runner_kind_from_classification,
    )
    # Bad inputs
    for summary in [None, "string", 123, []]:
        try:
            derive_runner_kind_from_classification(
                summary=summary,  # type: ignore
                outcome_str="runner",
                runner_attributed=True,
                class_notes="",
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"raised on summary={summary!r}: {exc}")


# ---------------------------------------------------------------------------
# SessionRecord round-trip
# ---------------------------------------------------------------------------


def test_session_record_omits_kind_when_none():
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, SessionRecord,
    )
    rec = SessionRecord(
        flag_name="x",
        session_id="s1",
        outcome=SessionOutcome.CLEAN,
        recorded_at_iso="2026-05-05T00:00:00Z",
        recorded_at_epoch=1.0,
        recorded_by="test",
        notes="",
    )
    d = rec.to_dict()
    assert "runner_attributed_kind" not in d


def test_session_record_includes_kind_when_present():
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, SessionRecord,
    )
    rec = SessionRecord(
        flag_name="x",
        session_id="s1",
        outcome=SessionOutcome.RUNNER,
        recorded_at_iso="2026-05-05T00:00:00Z",
        recorded_at_epoch=1.0,
        recorded_by="test",
        notes="",
        runner_attributed_kind="iron_gate_violation",
    )
    d = rec.to_dict()
    assert d["runner_attributed_kind"] == "iron_gate_violation"


# ---------------------------------------------------------------------------
# Ledger end-to-end — write + read + progress aggregation
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_ledger(monkeypatch, tmp_path):
    """Isolate to a temp ledger path with the master flag on."""
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "1")
    target = tmp_path / "graduation_ledger.jsonl"
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.adaptation."
        "graduation_ledger.ledger_path",
        lambda: target,
    )
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        GraduationLedger,
    )
    return GraduationLedger(path=target)


def test_record_session_persists_kind(temp_ledger):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome,
    )
    flag = next(
        f for f in __import__(
            "backend.core.ouroboros.governance.adaptation."
            "graduation_ledger",
            fromlist=["known_flags"],
        ).known_flags()
    )
    ok, _ = temp_ledger.record_session(
        flag_name=flag,
        session_id="s-test-1",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        runner_attributed_kind="iron_gate_violation",
    )
    assert ok is True
    raw = temp_ledger.path.read_text(encoding="utf-8")
    assert "iron_gate_violation" in raw


def test_record_session_coerces_unknown_kind_to_none(temp_ledger):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, known_flags,
    )
    flag = next(iter(known_flags()))
    ok, _ = temp_ledger.record_session(
        flag_name=flag,
        session_id="s-test-bad-kind",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        runner_attributed_kind="not_a_real_kind",  # garbage
    )
    assert ok is True
    raw = temp_ledger.path.read_text(encoding="utf-8")
    # Unknown kind coerced to None — field omitted from JSON
    assert "not_a_real_kind" not in raw


def test_legacy_row_without_kind_routes_via_suffix_shim(
    temp_ledger,
):
    """Legacy row: no runner_attributed_kind, notes ends with
    canonical suffix — must route to runner_legacy_downgrade
    via the back-compat shim."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, known_flags,
    )
    flag = next(iter(known_flags()))
    # Manually write a legacy row (no runner_attributed_kind)
    row = {
        "flag_name": flag,
        "session_id": "legacy-1",
        "outcome": "runner",
        "recorded_at_iso": "2026-04-01T00:00:00Z",
        "recorded_at_epoch": 1.0,
        "recorded_by": "legacy-test",
        "notes": (
            "complete_no_runner_failures|"
            "contract_predicate_downgraded_clean"
        ),
    }
    with temp_ledger.path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    progress = temp_ledger.progress(flag)
    assert progress["runner"] == 0
    assert progress["runner_legacy_downgrade"] == 1


def test_structured_field_routes_canonical_path(temp_ledger):
    """New row WITH structured kind must route via the
    structured field — not the suffix shim."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, known_flags,
    )
    flag = next(iter(known_flags()))
    # Notes do NOT contain the suffix — only the structured
    # kind drives routing.
    ok, _ = temp_ledger.record_session(
        flag_name=flag,
        session_id="new-row-1",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        notes="some_unrelated_notes",
        runner_attributed_kind="contract_downgrade_legacy",
    )
    assert ok is True
    progress = temp_ledger.progress(flag)
    # Structured field routed to legacy bucket — NOT runner.
    assert progress["runner"] == 0
    assert progress["runner_legacy_downgrade"] == 1


def test_structured_runner_kind_stays_blocking(temp_ledger):
    """A row with a CONCRETE runner kind (not legacy) MUST stay
    in the runner bucket — block graduation."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
        SessionOutcome, known_flags,
    )
    flag = next(iter(known_flags()))
    ok, _ = temp_ledger.record_session(
        flag_name=flag,
        session_id="real-failure",
        outcome=SessionOutcome.RUNNER,
        recorded_by="test",
        notes=(
            "complete_no_runner_failures|"
            "contract_predicate_downgraded_clean"
        ),  # suffix in notes — would falsely route via shim
        runner_attributed_kind="iron_gate_violation",  # but real failure
    )
    assert ok is True
    progress = temp_ledger.progress(flag)
    # Structured field wins — must stay in runner (block flip)
    assert progress["runner"] == 1
    assert progress["runner_legacy_downgrade"] == 0


# ---------------------------------------------------------------------------
# AST pins — runner_kind module
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    names = {i.invariant_name for i in invs}
    assert names == {
        "runner_kind_taxonomy_closed",
        "runner_kind_concrete_set_matches_live_fire",
        "runner_kind_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation/runner_kind.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_taxonomy_pin_fires_on_unauthorized_addition():
    """Synthetic regression — adding an unsanctioned enum value
    must trip the pin."""
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        register_shipped_invariants,
    )
    bad_source = '''
import enum

class RunnerAttributedKind(str, enum.Enum):
    PHASE_RUNNER_ERROR = "phase_runner_error"
    CANDIDATE_VALIDATE_ERROR = "candidate_validate_error"
    IRON_GATE_VIOLATION = "iron_gate_violation"
    SEMANTIC_GUARDIAN_BLOCK = "semantic_guardian_block"
    CHANGE_ENGINE_ERROR = "change_engine_error"
    VERIFY_REGRESSION = "verify_regression"
    L2_REPAIR_ERROR = "l2_repair_error"
    FSM_STATE_CORRUPTION = "fsm_state_corruption"
    ARTIFACT_CONTRACT_DRIFT = "artifact_contract_drift"
    CONTRACT_DOWNGRADE_LEGACY = "contract_downgrade_legacy"
    DEFAULT_CONSERVATIVE = "default_conservative"
    NONE = "none"
    UNAUTHORIZED_NEW_KIND = "unauthorized_new_kind"
'''
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("unexpected values" in v for v in violations)


def test_concrete_set_pin_fires_on_drift():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        register_shipped_invariants,
    )
    bad_source = '''
from typing import FrozenSet
CONCRETE_RUNNER_FAILURE_CLASS_VALUES: FrozenSet[str] = frozenset({
    "phase_runner_error",
    "drift_value_xyz",
})
'''
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "concrete_set" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


def test_authority_asymmetry_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.graduation.runner_kind import (
        register_shipped_invariants,
    )
    bad_source = '''
from backend.core.ouroboros.governance.orchestrator import x
'''
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance.graduation import runner_kind
    expected = {
        "CONCRETE_RUNNER_FAILURE_CLASS_VALUES",
        "RunnerAttributedKind",
        "coerce_kind",
        "infer_runner_kind",
        "is_blocking_kind",
        "is_legacy_downgrade_kind",
        "register_shipped_invariants",
    }
    assert set(runner_kind.__all__) == expected


def test_evidence_row_omits_kind_when_none():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        EvidenceRow,
    )
    row = EvidenceRow(
        schema_version="1.0",
        harness_status="OK",
        flag_name="x",
        session_id="s",
        outcome="clean",
        runner_attributed=False,
        stop_reason="",
        cost_total_usd=0.0,
        duration_s=1.0,
        ops_count=1,
        failure_class_counts={},
        deps_set=[],
        started_at_iso="",
        started_at_epoch=0.0,
        finished_at_iso="",
        finished_at_epoch=0.0,
        notes="",
    )
    d = row.to_dict()
    assert "runner_attributed_kind" not in d


def test_evidence_row_serializes_kind():
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        EvidenceRow,
    )
    row = EvidenceRow(
        schema_version="1.0",
        harness_status="OK",
        flag_name="x",
        session_id="s",
        outcome="runner",
        runner_attributed=True,
        stop_reason="",
        cost_total_usd=0.0,
        duration_s=1.0,
        ops_count=1,
        failure_class_counts={"iron_gate_violation": 1},
        deps_set=[],
        started_at_iso="",
        started_at_epoch=0.0,
        finished_at_iso="",
        finished_at_epoch=0.0,
        notes="",
        runner_attributed_kind="iron_gate_violation",
    )
    d = row.to_dict()
    assert d["runner_attributed_kind"] == "iron_gate_violation"


# ---------------------------------------------------------------------------
# Integration — progress() routes via structured field
# ---------------------------------------------------------------------------


def test_progress_uses_structured_field_first_via_grep():
    """AST regression: in the per-row routing loop, the
    structured-field check fires BEFORE the suffix back-compat
    shim. Anchored to the per-row routing block (``if
    outcome_key == "runner":``) so we don't accidentally match
    the lazy-import line at the top of progress() where
    is_legacy_contract_downgrade is *imported* (not called)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/adaptation/"
        "graduation_ledger.py"
    )
    source = target.read_text(encoding="utf-8")
    # Anchor to the routing block — there's only one such block.
    routing_idx = source.find('if outcome_key == "runner":')
    assert routing_idx >= 0, (
        "progress() must contain the routing block"
    )
    body = source[routing_idx:routing_idx + 4000]
    structured_idx = body.find("runner_attributed_kind is not None")
    # Match the *call* site, not the import — `is_legacy_contract_downgrade(`
    # call has the trailing `outcome=` kwarg.
    suffix_call_idx = body.find(
        "is_legacy_contract_downgrade(\n                        outcome=",
    )
    if suffix_call_idx == -1:
        suffix_call_idx = body.find(
            "is_legacy_contract_downgrade(",
        )
    assert structured_idx >= 0, (
        "progress() routing block must check structured field"
    )
    assert suffix_call_idx >= 0, (
        "progress() routing block must call suffix shim"
    )
    assert structured_idx < suffix_call_idx, (
        "structured field must be checked BEFORE suffix shim "
        "in the routing block"
    )
