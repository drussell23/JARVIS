"""Distributed Trace Lineage -- the cryptographic nametag across suspend/resume windows.

Live evidence (Window-2, bt-iso-1782944904): resumed ops got a BRAND-NEW causal_id
(`make_envelope` minted one because `_reinject` passed none), so the emit-probe logged
`MISSING ... ordered=False source=non-roadmap`, provenance classified `origin_class=
unknown`, `required_hops` returned None (UNVERIFIABLE), and the audit saw
`a1trace_hops: (none)` despite the chain genuinely spanning both windows.

Proves the span-continuation design:
  1. a1_trace can RESTORE an emit record from HMAC-verified checkpoint lineage --
     re-seeding the ledger BEFORE ingest (ordering genuinely holds in-process) and
     re-attesting the emit hop with an honest `lineage=resumed` tag.
  2. The checkpoint captures trace_lineage (origin goal id + origin source + emit
     evidence + resume_count) inside the HMAC-signed payload.
  3. A re-suspended RESUMED op carries the ORIGINAL lineage forward (window N+2
     still points at window 1's identity).
  4. The resume envelope kwargs reuse the ORIGINAL causal_id and carry the lineage.
  5. The REAL auditor recognizes the restored chain as one continuous winning goal.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import pathlib

import pytest

import backend.core.ouroboros.governance.a1_trace as a1t
import backend.core.ouroboros.governance.fsm_checkpoint as ckpt


class _Ctx:
    """Minimal OperationContext stand-in for capture_from_context."""

    def __init__(self, op_id, signal_source="roadmap", intake_evidence_json=""):
        self.op_id = op_id
        self.signal_source = signal_source
        self.target_files = ["a.py"]
        self.description = "A1 self-audit goal"
        self.intake_evidence_json = intake_evidence_json
        self.provider_route = "standard"


# --- 1. a1_trace restore: emit ledger re-seeded + honest lineage-tagged hop --


def test_restore_emit_record_makes_ingest_ordered(caplog):
    a1t.reset_emit_probe()
    with caplog.at_level(logging.WARNING):
        a1t.restore_emit_record("op-orig-1", source="roadmap", original_emit_wall=1751400000.0)
        a1t.probe_ingest_order("op-orig-1")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ordered=True" in logged
    assert "MISSING" not in logged


def test_restore_emits_lineage_tagged_hop_line(caplog):
    """The restored emit must appear as a REAL `[A1Trace] emit goal=` hop line
    (the auditor's census) tagged lineage=resumed -- attestation, not fabrication."""
    a1t.reset_emit_probe()
    with caplog.at_level(logging.WARNING):
        a1t.restore_emit_record("op-orig-2", source="roadmap", original_emit_wall=1751400000.0)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[A1Trace] emit goal=op-orig-2" in logged
    assert "lineage=resumed" in logged
    assert "source=roadmap" in logged


def test_get_emit_record_roundtrip():
    a1t.reset_emit_probe()
    a1t.emit_probe("op-orig-3", source="roadmap")
    rec = a1t.get_emit_record("op-orig-3")
    assert rec is not None
    ts, source = rec
    assert source == "roadmap" and ts > 0
    assert a1t.get_emit_record("op-never") is None


# --- 2. checkpoint captures lineage ------------------------------------------


def test_capture_mints_trace_lineage_with_emit_evidence():
    a1t.reset_emit_probe()
    a1t.emit_probe("op-orig-4", source="roadmap")
    cp = ckpt.capture_from_context(_Ctx("op-orig-4"), phase="GENERATE")
    lin = cp.trace_lineage
    assert lin["origin_goal_id"] == "op-orig-4"
    assert lin["origin_source"] == "roadmap"
    assert lin["emit_source"] == "roadmap"
    assert lin["emit_wall_ts"] > 0
    assert lin["resume_count"] == 1


def test_capture_without_emit_still_mints_lineage():
    a1t.reset_emit_probe()
    cp = ckpt.capture_from_context(_Ctx("op-sensor-1", signal_source="test_failure"),
                                   phase="GENERATE")
    lin = cp.trace_lineage
    assert lin["origin_goal_id"] == "op-sensor-1"
    assert lin["origin_source"] == "test_failure"
    assert lin["resume_count"] == 1
    assert "emit_source" not in lin or not lin.get("emit_source")


# --- 3. carry-forward across a second suspension ------------------------------


def test_resuspension_preserves_original_lineage():
    prior = {"origin_goal_id": "op-orig-5", "origin_source": "roadmap",
             "emit_source": "roadmap", "emit_wall_ts": 1751400000.0, "resume_count": 1}
    ev = json.dumps({"resume": True, "trace_lineage": prior})
    cp = ckpt.capture_from_context(
        _Ctx("op-orig-5", signal_source="fsm_resume", intake_evidence_json=ev),
        phase="GENERATE",
    )
    lin = cp.trace_lineage
    assert lin["origin_goal_id"] == "op-orig-5"
    assert lin["origin_source"] == "roadmap"          # NOT fsm_resume
    assert lin["emit_source"] == "roadmap"
    assert lin["resume_count"] == 2                    # incremented


# --- 4. lineage rides the HMAC-signed payload + resume envelope --------------


def test_lineage_survives_signed_roundtrip_and_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "lineage-secret")
    cp = ckpt.FSMCheckpoint(
        op_id="op-orig-6", phase="GENERATE",
        trace_lineage={"origin_goal_id": "op-orig-6", "origin_source": "roadmap",
                       "emit_source": "roadmap", "emit_wall_ts": 1751400000.0,
                       "resume_count": 1},
    )
    assert ckpt.write_checkpoint(cp)
    pend = ckpt.list_pending()
    assert len(pend) == 1
    assert pend[0].trace_lineage["origin_source"] == "roadmap"
    env = ckpt.build_resume_envelope(pend[0])
    assert env["trace_lineage"]["origin_goal_id"] == "op-orig-6"


