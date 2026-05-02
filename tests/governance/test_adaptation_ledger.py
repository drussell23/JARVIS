"""RR Pass C Slice 1 — AdaptationLedger substrate regression suite.

Pins:
  * Module constants + 5-value AdaptationSurface enum + 3-value
    OperatorDecisionStatus + 2-value MonotonicTighteningVerdict +
    7-value ProposeStatus + 6-value DecisionStatus.
  * Frozen dataclasses: AdaptationEvidence + AdaptationProposal
    (both with .to_dict / .from_dict round-trip + sha256 integrity).
  * Master flag default-false-pre-graduation.
  * Monotonic-tightening invariant (load-bearing):
    - Same hash → rejected (no_state_change).
    - Kind not in tighten allowlist → rejected.
    - Surface validator returning False → rejected.
    - Surface validator raising → rejected.
    - All checks passed → PASSED verdict, persisted.
  * propose() paths: OK / DISABLED / INVALID_PROPOSAL (4 sub-cases) /
    DUPLICATE_PROPOSAL_ID / CAPACITY_EXCEEDED / WOULD_LOOSEN /
    PERSIST_ERROR.
  * approve/reject paths: OK / DISABLED / NOT_FOUND / NOT_PENDING /
    OPERATOR_REQUIRED.
  * approve sets applied_at non-null; reject leaves applied_at None.
  * Persistence pins: append-only (state transitions write NEW
    lines, NOT overwrite); latest record per proposal_id wins;
    survives new instance; sha256 round-trip; tampered records
    skipped on read; malformed JSON lines skipped.
  * Surface validator registry: register / get / reset / per-surface
    routing.
  * Authority invariants (AST grep): no banned governance imports;
    no subprocess / network / env-mutation tokens.
"""
from __future__ import annotations

import ast as _ast
import dataclasses
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    ADAPTATION_SCHEMA_VERSION,
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    DecisionResult,
    DecisionStatus,
    MAX_EVIDENCE_SUMMARY_CHARS,
    MAX_HISTORY_LINES,
    MAX_OPERATOR_NAME_CHARS,
    MAX_PENDING_PROPOSALS,
    MAX_PROPOSAL_ID_CHARS,
    MAX_SOURCE_EVENT_IDS_PER_PROPOSAL,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    ProposeResult,
    ProposeStatus,
    get_default_ledger,
    get_surface_validator,
    is_enabled,
    ledger_path,
    register_surface_validator,
    reset_default_ledger,
    reset_surface_validators,
    validate_monotonic_tightening,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "ledger.py"
)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH",
        str(tmp_path / "ledger.jsonl"),
    )
    reset_surface_validators()
    yield
    reset_default_ledger()
    reset_surface_validators()


def _ev(observations=3, summary="evidence"):
    return AdaptationEvidence(
        window_days=7, observation_count=observations,
        source_event_ids=("ev-1", "ev-2"), summary=summary,
    )


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


def _propose(
    ledger, *,
    proposal_id="prop-1",
    surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    proposal_kind="add_pattern",
    current_hash="sha256:current",
    proposed_hash="sha256:proposed",
    evidence=None,
):
    return ledger.propose(
        proposal_id=proposal_id,
        surface=surface,
        proposal_kind=proposal_kind,
        evidence=evidence or _ev(),
        current_state_hash=current_hash,
        proposed_state_hash=proposed_hash,
    )


# ===========================================================================
# A — Module constants + enums
# ===========================================================================


def test_schema_version_pinned():
    # Item #2 (2026-04-26) bumped 1.0 → 2.0 (added optional
    # proposed_state_payload field). Pre-Item-#2 rows still readable.
    assert ADAPTATION_SCHEMA_VERSION == "2.0"


def test_caps_pinned():
    assert MAX_PENDING_PROPOSALS == 256
    assert MAX_HISTORY_LINES == 8_192
    assert MAX_EVIDENCE_SUMMARY_CHARS == 1_024
    assert MAX_OPERATOR_NAME_CHARS == 128
    assert MAX_SOURCE_EVENT_IDS_PER_PROPOSAL == 64
    assert MAX_PROPOSAL_ID_CHARS == 128


def test_adaptation_surface_six_values():
    """Pass C shipped with 5 surfaces; Deep Observability Gap #2
    added a 6th (operator-proposed Confidence-monitor threshold
    tightening). Each value below maps to a registered surface
    validator + an ``adapted_*.yaml`` materialization path. Add
    a new value here whenever a new adaptive surface lands."""
    assert {s.value for s in AdaptationSurface} == {
        "semantic_guardian.patterns",
        "iron_gate.exploration_floors",
        "scoped_tool_backend.mutation_budget",
        "risk_tier_floor.tiers",
        "exploration_ledger.category_weights",
        "confidence_monitor.thresholds",
    }


