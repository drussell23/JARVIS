"""P1 Slice 3 — BacklogSensor auto_proposed second-source regression.

Pins the new ``_scan_proposals_ledger`` branch so:
  (a) Default-off behavior is byte-for-byte unchanged from pre-Slice-3.
  (b) When opted in, proposals → IntentEnvelopes carry the audit
      contract (source="auto_proposed", evidence flags, requires_human_ack).
  (c) Bounds + dedup + best-effort failure modes hold.

Sections:
    (A) Master flag — env reader + sensor respects flag
    (B) Happy path — proposal → envelope shape (source / urgency /
        requires_human_ack / evidence keys / signature_hash carries)
    (C) Posture → urgency mapping
    (D) Dedup — same signature_hash not re-emitted across scans
    (E) Bounds — max-proposals-per-scan / max-ledger-entries-scanned
    (F) Tolerance — missing ledger / malformed lines / partial fields /
        ingest failure
    (G) Independence — backlog.json scan not affected by proposals branch
        (positive + negative cases)
    (H) Authority invariants — sensor still has no banned imports
    (I) IntentEnvelope source allowlist contains "auto_proposed"
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
    IntentEnvelope,
)
from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
    BacklogSensor,
    _MAX_LEDGER_ENTRIES_TO_SCAN,
    _MAX_PROPOSALS_PER_SCAN,
    _auto_proposed_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED",
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY",
        "JARVIS_BACKLOG_URGENCY_HINT_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)


def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "true")


def _proposal(
    *,
    signature_hash: str = "abc123def456",
    description: str = "Investigate provider exhaustion",
    target_files: list = None,
    posture_at_proposal: str = "EXPLORE",
    cluster_member_count: int = 5,
    rationale: str = "5 ops failed; investigate fallback path.",
    cost_usd_spent: float = 0.05,
) -> dict:
    if target_files is None:
        target_files = ["backend/foo.py", "backend/bar.py"]
    return {
        "schema_version": "self_goal_formation.1",
        "signature_hash": signature_hash,
        "cluster_member_count": cluster_member_count,
        "target_files": target_files,
        "dominant_next_safe_action": "retry_with_smaller_seed",
        "description": description,
        "rationale": rationale,
        "posture_at_proposal": posture_at_proposal,
        "cost_usd_spent": cost_usd_spent,
        "timestamp_unix": 1_700_000_000.0,
        "auto_proposed": True,
    }


def _make_sensor(
    repo: Path,
    *,
    proposals: Optional[List[dict]] = None,
    backlog_entries: Optional[list] = None,
    ledger_path: Optional[Path] = None,
) -> BacklogSensor:
    backlog = repo / "backlog.json"
    if backlog_entries is not None:
        backlog.write_text(json.dumps(backlog_entries))
    jarvis_dir = repo / ".jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    eff_ledger = ledger_path or (
        jarvis_dir / "self_goal_formation_proposals.jsonl"
    )
    if proposals is not None:
        eff_ledger.write_text(
            "\n".join(json.dumps(p) for p in proposals) + "\n"
        )
    router = AsyncMock()
    router.ingest = AsyncMock(return_value="enqueued")
    return BacklogSensor(
        backlog_path=backlog,
        repo_root=repo,
        router=router,
        proposals_ledger_path=eff_ledger,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# (A) Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    """Slice 3 ships default-off — Slice 5 graduation flips it."""
    assert _auto_proposed_enabled() is False


def test_master_flag_explicit_true(monkeypatch):
    monkeypatch.setenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", "true")
    assert _auto_proposed_enabled() is True


def test_flag_off_proposals_present_no_envelopes(tmp_path, monkeypatch):
    """Pre-graduation: ledger present + populated → zero envelopes from
    the proposals branch (sensor behaves byte-for-byte like pre-Slice-3
    when only manual backlog entries exist)."""
    monkeypatch.delenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", raising=False)
    s = _make_sensor(tmp_path, proposals=[_proposal()])
    envs = _run(s.scan_once())
    assert envs == []


# ---------------------------------------------------------------------------
# (B) Happy path — envelope shape
# ---------------------------------------------------------------------------


def test_happy_path_proposal_becomes_envelope(monkeypatch, tmp_path):
    _enable(monkeypatch)
    s = _make_sensor(tmp_path, proposals=[_proposal()])
    envs = _run(s.scan_once())
    assert len(envs) == 1
    e = envs[0]
    assert isinstance(e, IntentEnvelope)
    assert e.source == "auto_proposed"
    assert e.requires_human_ack is True
    assert e.target_files == ("backend/foo.py", "backend/bar.py")


def test_envelope_evidence_carries_full_audit(monkeypatch, tmp_path):
    _enable(monkeypatch)
    s = _make_sensor(tmp_path, proposals=[_proposal()])
    envs = _run(s.scan_once())
    e = envs[0]
    assert e.evidence["auto_proposed"] is True
    assert e.evidence["signature_hash"] == "abc123def456"
    assert e.evidence["signature"] == "abc123def456"
    assert e.evidence["task_id"] == "auto-proposed:abc123def456"
    assert e.evidence["cluster_member_count"] == 5
    assert e.evidence["posture_at_proposal"] == "EXPLORE"
    assert e.evidence["schema_version"] == "self_goal_formation.1"
    assert "rationale" in e.evidence


# ---------------------------------------------------------------------------
# (C) Posture → urgency mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("posture,expected", [
    ("EXPLORE", "normal"),
    ("CONSOLIDATE", "high"),
    ("HARDEN", "normal"),    # Not in the map → fallback to normal
    ("MAINTAIN", "normal"),  # Same — defensive default
    ("", "normal"),          # Empty → fallback
])
def test_posture_urgency_mapping(monkeypatch, tmp_path, posture, expected):
    _enable(monkeypatch)
    s = _make_sensor(
        tmp_path,
        proposals=[_proposal(posture_at_proposal=posture)],
    )
    envs = _run(s.scan_once())
    assert len(envs) == 1
    assert envs[0].urgency == expected


# ---------------------------------------------------------------------------
# (D) Dedup
# ---------------------------------------------------------------------------


def test_dedup_same_signature_not_reemitted(monkeypatch, tmp_path):
    _enable(monkeypatch)
    s = _make_sensor(tmp_path, proposals=[_proposal()])
    first = _run(s.scan_once())
    second = _run(s.scan_once())
    assert len(first) == 1
    assert second == []


def test_dedup_distinct_signatures_each_emit_once(monkeypatch, tmp_path):
    _enable(monkeypatch)
    s = _make_sensor(tmp_path, proposals=[
        _proposal(signature_hash="aaa111"),
        _proposal(signature_hash="bbb222"),
    ])
    envs = _run(s.scan_once())
    sigs = sorted(e.evidence["signature_hash"] for e in envs)
    assert sigs == ["aaa111", "bbb222"]
    # Second scan dedupes both.
    envs2 = _run(s.scan_once())
    assert envs2 == []


# ---------------------------------------------------------------------------
# (E) Bounds
# ---------------------------------------------------------------------------


def test_max_proposals_per_scan_caps_emission(monkeypatch, tmp_path):
    _enable(monkeypatch)
    proposals = [
        _proposal(signature_hash=f"sig-{i:04d}")
        for i in range(_MAX_PROPOSALS_PER_SCAN + 5)
    ]
    s = _make_sensor(tmp_path, proposals=proposals)
    envs = _run(s.scan_once())
    assert len(envs) == _MAX_PROPOSALS_PER_SCAN


def test_max_ledger_entries_scanned_is_bounded(monkeypatch, tmp_path):
    """Pin: even an unbounded ledger only reads at most N most-recent rows."""
    _enable(monkeypatch)
    # Generate WAY more than the cap; sensor should only walk last N.
    n_total = _MAX_LEDGER_ENTRIES_TO_SCAN + 50
    proposals = [
        _proposal(signature_hash=f"old-{i:04d}")
        for i in range(n_total)
    ]
    s = _make_sensor(tmp_path, proposals=proposals)
    envs = _run(s.scan_once())
    # All emitted envelopes must come from the *most recent* slice of
    # the ledger (the last N rows). "old-0000" would only emit if we
    # walked beyond the cap.
    sigs = {e.evidence["signature_hash"] for e in envs}
    assert "old-0000" not in sigs


# ---------------------------------------------------------------------------
# (F) Tolerance
# ---------------------------------------------------------------------------


def test_missing_ledger_returns_empty(monkeypatch, tmp_path):
    _enable(monkeypatch)
    # Don't seed any proposals → ledger doesn't exist.
    backlog = tmp_path / "backlog.json"
    router = AsyncMock(); router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=backlog, repo_root=tmp_path, router=router,
        proposals_ledger_path=tmp_path / "nonexistent.jsonl",
    )
    envs = _run(s.scan_once())
    assert envs == []


def test_malformed_line_skipped_not_aborted(monkeypatch, tmp_path):
    _enable(monkeypatch)
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    # First valid, second malformed, third valid.
    lines = [
        json.dumps(_proposal(signature_hash="good1")),
        "this is not json",
        json.dumps(_proposal(signature_hash="good2")),
    ]
    ledger.write_text("\n".join(lines) + "\n")
    router = AsyncMock(); router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json", repo_root=tmp_path,
        router=router, proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    sigs = sorted(e.evidence["signature_hash"] for e in envs)
    assert sigs == ["good1", "good2"]


def test_proposal_missing_signature_hash_skipped(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = _proposal()
    p["signature_hash"] = ""
    s = _make_sensor(tmp_path, proposals=[p])
    assert _run(s.scan_once()) == []


def test_proposal_missing_target_files_skipped(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = _proposal(target_files=[])
    s = _make_sensor(tmp_path, proposals=[p])
    assert _run(s.scan_once()) == []


def test_proposal_missing_description_skipped(monkeypatch, tmp_path):
    _enable(monkeypatch)
    p = _proposal(description="")
    s = _make_sensor(tmp_path, proposals=[p])
    assert _run(s.scan_once()) == []


def test_router_ingest_failure_swallowed(monkeypatch, tmp_path, caplog):
    _enable(monkeypatch)
    backlog = tmp_path / "backlog.json"
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    ledger.write_text(json.dumps(_proposal()) + "\n")
    router = AsyncMock()
    router.ingest = AsyncMock(side_effect=RuntimeError("intake down"))
    s = BacklogSensor(
        backlog_path=backlog, repo_root=tmp_path, router=router,
        proposals_ledger_path=ledger,
    )
    with caplog.at_level(logging.ERROR):
        envs = _run(s.scan_once())
    # Sensor must not raise; envelopes simply not emitted.
    assert envs == []


# ---------------------------------------------------------------------------
# (G) Independence — backlog.json + proposals branches don't interfere
# ---------------------------------------------------------------------------


def test_proposals_scan_works_when_backlog_json_missing(monkeypatch, tmp_path):
    """Pin: a missing backlog.json must NOT block the proposals branch
    (regression: the original scan_once short-circuited on missing
    backlog.json — Slice 3 must survive that)."""
    _enable(monkeypatch)
    # No backlog.json written.
    (tmp_path / ".jarvis").mkdir()
    ledger = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    ledger.write_text(json.dumps(_proposal()) + "\n")
    router = AsyncMock(); router.ingest = AsyncMock(return_value="enqueued")
    s = BacklogSensor(
        backlog_path=tmp_path / "backlog.json", repo_root=tmp_path,
        router=router, proposals_ledger_path=ledger,
    )
    envs = _run(s.scan_once())
    assert len(envs) == 1
    assert envs[0].source == "auto_proposed"


def test_both_sources_emit_in_one_scan(monkeypatch, tmp_path):
    _enable(monkeypatch)
    backlog_entries = [{
        "task_id": "manual-1",
        "description": "Manually authored backlog entry",
        "target_files": ["src/manual.py"],
        "priority": 3,
        "repo": "jarvis",
        "status": "pending",
    }]
    s = _make_sensor(
        tmp_path,
        backlog_entries=backlog_entries,
        proposals=[_proposal()],
    )
    envs = _run(s.scan_once())
    sources = sorted(e.source for e in envs)
    assert sources == ["auto_proposed", "backlog"]


def test_flag_off_only_backlog_json_emits(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", raising=False)
    backlog_entries = [{
        "task_id": "manual-1",
        "description": "Manually authored backlog entry",
        "target_files": ["src/manual.py"],
        "priority": 3, "repo": "jarvis", "status": "pending",
    }]
    s = _make_sensor(
        tmp_path,
        backlog_entries=backlog_entries,
        proposals=[_proposal()],
    )
    envs = _run(s.scan_once())
    assert len(envs) == 1
    assert envs[0].source == "backlog"  # proposal NOT emitted


# ---------------------------------------------------------------------------
# (H) Authority invariants
# ---------------------------------------------------------------------------


def test_backlog_sensor_no_authority_imports_post_slice3():
    """The sensor stays read-only / advisory after the auto_proposed
    second source lands. PRD §12.2 invariant unchanged."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py"
    ).read_text(encoding="utf-8")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for imp in banned:
        assert imp not in src, f"banned authority import in backlog_sensor.py: {imp}"


def test_auto_proposed_envelope_evidence_pins_audit_keys():
    """Source-grep pin: the evidence dict construction must include
    every audit field downstream surfaces depend on. Catches a refactor
    that silently drops a key."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py"
    ).read_text(encoding="utf-8")
    required_evidence_keys = [
        '"auto_proposed": True',
        '"signature_hash": sig_hash',
        '"cluster_member_count":',
        '"rationale":',
        '"posture_at_proposal":',
        '"schema_version":',
    ]
    for key in required_evidence_keys:
        assert key in src, f"audit key missing: {key}"


# ---------------------------------------------------------------------------
# (I) IntentEnvelope source allowlist
# ---------------------------------------------------------------------------


def test_auto_proposed_in_envelope_source_allowlist():
    """Pin: the IntentEnvelope schema explicitly allows source='auto_proposed'.
    A future refactor that removes it would break the entire P1 surface."""
    assert "auto_proposed" in _VALID_SOURCES
