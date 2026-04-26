"""P1 — Curiosity Engine v2 — graduation pin suite.

Mirrors P0 / P0.5 graduation pin patterns (PRD §11 Layer 4 prep). Pins
the post-graduation contract for the entire P1 stack — clusterer +
engine + BacklogSensor consumer + REPL operator-review surface — so:

  * Unintentional regression → fix the change.
  * Intentional rollback → update pins per embedded instructions.

Pin coverage (sections A-J):
  (A) Master flags — DUAL flip pinned (engine + sensor, both default true)
  (B) Hot-revert paths — explicit false on each flag returns byte-identical
      pre-graduation behavior
  (C) Authority invariants — banned-import grep across all 5 P1 modules
  (D) Schema invariants — ProposalDraft frozen + schema_version pinned;
      DecisionRecord frozen; IntentEnvelope source allowlist contains
      "auto_proposed"; module-level constants pinned
  (E) Wiring source-grep pins — engine writes ledger, sensor reads
      ledger, REPL writes decisions + appends to backlog.json
  (F) Bounded-by-construction safety — cap defaults pinned, runaway
      adversarial inputs cannot exceed bounds
  (G) Backwards-compat — pre-Slice-3 manual backlog.json entries still
      flow normally; pre-Slice-2 callers without arc_context unchanged
  (H) Posture veto — engine refuses HARDEN/MAINTAIN postures
  (I) End-to-end integration — cluster → engine.evaluate → ledger →
      sensor → envelope → REPL approve → backlog.json all in one test
  (J) Operator-review-tier invariant — every auto-proposed envelope
      carries requires_human_ack=True
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.backlog_auto_proposed_repl import (
    dispatch_backlog_auto_proposed_command as REPL,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
)
from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    _MAX_LEDGER_ENTRIES_TO_SCAN,
    _MAX_PROPOSALS_PER_SCAN,
    _auto_proposed_enabled,
)
from backend.core.ouroboros.governance.postmortem_clusterer import (
    DEFAULT_MAX_CLUSTERS,
    DEFAULT_MIN_CLUSTER_SIZE,
    cluster_postmortems,
)
from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecord
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.self_goal_formation import (
    DEFAULT_COST_CAP_USD,
    DEFAULT_PER_SESSION_CAP,
    PROPOSAL_SCHEMA_VERSION,
    ProposalDraft,
    SelfGoalFormationEngine,
    cost_cap_usd,
    is_enabled as engine_enabled,
    per_session_cap,
    reset_default_engine,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_SELF_GOAL_FORMATION_ENABLED",
        "JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED",
        "JARVIS_SELF_GOAL_PER_SESSION_CAP",
        "JARVIS_SELF_GOAL_COST_CAP_USD",
        "JARVIS_SELF_GOAL_MIN_CLUSTER_SIZE",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_default_engine()
    yield
    reset_default_engine()


def _record(op_id: str, **kw) -> PostmortemRecord:
    base = dict(
        session_id="s1",
        root_cause="all_providers_exhausted:fallback_failed",
        failed_phase="GENERATE",
        next_safe_action="retry_with_smaller_seed",
        target_files=("a.py",),
        timestamp_iso="2026-04-26T10:00:00",
        timestamp_unix=1_700_000_000.0,
    )
    base.update(kw)
    return PostmortemRecord(op_id=op_id, **base)


def _three_records(**kw) -> List[PostmortemRecord]:
    return [
        _record(op_id=f"op{i}", timestamp_unix=1_700_000_000.0 + i * 3600.0, **kw)
        for i in range(3)
    ]


def _stub_model(description="Investigate recurring failure", cost=0.05):
    return lambda p, m: (
        json.dumps({"description": description, "rationale": "5 ops failed."}),
        cost,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# A — Master flags (DUAL flip pinned)
# ===========================================================================


def test_engine_master_flag_default_true_post_graduation(monkeypatch):
    """JARVIS_SELF_GOAL_FORMATION_ENABLED defaults True post-graduation.

    Hot-revert: ``export JARVIS_SELF_GOAL_FORMATION_ENABLED=false``.
    Rollback: rename to test_master_flag_default_false + flip assertion +
    update source-grep pin in (E)."""
    monkeypatch.delenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", raising=False)
    assert engine_enabled() is True


def test_sensor_master_flag_default_true_post_graduation(monkeypatch):
    """JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED defaults True post-graduation."""
    monkeypatch.delenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", raising=False)
    assert _auto_proposed_enabled() is True


def test_pin_engine_env_reader_default_true_literal():
    """Source-grep pin: the engine's is_enabled() literal-defaults to true."""
    src = _read("backend/core/ouroboros/governance/self_goal_formation.py")
    # Note: engine uses os.environ.get(name, "true") + truthy check, not _env_bool
    assert (
        '"JARVIS_SELF_GOAL_FORMATION_ENABLED", "true"' in src
    ), (
        "Engine master flag default literal moved or changed. If P1 was "
        "rolled back, update both the source AND this pin."
    )


