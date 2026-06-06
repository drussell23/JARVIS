"""Slice 101 Phase 3 — cognitive subscribers + the belief-revision learning loop.

Proves the Adaptive Intelligence Invariant end-to-end against a REAL tmp belief
ledger: a post_failure event records a falsifying belief; the avoidance digest
surfaces the failing files; and the StrategicDirection injection gate is inert
unless the cognitive bus is on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.core.ouroboros.governance import cognitive_subscribers as CS
from backend.core.ouroboros.governance.belief_revision_ledger import (
    EvidenceKind,
    record_claim,
    record_evidence,
)


def _belief_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_PERSIST_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
        str(tmp_path / "belief_revision_ledger.jsonl"),
    )


def _post_failure_event(*, files, op_id="op-1", reason="validate_failed"):
    return SimpleNamespace(
        payload={
            "lifecycle_kind": CS.LIFECYCLE_POST_FAILURE,
            "op_id": op_id,
            "state": "failed",
            "reason": reason,
            "target_files": list(files),
        }
    )


# --- subscriber records a falsifying belief ---------------------------------

def test_post_failure_records_falsifying_belief(monkeypatch, tmp_path):
    _belief_env(monkeypatch, tmp_path)
    asyncio.run(CS.belief_revision_on_failure(_post_failure_event(files=["bar.py"])))
    digest = CS.recent_avoidance_digest()
    assert "bar.py" in digest
    assert "Recently-Failing Areas" in digest


def test_non_failure_event_records_nothing(monkeypatch, tmp_path):
    _belief_env(monkeypatch, tmp_path)
    evt = SimpleNamespace(
        payload={"lifecycle_kind": "post_apply",
                 "op_id": "op-2", "target_files": ["baz.py"]}
    )
    asyncio.run(CS.belief_revision_on_failure(evt))
    assert CS.recent_avoidance_digest() == ""


def test_belief_master_off_is_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_BELIEF_REVISION_ENABLED", raising=False)
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH",
        str(tmp_path / "belief_revision_ledger.jsonl"),
    )
    asyncio.run(CS.belief_revision_on_failure(_post_failure_event(files=["x.py"])))
    # master off → digest is empty regardless
    assert CS.recent_avoidance_digest() == ""


# --- avoidance digest ranks recurrence --------------------------------------

def test_digest_ranks_by_recurrence(monkeypatch, tmp_path):
    _belief_env(monkeypatch, tmp_path)
    # hot.py fails twice, cold.py once → hot.py ranked first
    for _ in range(2):
        c = record_claim("generation in [hot.py] succeeds", "generation/failure",
                         target_files=["hot.py"])
        record_evidence(c.claim_id, EvidenceKind.FALSIFYING)
    c2 = record_claim("generation in [cold.py] succeeds", "generation/failure",
                      target_files=["cold.py"])
    record_evidence(c2.claim_id, EvidenceKind.FALSIFYING)

    digest = CS.recent_avoidance_digest()
    assert "hot.py" in digest and "cold.py" in digest
    assert digest.index("hot.py") < digest.index("cold.py")


def test_digest_empty_when_no_failures(monkeypatch, tmp_path):
    _belief_env(monkeypatch, tmp_path)
    assert CS.recent_avoidance_digest() == ""


# --- StrategicDirection injection gate --------------------------------------

def test_strategic_injection_inert_when_bus_off(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    _belief_env(monkeypatch, tmp_path)
    # Record a real failing belief so the digest WOULD be non-empty...
    c = record_claim("generation in [z.py] succeeds", "generation/failure",
                     target_files=["z.py"])
    record_evidence(c.claim_id, EvidenceKind.FALSIFYING)
    # ...but with the cognitive bus master OFF, the injection is byte-identical
    # to legacy (empty), even though the belief substrate itself is on.
    monkeypatch.delenv("JARVIS_COGNITIVE_BUS_ENABLED", raising=False)
    svc = object.__new__(StrategicDirectionService)  # bypass heavy __init__
    assert svc._render_avoidance_section() == ""


def test_strategic_injection_fires_when_bus_on(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    _belief_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_COGNITIVE_BUS_ENABLED", "1")
    c = record_claim("generation in [z.py] succeeds", "generation/failure",
                     target_files=["z.py"])
    record_evidence(c.claim_id, EvidenceKind.FALSIFYING)
    svc = object.__new__(StrategicDirectionService)
    block = svc._render_avoidance_section()
    assert "z.py" in block
    assert "Recently-Failing Areas" in block


def test_build_default_subscribers_shape():
    subs = CS.build_default_subscribers()
    assert len(subs) >= 1
    labels = {s.label for s in subs}
    assert "belief_revision_failure" in labels
