"""Slice 101 Phase 5 — pre-APPLY proof gate.

counterfactual_rehearsal composes into the strictest-wins risk-tier floor
(the "structural failure-memory checked before a patch lands" gate);
proof_carrier_transport aggregates the pre-APPLY signals into one artifact.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.governance import risk_tier_floor as RTF
from backend.core.ouroboros.governance import counterfactual_rehearsal_mode as CRM
from backend.core.ouroboros.governance.counterfactual_rehearsal_mode import (
    RehearsalVerdict,
)


def _fake_report(verdict):
    return SimpleNamespace(verdict=verdict)


# === Part A: rehearsal → risk-tier floor mapping ============================

def test_concern_maps_to_notify_apply(monkeypatch):
    monkeypatch.setattr(
        CRM, "evaluate_rehearsal",
        lambda tf, **k: _fake_report(RehearsalVerdict.CONCERN_RAISED),
    )
    assert RTF._rehearsal_floor(["foo.py"]) == "notify_apply"


def test_escalate_maps_to_approval_required(monkeypatch):
    monkeypatch.setattr(
        CRM, "evaluate_rehearsal",
        lambda tf, **k: _fake_report(RehearsalVerdict.ESCALATE),
    )
    assert RTF._rehearsal_floor(["foo.py"]) == "approval_required"


def test_clean_and_disabled_map_to_no_floor(monkeypatch):
    for v in (RehearsalVerdict.CLEAN, RehearsalVerdict.DISABLED):
        monkeypatch.setattr(
            CRM, "evaluate_rehearsal", lambda tf, **k: _fake_report(v),
        )
        assert RTF._rehearsal_floor(["foo.py"]) is None


def test_empty_target_files_is_no_floor():
    assert RTF._rehearsal_floor([]) is None
    assert RTF._rehearsal_floor(None) is None


def test_rehearsal_floor_never_raises(monkeypatch):
    def _boom(tf, **k):
        raise RuntimeError("rehearsal exploded")
    monkeypatch.setattr(CRM, "evaluate_rehearsal", _boom)
    # The floor must stay robust even if the substrate throws.
    assert RTF._rehearsal_floor(["foo.py"]) is None


# === Part A: master-OFF byte-identical inertness ===========================

def test_master_off_floor_is_inert(monkeypatch):
    # Real substrate, master off → DISABLED verdict → no floor → legacy.
    monkeypatch.delenv("JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED", raising=False)
    assert RTF._rehearsal_floor(["foo.py"]) is None


# === Part A: composition into the strictest-wins ladder =====================

def test_rehearsal_composes_into_recommended_floor(monkeypatch):
    # Clear other floor sources so rehearsal is the only candidate.
    for env in (
        "JARVIS_MIN_RISK_TIER", "JARVIS_PARANOIA_MODE",
        "JARVIS_AUTO_APPLY_QUIET_HOURS",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(
        CRM, "evaluate_rehearsal",
        lambda tf, **k: _fake_report(RehearsalVerdict.CONCERN_RAISED),
    )
    floor = RTF.recommended_floor(target_files=["foo.py"])
    assert floor == "notify_apply"


def test_strictest_wins_when_rehearsal_and_paranoia(monkeypatch):
    # paranoia → notify_apply, rehearsal ESCALATE → approval_required; strictest wins.
    monkeypatch.setenv("JARVIS_PARANOIA_MODE", "1")
    monkeypatch.setattr(
        CRM, "evaluate_rehearsal",
        lambda tf, **k: _fake_report(RehearsalVerdict.ESCALATE),
    )
    floor = RTF.recommended_floor(target_files=["foo.py"])
    assert floor == "approval_required"


# === Part B: proof_carrier_transport aggregation ============================

def test_proof_carrier_inert_when_master_off(monkeypatch):
    from backend.core.ouroboros.governance.proof_carrier_transport import (
        ProofVerdict,
        build_proof_carrier,
    )
    monkeypatch.delenv("JARVIS_PROOF_CARRIER_ENABLED", raising=False)
    carrier = build_proof_carrier("op-1", ["foo.py"])
    assert carrier.verdict == ProofVerdict.DISABLED


def test_proof_carrier_blocks_on_rehearsal_escalate(monkeypatch):
    from backend.core.ouroboros.governance.proof_carrier_transport import (
        ProofVerdict,
        build_proof_carrier,
    )
    monkeypatch.setenv("JARVIS_PROOF_CARRIER_ENABLED", "1")
    carrier = build_proof_carrier(
        "op-2", ["foo.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="none",
        rehearsal_count_override=3,
        rehearsal_verdict_override="escalate",
    )
    assert carrier.verdict == ProofVerdict.BLOCK


def test_proof_carrier_warns_on_rehearsal_concern(monkeypatch):
    from backend.core.ouroboros.governance.proof_carrier_transport import (
        ProofVerdict,
        build_proof_carrier,
    )
    monkeypatch.setenv("JARVIS_PROOF_CARRIER_ENABLED", "1")
    carrier = build_proof_carrier(
        "op-3", ["foo.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="none",
        rehearsal_count_override=2,
        rehearsal_verdict_override="concern_raised",
    )
    assert carrier.verdict == ProofVerdict.WARN


def test_proof_carrier_clean_when_no_signals(monkeypatch):
    from backend.core.ouroboros.governance.proof_carrier_transport import (
        ProofVerdict,
        build_proof_carrier,
    )
    monkeypatch.setenv("JARVIS_PROOF_CARRIER_ENABLED", "1")
    carrier = build_proof_carrier(
        "op-4", ["foo.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="none",
        rehearsal_count_override=0,
        rehearsal_verdict_override="clean",
    )
    assert carrier.verdict == ProofVerdict.CLEAN
