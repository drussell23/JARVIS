"""Tests for the A1 GraduationAuditor causal lineage scoping (run #13 fix).

The Absolute Intervention-Lock must fire ONLY when a human gate
(``APPROVAL_REQUIRED`` / ``plan_pending`` / ``ask_human`` / ``CLARIFICATION``)
belongs to an op IN the chaos-repair op's causal subtree -- NOT when an
unrelated autonomous op (e.g. an OpportunityMiner exploration) correctly hits
``APPROVAL_REQUIRED`` (the Immutable Orange safety guard working as designed).

These tests prove:
  * an UNRELATED op hitting APPROVAL_REQUIRED -> NO GraduationFailedException
    (the run-#13 false-fail, now fixed) but it IS logged as observed;
  * a CHAOS-LINEAGE op (target_files include the manifest file) hitting
    APPROVAL_REQUIRED -> GraduationFailedException with the chaos op id;
  * a CHILD op descended from the chaos op halting -> still throws;
  * lineage-unknowable + a gate -> UNVERIFIABLE_LINEAGE (no fake-pass);
  * OFF flag (``JARVIS_A1_LINEAGE_SCOPING_ENABLED=false``) -> legacy global-lock.

Synthetic event/log streams only -- no network, no live soak.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the standalone script by path (it lives in scripts/, not a package).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "a1_graduation_auditor.py"
_spec = importlib.util.spec_from_file_location("a1_graduation_auditor", _SCRIPT)
assert _spec and _spec.loader
aud = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = aud
_spec.loader.exec_module(aud)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAOS_REL = "backend/core/ouroboros/governance/util/example_utils.py"
CHAOS_ABS = str(_REPO_ROOT / CHAOS_REL)


def _write_manifest(tmp_path, *, target_file=CHAOS_REL, target_file_abs=CHAOS_ABS):
    """Write a synthetic chaos manifest (mirrors chaos_injector_ast schema)."""
    manifest = {
        "schema_version": 1,
        "injector_version": "1.0.0",
        "target_file": target_file,
        "target_file_abs": target_file_abs,
        "function": "compute_score",
        "line": 12,
        "mutation_kind": "binop:Add->Sub",
        "test_node": "tests/util/test_example_utils.py::test_compute_score",
    }
    path = tmp_path / "chaos_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return str(path)


def _make_auditor(tmp_path, *, manifest=True, scoping=True, strict=False):
    flags = ["JARVIS_DECISION_TRACE_LEDGER_ENABLED"]
    manifest_path = _write_manifest(tmp_path) if manifest else None
    return aud.A1GraduationAuditor(
        flags=flags,
        strict=strict,
        chaos_manifest_path=manifest_path,
        lineage_scoping_enabled=scoping,
    )


# ===========================================================================
# 1. Chaos manifest -> chaos target file resolution
# ===========================================================================


def test_chaos_manifest_loads_target_file(tmp_path):
    a = _make_auditor(tmp_path)
    assert a.chaos_target_files  # non-empty
    # Both rel + abs forms recorded for robust correlation.
    assert any(CHAOS_REL in t for t in a.chaos_target_files)


def test_no_manifest_means_no_chaos_target(tmp_path):
    a = _make_auditor(tmp_path, manifest=False)
    assert not a.chaos_target_files


# ===========================================================================
# 2. The run-#13 false-fail: an UNRELATED op correctly gated must NOT throw
# ===========================================================================


def test_unrelated_approval_required_does_not_throw(tmp_path):
    a = _make_auditor(tmp_path)
    # An unrelated OpportunityMiner op declares a totally different target file.
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-miner", "phase": "CLASSIFY",
         "target_files": ["backend/core/ouroboros/consciousness/dream_engine.py"]},
    )
    # It correctly hits APPROVAL_REQUIRED (Immutable Orange working as designed).
    # Must NOT raise -- it is outside the chaos lineage.
    a.ingest_event(
        "risk_tier_escalated",
        {"op_id": "op-miner", "risk_tier": "APPROVAL_REQUIRED"},
    )
    assert a.intervention_tripped is False
    # ... but it IS logged as observed (transparency: the safety system working).
    assert any("op-miner" in obs for obs in a.observed_unrelated_gates)


# ===========================================================================
# 3. A chaos-lineage op halting MUST still throw
# ===========================================================================


def test_chaos_lineage_op_approval_required_throws(tmp_path):
    a = _make_auditor(tmp_path)
    # The chaos op: its target_files include the manifest file.
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-chaos", "phase": "CLASSIFY",
         "target_files": [CHAOS_REL]},
    )
    with pytest.raises(aud.GraduationFailedException) as ei:
        a.ingest_event(
            "risk_tier_escalated",
            {"op_id": "op-chaos", "risk_tier": "APPROVAL_REQUIRED"},
        )
    # The chaos op id is named in the failure.
    assert "op-chaos" in str(ei.value)
    assert "intervention_lock" in ei.value.failure_locus


def test_chaos_op_via_a1trace_goal_target_files(tmp_path):
    """The chaos op can be identified from an A1Trace breadcrumb that carries
    target_files (goal=op-id ... target_files=...)."""
    a = _make_auditor(tmp_path)
    a.ingest_log_line(
        f"[A1Trace] accept goal=op-chaos target_files={CHAOS_REL}"
    )
    with pytest.raises(aud.GraduationFailedException):
        a.ingest_log_line("risk_tier=APPROVAL_REQUIRED op=op-chaos")


# ===========================================================================
# 4. Lineage DESCENT: a child op of the chaos op halting still throws
# ===========================================================================


def test_descendant_of_chaos_op_throws(tmp_path):
    a = _make_auditor(tmp_path)
    # Chaos root.
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-chaos", "phase": "CLASSIFY", "target_files": [CHAOS_REL]},
    )
    # A child op decomposed from the chaos op (parent_op_id edge) -- different
    # target file, but it DESCENDS from the chaos op.
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-child", "phase": "GENERATE",
         "parent_op_id": "op-chaos",
         "target_files": ["backend/some/other/file.py"]},
    )
    with pytest.raises(aud.GraduationFailedException) as ei:
        a.ingest_event(
            "plan_pending", {"op_id": "op-child"},
        )
    assert "op-child" in str(ei.value) or "op-chaos" in str(ei.value)


def test_grandchild_of_chaos_op_throws(tmp_path):
    """Lineage descent is transitive (parent chain of depth > 1)."""
    a = _make_auditor(tmp_path)
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-chaos", "phase": "CLASSIFY", "target_files": [CHAOS_REL]},
    )
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-child", "parent_op_id": "op-chaos"},
    )
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-grandchild", "parent_goal_id": "op-child"},
    )
    with pytest.raises(aud.GraduationFailedException):
        a.ingest_log_line("[Venom] ask_human op=op-grandchild: clarify?")


# ===========================================================================
# 5. Fail-CLOSED: unknowable lineage + a gate -> UNVERIFIABLE_LINEAGE (no pass)
# ===========================================================================


def test_unknowable_lineage_gate_marks_unverifiable_and_fails(tmp_path):
    """A gate whose op cannot be correlated to ANY lineage (no manifest, or no
    op identity) must NOT silently pass. It records UNVERIFIABLE_LINEAGE and the
    final verdict is not proven."""
    a = _make_auditor(tmp_path, manifest=False)  # no chaos target known
    # A gate with an op id we cannot place in any lineage.
    a.ingest_event(
        "risk_tier_escalated", {"op_id": "op-mystery", "risk_tier": "APPROVAL_REQUIRED"},
    )
    # Did not throw (we can't prove it's the chaos op) ...
    assert a.intervention_tripped is False
    # ... but it is NOT a fake-pass: lineage is unverifiable -> verdict fails.
    v = a.verdict()
    assert v.proven is False
    assert "UNVERIFIABLE_LINEAGE" in v.failure_locus


def test_gate_with_no_op_id_is_unverifiable(tmp_path):
    """A human-gate marker with no extractable op id under an active manifest is
    unverifiable lineage -- fail-CLOSED, not a pass and not a false-throw."""
    a = _make_auditor(tmp_path)  # manifest present
    a.ingest_log_line("CLARIFICATION_REQUEST: something happened (no op id)")
    assert a.intervention_tripped is False
    v = a.verdict()
    assert v.proven is False
    assert "UNVERIFIABLE_LINEAGE" in v.failure_locus


# ===========================================================================
# 6. OFF flag -> legacy global-lock behavior (any gate throws)
# ===========================================================================


def test_scoping_off_restores_global_lock(tmp_path):
    a = _make_auditor(tmp_path, scoping=False)
    # With scoping OFF, even an unrelated op's APPROVAL_REQUIRED throws (legacy).
    with pytest.raises(aud.GraduationFailedException):
        a.ingest_event(
            "risk_tier_escalated",
            {"op_id": "op-miner", "risk_tier": "APPROVAL_REQUIRED"},
        )


def test_scoping_off_env_default(tmp_path, monkeypatch):
    """The flag default is read from JARVIS_A1_LINEAGE_SCOPING_ENABLED (default
    true). Setting it false restores the global lock."""
    monkeypatch.setenv("JARVIS_A1_LINEAGE_SCOPING_ENABLED", "false")
    assert aud.lineage_scoping_enabled_default() is False
    monkeypatch.setenv("JARVIS_A1_LINEAGE_SCOPING_ENABLED", "true")
    assert aud.lineage_scoping_enabled_default() is True
    monkeypatch.delenv("JARVIS_A1_LINEAGE_SCOPING_ENABLED", raising=False)
    assert aud.lineage_scoping_enabled_default() is True  # default on


# ===========================================================================
# 7. The terminal CRITICAL_ELEVATION merge remains permitted (regression)
# ===========================================================================


def test_terminal_merge_still_permitted_under_scoping(tmp_path):
    a = _make_auditor(tmp_path)
    a.ingest_event("cross_repo_elevation_pending", {"op_id": "op-chaos", "pr_id": "PR-1"})
    assert a.terminal_merge_reached is True
    assert a.intervention_tripped is False


# ===========================================================================
# 8. A chaos-lineage gate WINS over the unrelated path even when both present
# ===========================================================================


def test_chaos_gate_throws_even_after_unrelated_gate_logged(tmp_path):
    a = _make_auditor(tmp_path)
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-chaos", "phase": "CLASSIFY", "target_files": [CHAOS_REL]},
    )
    a.ingest_event(
        "fsm_phase_changed",
        {"op_id": "op-miner", "phase": "CLASSIFY",
         "target_files": ["unrelated/file.py"]},
    )
    # Unrelated gate first -> logged, no throw.
    a.ingest_event("risk_tier_escalated", {"op_id": "op-miner", "risk_tier": "APPROVAL_REQUIRED"})
    assert a.intervention_tripped is False
    # Chaos-lineage gate -> throws.
    with pytest.raises(aud.GraduationFailedException):
        a.ingest_event("risk_tier_escalated", {"op_id": "op-chaos", "risk_tier": "APPROVAL_REQUIRED"})
