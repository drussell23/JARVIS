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


def _fresh_auditor():
    # Same construction pattern as tests/scripts/test_a1_provenance_auditor.py.
    # One semantic_guardian-family flag is enough: family_for_flag maps the
    # SEMANTIC_GUARDIAN needle in the name to the family.
    return auditor_mod.A1GraduationAuditor(
        flags=["JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED"],
        strict=True,
        chaos_manifest_path=None,
        lineage_scoping_enabled=False,
    )


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