def test_old_schema_checkpoint_still_hydrates(tmp_path, monkeypatch):
    """A v1 checkpoint written WITHOUT trace_lineage must still verify + hydrate
    (from_json tolerance) with an empty lineage -- no fail-closed regression."""
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "lineage-secret")
    cp = ckpt.FSMCheckpoint(op_id="op-old-1", phase="GENERATE")
    blob = json.loads(cp.to_json())
    blob.pop("trace_lineage", None)                    # simulate pre-lineage payload
    payload = json.dumps(blob, sort_keys=True)
    import os
    d = ckpt.checkpoint_dir(None)
    sig = ckpt._sign(payload, ckpt._checkpoint_key(None))
    with open(os.path.join(d, "op-old-1.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": 1, "payload": payload, "hmac": sig}))
    pend = ckpt.list_pending()
    assert any(c.op_id == "op-old-1" for c in pend)
    assert isinstance(pend[0].trace_lineage, dict)


# --- 4b. resume envelope kwargs reuse the original causal id ------------------


def test_resume_envelope_kwargs_reuse_original_causal_id():
    from backend.core.ouroboros.governance.intake import unified_intake_router as uir
    env = {"op_id": "op-orig-7", "description": "goal", "target_files": ["a.py"],
           "resume_phase": "GENERATE", "partial_completion": "def f(", "provider_route": "",
           "tool_history": [], "exploration_records": [], "intake_evidence_json": "",
           "trace_lineage": {"origin_goal_id": "op-orig-7", "origin_source": "roadmap",
                             "emit_source": "roadmap", "emit_wall_ts": 1.0,
                             "resume_count": 1}}
    kw = uir._resume_envelope_kwargs(env)
    assert kw["causal_id"] == "op-orig-7"
    assert kw["evidence"]["trace_lineage"]["origin_source"] == "roadmap"


def test_provenance_origin_resolves_from_lineage():
    from backend.core.ouroboros.governance.intake import unified_intake_router as uir

    class _Env:
        source = "fsm_resume"
        evidence = {"trace_lineage": {"origin_source": "roadmap"}}

    class _EnvNoLineage:
        source = "fsm_resume"
        evidence = {}

    assert uir._provenance_origin_for(_Env()) == "roadmap"
    assert uir._provenance_origin_for(_EnvNoLineage()) == "fsm_resume"


# --- 5. the REAL auditor credits the restored chain as one winning goal -------


def _load_auditor():
    import sys
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "a1_graduation_auditor.py"
    spec = importlib.util.spec_from_file_location("_aud_lineage_test", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_aud_lineage_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_auditor_credits_restored_chain_as_winning_goal():
    mod = _load_auditor()
    auditor = mod.A1GraduationAuditor(chaos_manifest_path=None)
    gid = "op-orig-8"
    lines = [
        f"[Provenance] op={gid} origin=roadmap origin_class=roadmap chain_ok=True",
        f"[A1Trace] emit goal={gid} source=roadmap lineage=resumed",
        f"[A1Trace] ingest goal={gid} router=attached",
        f"[A1Trace] dequeue goal={gid}",
        f"[A1Trace] submit goal={gid} target=GLS",
        f"[A1Trace] accept goal={gid} phase=CLASSIFY",
    ]
    for ln in lines:
        auditor.ingest_log_line(ln)
    assert auditor.trace.winning_goal() == gid
    assert auditor.trace.all_hops_in_order() is True