def test_pin_sensor_env_reader_default_true_literal():
    """Source-grep pin: the sensor's _auto_proposed_enabled() default."""
    src = _read("backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py")
    assert (
        '"JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "true"' in src
    ), (
        "Sensor master flag default literal moved or changed. If P1 was "
        "rolled back, update both the source AND this pin."
    )


# ===========================================================================
# B — Hot-revert paths
# ===========================================================================


def test_engine_hot_revert_explicit_false(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "false")
    eng = SelfGoalFormationEngine(
        project_root=tmp_path, ledger_path=tmp_path / "ledger.jsonl",
    )
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is None  # byte-for-byte pre-graduation behavior


def test_sensor_hot_revert_explicit_false_silences_proposals(
    monkeypatch, tmp_path,
):
    """Hot-revert flag → BacklogSensor's proposals branch silenced."""
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "false")
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    ledger.write_text(json.dumps({
        "schema_version": "self_goal_formation.1",
        "signature_hash": "abc123",
        "cluster_member_count": 5,
        "target_files": ["a.py"],
        "description": "test",
        "rationale": "r",
        "posture_at_proposal": "EXPLORE",
        "auto_proposed": True,
    }) + "\n")
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json",
        repo_root=tmp_path,
        router=router,
        proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    assert envs == []


# ===========================================================================
# C — Authority invariants (5 P1 modules)
# ===========================================================================


_BANNED_AUTHORITY_IMPORTS = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]

_P1_MODULES = [
    "backend/core/ouroboros/governance/postmortem_clusterer.py",
    "backend/core/ouroboros/governance/self_goal_formation.py",
    "backend/core/ouroboros/governance/backlog_auto_proposed_repl.py",
    # BacklogSensor stays read-only too — pinned across the whole module.
    "backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py",
]


@pytest.mark.parametrize("relpath", _P1_MODULES)
def test_p1_module_no_authority_imports(relpath):
    src = _read(relpath)
    for imp in _BANNED_AUTHORITY_IMPORTS:
        assert imp not in src, f"banned import in {relpath}: {imp}"


def test_engine_no_provider_imports():
    """Per PRD §9 P1: engine never imports providers — model_caller is
    dependency-injected. Pinned separately because providers != authority."""
    src = _read("backend/core/ouroboros/governance/self_goal_formation.py")
    assert "from backend.core.ouroboros.governance.providers" not in src
    assert "from backend.core.ouroboros.governance.doubleword_provider" not in src


def test_repl_no_router_or_change_engine_coupling():
    """REPL is read-mostly + append-only — no FSM mutation surface."""
    src = _read("backend/core/ouroboros/governance/backlog_auto_proposed_repl.py")
    assert "router.ingest" not in src
    assert "ChangeEngine" not in src


# ===========================================================================
# D — Schema invariants
# ===========================================================================


def test_proposal_schema_version_frozen():
    assert PROPOSAL_SCHEMA_VERSION == "self_goal_formation.1"


def test_proposal_draft_is_frozen():
    """ProposalDraft must stay hashable / immutable for Slice 3 envelope
    carry-through to work safely."""
    from backend.core.ouroboros.governance.self_goal_formation import (
        ProposalDraft as PD,
    )
    pd = PD(
        schema_version="self_goal_formation.1",
        signature_hash="x",
        cluster_member_count=1,
        target_files=(),
        dominant_next_safe_action="",
        description="d",
        rationale="r",
        posture_at_proposal="EXPLORE",
        cost_usd_spent=0.0,
        timestamp_unix=0.0,
    )
    with pytest.raises(Exception):
        pd.description = "mutated"  # type: ignore[misc]


