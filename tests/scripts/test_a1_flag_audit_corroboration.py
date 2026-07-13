"""Slice 8 — corroborated-rejects rule for the A1 flag audit.

Run #18 false-red: a CommProtocol INTENT payload carrying
risk_tier='APPROVAL_REQUIRED' (stamped by the Slice-6 attribution gate)
matched semantic_guardian's bare 'APPROVAL_REQUIRED' rejected-marker and
poisoned the family to REJECTED → twelve_flag_audit_passed=false. A REJECT
must be corroborated by the family's own voice on the same line."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "a1_graduation_auditor", _REPO / "scripts" / "a1_graduation_auditor.py",
)
assert _spec is not None and _spec.loader is not None
auditor_mod = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = auditor_mod
_spec.loader.exec_module(auditor_mod)

# The REAL Run-18 poisoning line (truncated as the auditor stores it):
_RUN18_INTENT_LINE = (
    "2026-07-12T00:28:42 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] INTENT op=op-019f5539-f107-74e0 seq=1 payload="
    "{'goal': 'x', 'risk_tier': 'APPROVAL_REQUIRED'}"
)
_GENUINE_GUARD_REJECT_LINE = (
    "2026-07-12T00:29:01 [Ouroboros.Orchestrator] INFO [SemanticGuard] "
    "op=op-x findings=1 pattern=removed_import_still_referenced "
    "risk=APPROVAL_REQUIRED"
)


def _fresh_auditor_families(flags):
    # Same construction pattern as tests/scripts/test_a1_provenance_auditor.py.
    # family_for_flag maps each flag's family needle (e.g. SEMANTIC_GUARDIAN,
    # IRON_GATE) to the signal family under test.
    return auditor_mod.A1GraduationAuditor(
        flags=list(flags),
        strict=True,
        chaos_manifest_path=None,
        lineage_scoping_enabled=False,
    )


def _fresh_auditor():
    # One semantic_guardian-family flag is enough for the Slice 8 tests.
    return _fresh_auditor_families(["JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED"])


def test_intent_payload_line_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )
    assert any(
        "semantic_guardian" in x for x in a.uncorroborated_reject_lines
    )


def test_genuine_guard_rejection_still_poisons(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_GENUINE_GUARD_REJECT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )


def test_kill_switch_restores_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", "false")
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )


# ---------------------------------------------------------------------------
# Slice 9 Task 5 — chaos-lineage reject scoping (Run #19 false-red)
#
# The unrelated-reject lane above only activates for resumed_ops audits.
# Single-session audits (no resumed ops) still globally correlated a REJECT
# even when a chaos manifest AND an op-lineage graph are available -- a
# GENUINE, self-corroborated rejection on a background op with nothing to do
# with the audited chaos repair poisoned the flag anyway.
# ---------------------------------------------------------------------------

_GENUINE_IRON_GATE_REJECT_UNRELATED_OP = (
    "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
    "Iron Gate — exploration_insufficient: 0/2 (attempt=1) "
    "op=op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
)


def _auditor_with_chaos_lineage(chaos_op: str = "op-chaos-1"):
    a = _fresh_auditor_families(["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"])
    a.lineage = auditor_mod.OpLineageGraph(
        ["backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"],
    )
    # Register the chaos op so the lineage is non-empty and scoping is live.
    # Real node-ingestion API is OpLineageGraph.observe_op(op_id, target_files=...).
    a.lineage.observe_op(
        chaos_op,
        target_files=[
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
        ],
    )
    return a


def test_unrelated_op_reject_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    assert a.lineage.in_chaos_lineage("op-chaos-1") is True
    assert (
        a.lineage.in_chaos_lineage("op-019f58a7-f623-756b-bf73-0b4309aaaaaa")
        is False
    )
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_chaos_lineage_op_reject_still_poisons(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage(
        chaos_op="op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
    )
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_lineage_scoping_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", "false")
    a = _auditor_with_chaos_lineage()
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_resumed_ops_path_unchanged_by_lineage_flag(monkeypatch):
    """Resumed-ops scoping (Slice-6-adjacent, pre-existing) must not be
    touched by the new chaos-lineage lane: with resumed_ops set, an op
    outside that set is unrelated regardless of JARVIS_A1_AUDIT_
    LINEAGE_SCOPED_REJECTS, and chaos-lineage membership is never consulted
    (the resumed-ops branch is mutually exclusive with the chaos-lineage
    branch -- see the ``not self.resumed_ops`` guard)."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage(
        chaos_op="op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
    )
    # Even though the rejecting op IS in the chaos lineage, a resumed-ops
    # scope that excludes it must still mark it unrelated (resumed_ops wins).
    a.resumed_ops = {"op-some-other-resumed-op": {"source": "test"}}
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_unattributable_reject_stays_globally_correlated(monkeypatch):
    """A reject line with no parseable op id fails CLOSED: it still poisons,
    even with chaos-lineage scoping enabled and a live chaos manifest."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    no_op_line = (
        "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
        "Iron Gate — exploration_insufficient: 0/2 (attempt=1)"
    )
    a._correlate_flag_signal(no_op_line)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