def test_operator_decision_status_three_values():
    assert {s.value for s in OperatorDecisionStatus} == {
        "pending", "approved", "rejected",
    }


def test_monotonic_tightening_verdict_two_values():
    assert {v.value for v in MonotonicTighteningVerdict} == {
        "passed", "rejected:would_loosen",
    }


def test_propose_status_seven_values():
    assert {s.name for s in ProposeStatus} == {
        "OK", "DISABLED", "INVALID_PROPOSAL", "DUPLICATE_PROPOSAL_ID",
        "CAPACITY_EXCEEDED", "WOULD_LOOSEN", "PERSIST_ERROR",
    }


def test_decision_status_six_values():
    assert {s.name for s in DecisionStatus} == {
        "OK", "DISABLED", "NOT_FOUND", "NOT_PENDING",
        "OPERATOR_REQUIRED", "PERSIST_ERROR",
    }


def test_dataclasses_are_frozen():
    e = AdaptationEvidence(window_days=7, observation_count=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.window_days = 5  # type: ignore[misc]
    p = AdaptationProposal(
        schema_version="1.0", proposal_id="x",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind="add_pattern",
        evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="now", proposed_at_epoch=1.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.proposal_id = "y"  # type: ignore[misc]


# ===========================================================================
# B — Master flag
# ===========================================================================


def test_master_flag_off_returns_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "0")
    assert is_enabled() is False
    res = _propose(_ledger(tmp_path))
    assert res.status is ProposeStatus.DISABLED


def test_master_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv("JARVIS_ADAPTATION_LEDGER_ENABLED", raising=False)
    assert is_enabled() is True


def test_disabled_returns_empty_for_read_methods(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "0")
    led = _ledger(tmp_path)
    assert led.list_pending() == ()
    assert led.history() == ()
    assert led.get("any-id") is None


# ===========================================================================
# C — Monotonic-tightening invariant (LOAD-BEARING)
# ===========================================================================


def test_propose_rejects_no_state_change(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(
        led, current_hash="same", proposed_hash="same",
    )
    assert res.status is ProposeStatus.WOULD_LOOSEN
    assert "no_state_change" in res.detail
    # Critically: NOT persisted
    assert not (tmp_path / "ledger.jsonl").exists()


def test_propose_rejects_kind_not_in_allowlist(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(
        led, proposal_kind="loosen_floor",  # not in allowlist
    )
    assert res.status is ProposeStatus.WOULD_LOOSEN
    assert "kind_not_in_tighten_allowlist" in res.detail
    assert not (tmp_path / "ledger.jsonl").exists()


def test_propose_kind_in_allowlist_passes(tmp_path):
    led = _ledger(tmp_path)
    for kind in ("add_pattern", "raise_floor", "lower_budget",
                 "add_tier", "rebalance_weight"):
        res = _propose(
            led, proposal_id=f"prop-{kind}",
            proposal_kind=kind,
        )
        assert res.status is ProposeStatus.OK, (
            f"kind {kind!r} should pass default validator"
        )


def test_surface_validator_rejection_blocks_persist(tmp_path):
    """Surface validator returning False MUST block persistence —
    cage rule per Pass C §4.1."""
    def deny(p):
        return (False, "surface_specific_check_failed")
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, deny,
    )
    led = _ledger(tmp_path)
    res = _propose(led)
    assert res.status is ProposeStatus.WOULD_LOOSEN
    assert "surface_specific_check_failed" in res.detail
    assert not (tmp_path / "ledger.jsonl").exists()


def test_surface_validator_raise_blocks_persist(tmp_path):
    """If a surface validator raises, treat as would-loosen (fail-
    closed cage rule)."""
    def boom(p):
        raise ValueError("validator broken")
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, boom,
    )
    led = _ledger(tmp_path)
    res = _propose(led)
    assert res.status is ProposeStatus.WOULD_LOOSEN
    assert "surface_validator_raised:ValueError" in res.detail


def test_surface_validator_pass_allows_persist(tmp_path):
    def allow(p):
        return (True, "surface_check_ok")
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, allow,
    )
    led = _ledger(tmp_path)
    res = _propose(led)
    assert res.status is ProposeStatus.OK
    assert res.proposal is not None
    assert (
        res.proposal.monotonic_tightening_verdict
        is MonotonicTighteningVerdict.PASSED
    )