def test_decision_record_is_frozen():
    from backend.core.ouroboros.governance.backlog_auto_proposed_repl import (
        DecisionRecord,
    )
    d = DecisionRecord(
        signature_hash="x", decision="approve", reason="r", timestamp_unix=0.0,
    )
    with pytest.raises(Exception):
        d.decision = "reject"  # type: ignore[misc]


def test_auto_proposed_in_envelope_source_allowlist():
    assert "auto_proposed" in _VALID_SOURCES


def test_default_caps_pinned():
    assert DEFAULT_PER_SESSION_CAP == 1
    assert DEFAULT_COST_CAP_USD == 0.10
    assert DEFAULT_MIN_CLUSTER_SIZE == 3
    assert DEFAULT_MAX_CLUSTERS == 10
    assert _MAX_PROPOSALS_PER_SCAN == 5
    assert _MAX_LEDGER_ENTRIES_TO_SCAN == 200


# ===========================================================================
# E — Wiring source-grep pins
# ===========================================================================


def test_pin_engine_persists_to_jsonl_ledger():
    src = _read("backend/core/ouroboros/governance/self_goal_formation.py")
    assert "self_goal_formation_proposals.jsonl" in src
    assert "def _persist" in src


def test_pin_sensor_reads_proposals_ledger():
    src = _read("backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py")
    assert "_scan_proposals_ledger" in src
    assert 'source="auto_proposed"' in src
    assert "requires_human_ack=True" in src


def test_pin_repl_writes_decisions_and_backlog():
    src = _read("backend/core/ouroboros/governance/backlog_auto_proposed_repl.py")
    assert "self_goal_formation_decisions.jsonl" in src
    assert "_append_to_backlog_json" in src
    assert "approved_signature_hash" in src


# ===========================================================================
# F — Bounded-by-construction safety
# ===========================================================================


def test_per_session_cap_default_pinned():
    assert per_session_cap() == 1


def test_cost_cap_default_pinned():
    assert cost_cap_usd() == 0.10


def test_engine_runaway_input_bounded_by_per_session_cap(
    monkeypatch, tmp_path,
):
    """Adversarial scenario: 100 distinct cluster signatures + master ON
    + cap=1 → engine emits exactly 1 proposal, no more."""
    eng = SelfGoalFormationEngine(
        project_root=tmp_path, ledger_path=tmp_path / "ledger.jsonl",
    )
    # Each call has distinct phase → distinct signature
    phases = [f"PHASE-{i}" for i in range(100)]
    emitted = 0
    for phase in phases:
        out = eng.evaluate(
            postmortems=_three_records(failed_phase=phase),
            posture=Posture.EXPLORE,
            model_caller=_stub_model(),
        )
        if out is not None:
            emitted += 1
    assert emitted == 1  # cap holds


def test_sensor_runaway_ledger_bounded_by_max_per_scan(
    monkeypatch, tmp_path,
):
    """Adversarial scenario: 50 proposals in ledger → sensor emits at
    most _MAX_PROPOSALS_PER_SCAN per single scan."""
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    proposals = [
        {
            "schema_version": "self_goal_formation.1",
            "signature_hash": f"sig-{i:04d}",
            "cluster_member_count": 5,
            "target_files": ["a.py"],
            "description": "d",
            "rationale": "r",
            "posture_at_proposal": "EXPLORE",
            "auto_proposed": True,
        }
        for i in range(50)
    ]
    ledger.write_text("\n".join(json.dumps(p) for p in proposals) + "\n")
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json",
        repo_root=tmp_path,
        router=router,
        proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    assert len(envs) == _MAX_PROPOSALS_PER_SCAN


# ===========================================================================
# G — Backwards-compat
# ===========================================================================