def test_validate_monotonic_tightening_public():
    """The validator function is public so tests + callers can
    inspect a proposal without persisting it."""
    p = AdaptationProposal(
        schema_version="1.0", proposal_id="p",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind="add_pattern",
        evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="t", proposed_at_epoch=1.0,
    )
    verdict, _ = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.PASSED


def test_validate_monotonic_tightening_rejects_loosen():
    p = AdaptationProposal(
        schema_version="1.0", proposal_id="p",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind="remove_pattern",  # not in allowlist
        evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="t", proposed_at_epoch=1.0,
    )
    verdict, detail = validate_monotonic_tightening(p)
    assert verdict is MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN
    assert "kind_not_in_tighten_allowlist" in detail


# ===========================================================================
# D — propose() paths
# ===========================================================================


def test_propose_ok_persists(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(led)
    assert res.status is ProposeStatus.OK
    assert res.proposal_id == "prop-1"
    assert (tmp_path / "ledger.jsonl").exists()


def test_propose_invalid_empty_proposal_id(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(led, proposal_id="")
    assert res.status is ProposeStatus.INVALID_PROPOSAL
    assert "proposal_id_empty" in res.detail


def test_propose_invalid_empty_kind(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(led, proposal_kind="")
    assert res.status is ProposeStatus.INVALID_PROPOSAL
    assert "proposal_kind_empty" in res.detail


def test_propose_invalid_evidence_type(tmp_path):
    led = _ledger(tmp_path)
    res = led.propose(
        proposal_id="p",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind="add_pattern",
        evidence={"not": "an AdaptationEvidence"},  # type: ignore[arg-type]
        current_state_hash="a", proposed_state_hash="b",
    )
    assert res.status is ProposeStatus.INVALID_PROPOSAL
    assert "evidence_not_AdaptationEvidence" in res.detail


def test_propose_invalid_surface_type(tmp_path):
    led = _ledger(tmp_path)
    res = led.propose(
        proposal_id="p",
        surface="semantic_guardian.patterns",  # type: ignore[arg-type]
        proposal_kind="add_pattern", evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
    )
    assert res.status is ProposeStatus.INVALID_PROPOSAL


def test_propose_duplicate_id(tmp_path):
    led = _ledger(tmp_path)
    r1 = _propose(led)
    assert r1.status is ProposeStatus.OK
    r2 = _propose(led)
    assert r2.status is ProposeStatus.DUPLICATE_PROPOSAL_ID
    assert r2.proposal is not None


def test_propose_evidence_summary_truncated(tmp_path):
    led = _ledger(tmp_path)
    big = "x" * (MAX_EVIDENCE_SUMMARY_CHARS + 100)
    res = _propose(led, evidence=AdaptationEvidence(
        window_days=7, observation_count=3, summary=big,
    ))
    assert res.status is ProposeStatus.OK
    assert res.proposal is not None
    assert len(res.proposal.evidence.summary) == MAX_EVIDENCE_SUMMARY_CHARS


def test_propose_source_event_ids_truncated(tmp_path):
    led = _ledger(tmp_path)
    many = tuple(f"ev-{i}" for i in range(MAX_SOURCE_EVENT_IDS_PER_PROPOSAL + 50))
    res = _propose(led, evidence=AdaptationEvidence(
        window_days=7, observation_count=3, source_event_ids=many,
    ))
    assert res.status is ProposeStatus.OK
    assert res.proposal is not None
    assert (
        len(res.proposal.evidence.source_event_ids)
        == MAX_SOURCE_EVENT_IDS_PER_PROPOSAL
    )


# ===========================================================================
# E — approve / reject paths
# ===========================================================================


def test_approve_ok_sets_applied_at(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    res = led.approve("prop-1", operator="alice")
    assert res.status is DecisionStatus.OK
    assert res.proposal is not None
    assert res.proposal.operator_decision is OperatorDecisionStatus.APPROVED
    assert res.proposal.operator_decision_by == "alice"
    assert res.proposal.applied_at is not None
    assert res.proposal.operator_decision_at is not None


def test_reject_ok_leaves_applied_at_none(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    res = led.reject("prop-1", operator="bob")
    assert res.status is DecisionStatus.OK
    assert res.proposal is not None
    assert res.proposal.operator_decision is OperatorDecisionStatus.REJECTED
    assert res.proposal.applied_at is None  # rejected → never applied
    assert res.proposal.operator_decision_by == "bob"


def test_approve_not_found(tmp_path):
    led = _ledger(tmp_path)
    res = led.approve("missing", operator="alice")
    assert res.status is DecisionStatus.NOT_FOUND


def test_reject_not_found(tmp_path):
    led = _ledger(tmp_path)
    res = led.reject("missing", operator="alice")
    assert res.status is DecisionStatus.NOT_FOUND


def test_approve_not_pending_after_already_approved(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    led.approve("prop-1", operator="alice")
    res = led.approve("prop-1", operator="bob")
    assert res.status is DecisionStatus.NOT_PENDING


def test_reject_not_pending_after_approved(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    led.approve("prop-1", operator="alice")
    res = led.reject("prop-1", operator="bob")
    assert res.status is DecisionStatus.NOT_PENDING


def test_approve_operator_required(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    res = led.approve("prop-1", operator="")
    assert res.status is DecisionStatus.OPERATOR_REQUIRED


def test_reject_operator_required(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    res = led.reject("prop-1", operator="")
    assert res.status is DecisionStatus.OPERATOR_REQUIRED


def test_operator_name_truncated_at_max(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    long = "alice" + "x" * (MAX_OPERATOR_NAME_CHARS + 50)
    res = led.approve("prop-1", operator=long)
    assert res.status is DecisionStatus.OK
    assert res.proposal is not None
    assert len(res.proposal.operator_decision_by or "") == MAX_OPERATOR_NAME_CHARS


# ===========================================================================
# F — Read queries
# ===========================================================================


def test_get_returns_latest_record(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    led.approve("prop-1", operator="alice")
    p = led.get("prop-1")
    assert p is not None
    assert p.operator_decision is OperatorDecisionStatus.APPROVED


def test_list_pending_excludes_terminals(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p1")
    _propose(led, proposal_id="p2")
    led.approve("p1", operator="alice")
    pending = led.list_pending()
    assert {p.proposal_id for p in pending} == {"p2"}


def test_history_includes_all_states_newest_first(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p1")
    _propose(led, proposal_id="p2")
    led.approve("p1", operator="alice")
    h = led.history(limit=10)
    assert len(h) >= 2  # may include the approval transition row


def test_history_filters_by_surface(tmp_path):
    led = _ledger(tmp_path)
    _propose(
        led, proposal_id="p-sg",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    )
    _propose(
        led, proposal_id="p-ig", proposal_kind="raise_floor",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
    )
    h = led.history(
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, limit=10,
    )
    assert all(
        p.surface is AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS
        for p in h
    )


def test_history_limit_zero_returns_empty(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    assert led.history(limit=0) == ()


# ===========================================================================
# G — Persistence + integrity
# ===========================================================================


def test_persistence_survives_new_instance(tmp_path):
    l1 = _ledger(tmp_path)
    _propose(l1)
    l2 = _ledger(tmp_path)
    assert l2.get("prop-1") is not None


def test_state_transitions_append_new_lines(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    after_propose = len(
        (tmp_path / "ledger.jsonl").read_text().splitlines()
    )
    led.approve("prop-1", operator="alice")
    after_approve = len(
        (tmp_path / "ledger.jsonl").read_text().splitlines()
    )
    assert after_approve == after_propose + 1


def test_record_sha256_round_trip(tmp_path):
    led = _ledger(tmp_path)
    res = _propose(led)
    assert res.proposal is not None
    assert res.proposal.record_sha256
    assert res.proposal.verify_integrity() is True


def test_tampered_record_skipped_on_read(tmp_path, caplog):
    led = _ledger(tmp_path)
    _propose(led)
    p = tmp_path / "ledger.jsonl"
    raw = p.read_text()
    tampered = raw.replace('"prop-1"', '"prop-TAMPERED"', 1)
    p.write_text(tampered)
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        out = led.get("prop-1")
    assert out is None
    assert any("sha256 mismatch" in r.message for r in caplog.records)


def test_malformed_json_line_skipped(tmp_path, caplog):
    led = _ledger(tmp_path)
    _propose(led)
    p = tmp_path / "ledger.jsonl"
    with p.open("a") as f:
        f.write("{not valid json\n")
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        out = led.get("prop-1")
    assert out is not None  # original still readable
    assert any("malformed json" in r.message for r in caplog.records)


def test_jsonl_one_record_per_line(tmp_path):
    led = _ledger(tmp_path)
    _propose(led)
    led.approve("prop-1", operator="alice")
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    for line in lines:
        d = json.loads(line)
        assert "proposal_id" in d
        assert "surface" in d
        assert "record_sha256" in d


# ===========================================================================
# H — Surface validator registry
# ===========================================================================


def test_register_and_get_surface_validator():
    def v(p):
        return (True, "ok")
    assert get_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    ) is None
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, v,
    )
    assert get_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    ) is v


def test_reset_surface_validators_clears_registry():
    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        lambda p: (True, "ok"),
    )
    reset_surface_validators()
    assert get_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    ) is None


def test_surface_validator_routing_per_surface(tmp_path):
    """Validators registered for surface A must NOT fire for surface B."""
    fired: list = []

    def vsg(p):
        fired.append("sg")
        return (True, "ok")

    register_surface_validator(
        AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS, vsg,
    )
    led = _ledger(tmp_path)
    # Surface IRON_GATE has no validator → default check only
    _propose(
        led, proposal_id="p-ig", proposal_kind="raise_floor",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
    )
    assert fired == []
    # Surface SG has validator → fires
    _propose(led, proposal_id="p-sg")
    assert fired == ["sg"]


# ===========================================================================
# I — Default singleton
# ===========================================================================


def test_get_default_ledger_returns_singleton(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH",
        str(tmp_path / "default.jsonl"),
    )
    reset_default_ledger()
    l1 = get_default_ledger()
    l2 = get_default_ledger()
    assert l1 is l2


def test_reset_default_ledger_creates_fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH",
        str(tmp_path / "default.jsonl"),
    )
    reset_default_ledger()
    l1 = get_default_ledger()
    reset_default_ledger()
    l2 = get_default_ledger()
    assert l1 is not l2


def test_ledger_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.jsonl"
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_PATH", str(custom))
    assert ledger_path() == custom


# ===========================================================================
# J — Round-trip serialization
# ===========================================================================


def test_evidence_round_trip():
    e = AdaptationEvidence(
        window_days=7, observation_count=12,
        source_event_ids=("a", "b", "c"), summary="hi",
    )
    e2 = AdaptationEvidence.from_dict(e.to_dict())
    assert e2 == e


def test_proposal_round_trip():
    p = AdaptationProposal(
        schema_version="1.0", proposal_id="p-rt",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
        proposal_kind="raise_floor",
        evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="2026-04-26T00:00:00+00:00",
        proposed_at_epoch=1000.0,
        operator_decision=OperatorDecisionStatus.APPROVED,
        operator_decision_at="2026-04-26T00:01:00+00:00",
        operator_decision_by="alice",
        applied_at="2026-04-26T00:01:00+00:00",
    )
    d = p.to_dict()
    p2 = AdaptationProposal.from_dict(d)
    assert p2.proposal_id == "p-rt"
    assert p2.surface is AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS
    assert p2.operator_decision is OperatorDecisionStatus.APPROVED
    assert p2.applied_at == "2026-04-26T00:01:00+00:00"


def test_proposal_to_dict_includes_rollback_via():
    """Pin: every proposal row carries `rollback_via:
    pass_b_manifest_amendment` so the audit trail is self-documenting
    about the cage rule (loosening goes through Pass B)."""
    p = AdaptationProposal(
        schema_version="1.0", proposal_id="p",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
        proposal_kind="add_pattern", evidence=_ev(),
        current_state_hash="a", proposed_state_hash="b",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="t", proposed_at_epoch=1.0,
    )
    d = p.to_dict()
    assert d["rollback_via"] == "pass_b_manifest_amendment"


# ===========================================================================
# K — Authority invariants (AST grep)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    assert not found_banned, (
        f"adaptation/ledger.py contains banned imports: {found_banned}"
    )


def test_module_imports_only_stdlib():
    """Pin Slice 1 substrate's tight import surface: stdlib ONLY.
    Slices 2-5 may add their own imports, but the substrate stays
    pristine (nothing of theirs imports back)."""
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
        "__future__",
        "enum", "hashlib", "json", "logging", "os", "threading",
        "time", "dataclasses", "datetime", "pathlib", "typing",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert any(
                mod == p or mod.startswith(p + ".")
                for p in stdlib_prefixes
            ), f"non-stdlib import {mod!r} in adaptation/ledger.py"
        elif isinstance(node, _ast.Import):
            for n in node.names:
                assert any(
                    n.name == p or n.name.startswith(p + ".")
                    for p in stdlib_prefixes
                ), f"non-stdlib import {n.name!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found, (
        f"adaptation/ledger.py contains forbidden tokens: {found}"
    )


def test_propose_never_writes_loosening_to_disk(tmp_path):
    """Cage check: the file MUST NOT exist after a would-loosen
    proposal — load-bearing for the §4.1 invariant."""
    led = _ledger(tmp_path)
    _propose(led, proposal_kind="totally_loose_kind")
    assert not (tmp_path / "ledger.jsonl").exists()