def test_manual_backlog_json_still_emits_when_proposals_branch_active(
    monkeypatch, tmp_path,
):
    """Pre-Slice-3 manual entries must still flow normally even with
    P1 graduated."""
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps([{
        "task_id": "manual-1",
        "description": "d",
        "target_files": ["m.py"],
        "priority": 3,
        "repo": "jarvis",
        "status": "pending",
    }]))
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=backlog, repo_root=tmp_path, router=router,
        proposals_ledger_path=tmp_path / "nonexistent.jsonl",
    )
    envs = _run(s.scan_once())
    assert any(e.source == "backlog" for e in envs)


# ===========================================================================
# H — Posture veto
# ===========================================================================


@pytest.mark.parametrize("posture", [Posture.HARDEN, Posture.MAINTAIN])
def test_posture_veto_blocks_proposal(tmp_path, posture):
    """Engine must veto HARDEN + MAINTAIN postures regardless of master
    flag state. PRD §9 P1 DirectionInferrer veto."""
    eng = SelfGoalFormationEngine(
        project_root=tmp_path, ledger_path=tmp_path / "ledger.jsonl",
    )
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=posture,
        model_caller=_stub_model(),
    )
    assert out is None


# ===========================================================================
# I — End-to-end integration (the load-bearing pin)
# ===========================================================================


def test_end_to_end_cluster_engine_sensor_repl_backlog(tmp_path):
    """The whole P1 chain in one test:

      1. Real PostmortemRecords → cluster_postmortems → ProposalCandidate
      2. SelfGoalFormationEngine.evaluate → ProposalDraft → JSONL ledger
      3. BacklogSensor reads ledger → IntentEnvelope (auto_proposed)
      4. REPL approve → backlog.json appended with full audit fields

    If this test breaks, the entire P1 pipeline is broken — the rest of
    the pin suite tells you exactly which slice."""
    # Step 1 — cluster math
    recs = _three_records()
    clusters = cluster_postmortems(recs)
    assert len(clusters) == 1
    sig_hash = clusters[0].signature.signature_hash()

    # Step 2 — engine evaluate + persist
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    eng = SelfGoalFormationEngine(
        project_root=tmp_path, ledger_path=ledger,
    )
    draft = eng.evaluate(
        postmortems=recs,
        posture=Posture.EXPLORE,
        model_caller=_stub_model(description="Investigate end-to-end"),
    )
    assert draft is not None
    assert draft.signature_hash == sig_hash
    assert ledger.exists()

    # Step 3 — sensor reads ledger + emits envelope
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json",
        repo_root=tmp_path,
        router=router,
        proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    assert len(envs) == 1
    e = envs[0]
    assert e.source == "auto_proposed"
    assert e.requires_human_ack is True
    assert e.evidence["signature_hash"] == sig_hash

    # Step 4 — REPL approve → backlog.json appended
    r = REPL(
        f"/backlog auto-proposed approve {sig_hash} --reason looks good",
        project_root=tmp_path,
    )
    assert r.ok is True

    backlog = tmp_path / ".jarvis" / "backlog.json"
    entries = json.loads(backlog.read_text())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["task_id"] == f"auto-proposed:{sig_hash}"
    assert entry["auto_proposed"] is True
    assert entry["approved_signature_hash"] == sig_hash
    assert entry["approval_reason"] == "looks good"
    assert entry["description"] == "Investigate end-to-end"


# ===========================================================================
# J — Operator-review-tier invariant
# ===========================================================================


def test_every_auto_proposed_envelope_requires_human_ack(tmp_path):
    """Pin: every envelope from the auto_proposed branch carries
    requires_human_ack=True. PRD §9 P1 operator-review tier."""
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    proposals = [
        {
            "schema_version": "self_goal_formation.1",
            "signature_hash": f"sig-{i:02d}",
            "cluster_member_count": 5,
            "target_files": ["a.py"],
            "description": "d",
            "rationale": "r",
            "posture_at_proposal": "EXPLORE",
            "auto_proposed": True,
        }
        for i in range(_MAX_PROPOSALS_PER_SCAN)
    ]
    ledger.write_text("\n".join(json.dumps(p) for p in proposals) + "\n")
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json",
        repo_root=tmp_path,
        router=router,
        proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    assert len(envs) == _MAX_PROPOSALS_PER_SCAN
    for e in envs:
        assert e.requires_human_ack is True, (
            f"envelope {e.evidence.get('signature_hash')} "
            "missing requires_human_ack"
        )
