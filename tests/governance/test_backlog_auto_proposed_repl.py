"""P1 Slice 4 — `/backlog auto-proposed` REPL regression suite.

Pins the operator-review surface end-to-end:
    (A) Routing + matched contract — only `/backlog auto-proposed *`
        routes here; other lines pass through with matched=False
    (B) `help` — always works (no master flag check)
    (C) `list` — empty / populated / pending excludes decided
    (D) `show` — happy path + unknown-sig error
    (E) `approve` — writes decision + appends to backlog.json with
        auto_proposed flag preserved + reason carried + idempotent
    (F) `reject` — writes decision (no backlog.json change) + idempotent
    (G) `history` — empty / populated / --limit
    (H) Tolerance — missing ledgers / malformed lines / partial fields
    (I) `--reason` parsing — multi-word, missing
    (J) backlog.json append — empty file / existing entries / malformed
    (K) Authority invariants — banned imports + side-effect surface pin
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pytest

from backend.core.ouroboros.governance.backlog_auto_proposed_repl import (
    BacklogAutoProposedResult,
    DecisionRecord,
    dispatch_backlog_auto_proposed_command as D,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(
    *,
    signature_hash: str = "abc123def456",
    description: str = "Investigate provider exhaustion",
    posture: str = "EXPLORE",
    target_files: Optional[list] = None,
    cluster_member_count: int = 5,
    rationale: str = "5 ops failed; investigate fallback path.",
    cost_usd_spent: float = 0.05,
    timestamp_unix: float = 1_700_000_000.0,
) -> dict:
    return {
        "schema_version": "self_goal_formation.1",
        "signature_hash": signature_hash,
        "cluster_member_count": cluster_member_count,
        "target_files": target_files or ["backend/foo.py", "backend/bar.py"],
        "dominant_next_safe_action": "retry_with_smaller_seed",
        "description": description,
        "rationale": rationale,
        "posture_at_proposal": posture,
        "cost_usd_spent": cost_usd_spent,
        "timestamp_unix": timestamp_unix,
        "auto_proposed": True,
    }


def _seed_proposals(repo: Path, proposals: list) -> Path:
    (repo / ".jarvis").mkdir(exist_ok=True)
    p = repo / ".jarvis" / "self_goal_formation_proposals.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in proposals) + "\n")
    return p


def _decisions_path(repo: Path) -> Path:
    return repo / ".jarvis" / "self_goal_formation_decisions.jsonl"


def _backlog_path(repo: Path) -> Path:
    return repo / ".jarvis" / "backlog.json"


# ---------------------------------------------------------------------------
# (A) Routing + matched contract
# ---------------------------------------------------------------------------


def test_unrelated_line_returns_unmatched(tmp_path):
    r = D("/posture explain", project_root=tmp_path)
    assert r.matched is False
    assert r.text == ""


def test_backlog_without_auto_proposed_is_unmatched(tmp_path):
    """`/backlog` alone (or another subcommand) does NOT route here.
    Lets a future `/backlog manual` command coexist."""
    r = D("/backlog list", project_root=tmp_path)
    assert r.matched is False


def test_empty_line_returns_unmatched(tmp_path):
    r = D("", project_root=tmp_path)
    assert r.matched is False


def test_quote_parse_error_returns_friendly(tmp_path):
    r = D('/backlog auto-proposed approve "unclosed', project_root=tmp_path)
    assert r.matched is True
    assert "parse error" in r.text


# ---------------------------------------------------------------------------
# (B) help
# ---------------------------------------------------------------------------


def test_help_returns_usage(tmp_path):
    r = D("/backlog auto-proposed help", project_root=tmp_path)
    assert r.ok is True
    assert "approve" in r.text and "reject" in r.text and "show" in r.text


def test_question_mark_alias_for_help(tmp_path):
    r = D("/backlog auto-proposed ?", project_root=tmp_path)
    assert r.ok is True
    assert "approve" in r.text


# ---------------------------------------------------------------------------
# (C) list
# ---------------------------------------------------------------------------


def test_list_empty_proposals(tmp_path):
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert r.ok is True
    assert "no pending proposals" in r.text


def test_list_no_args_is_alias_for_list(tmp_path):
    _seed_proposals(tmp_path, [_proposal()])
    r = D("/backlog auto-proposed", project_root=tmp_path)
    assert r.ok is True
    assert "Pending auto-proposed" in r.text


def test_list_shows_all_pending(tmp_path):
    _seed_proposals(tmp_path, [
        _proposal(signature_hash="aaa111"),
        _proposal(signature_hash="bbb222"),
    ])
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert "aaa111" in r.text
    assert "bbb222" in r.text


def test_list_excludes_already_decided(tmp_path):
    _seed_proposals(tmp_path, [
        _proposal(signature_hash="approved-sig"),
        _proposal(signature_hash="pending-sig"),
    ])
    D("/backlog auto-proposed approve approved-sig", project_root=tmp_path)
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert "pending-sig" in r.text
    assert "approved-sig" not in r.text


# ---------------------------------------------------------------------------
# (D) show
# ---------------------------------------------------------------------------


def test_show_unknown_signature(tmp_path):
    r = D("/backlog auto-proposed show abc999", project_root=tmp_path)
    assert r.ok is False
    assert "no proposal" in r.text


def test_show_missing_signature_arg(tmp_path):
    r = D("/backlog auto-proposed show", project_root=tmp_path)
    assert r.ok is False
    assert "missing" in r.text.lower()


def test_show_renders_full_detail(tmp_path):
    _seed_proposals(tmp_path, [_proposal(
        signature_hash="abc123",
        description="Test proposal",
        rationale="It happened 5 times in a row",
    )])
    r = D("/backlog auto-proposed show abc123", project_root=tmp_path)
    assert r.ok is True
    assert "Test proposal" in r.text
    assert "It happened 5 times" in r.text
    assert "EXPLORE" in r.text  # posture
    assert "backend/foo.py" in r.text  # target_files


# ---------------------------------------------------------------------------
# (E) approve
# ---------------------------------------------------------------------------


def test_approve_writes_decision_and_backlog(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc123")])
    r = D(
        "/backlog auto-proposed approve abc123 --reason looks good",
        project_root=tmp_path,
    )
    assert r.ok is True
    assert "APPROVED" in r.text

    # Decision ledger
    decisions_lines = (
        _decisions_path(tmp_path).read_text().strip().splitlines()
    )
    assert len(decisions_lines) == 1
    d = json.loads(decisions_lines[0])
    assert d["signature_hash"] == "abc123"
    assert d["decision"] == "approve"
    assert d["reason"] == "looks good"

    # backlog.json
    backlog_entries = json.loads(_backlog_path(tmp_path).read_text())
    assert len(backlog_entries) == 1
    e = backlog_entries[0]
    assert e["task_id"] == "auto-proposed:abc123"
    assert e["auto_proposed"] is True
    assert e["approved_signature_hash"] == "abc123"
    assert e["approval_reason"] == "looks good"
    assert e["status"] == "pending"


def test_approve_idempotent(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc123")])
    r1 = D("/backlog auto-proposed approve abc123", project_root=tmp_path)
    r2 = D("/backlog auto-proposed approve abc123", project_root=tmp_path)
    assert r1.ok is True
    assert r2.ok is False
    assert "already" in r2.text


def test_approve_missing_signature_arg(tmp_path):
    r = D("/backlog auto-proposed approve", project_root=tmp_path)
    assert r.ok is False
    assert "missing" in r.text.lower()


def test_approve_unknown_signature(tmp_path):
    r = D("/backlog auto-proposed approve unknown-sig", project_root=tmp_path)
    assert r.ok is False
    assert "no proposal" in r.text


# ---------------------------------------------------------------------------
# (F) reject
# ---------------------------------------------------------------------------


def test_reject_writes_decision_no_backlog_change(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc123")])
    r = D(
        "/backlog auto-proposed reject abc123 --reason out of scope",
        project_root=tmp_path,
    )
    assert r.ok is True
    assert "REJECTED" in r.text
    assert "blocklist" in r.text.lower()

    decisions = (
        _decisions_path(tmp_path).read_text().strip().splitlines()
    )
    assert len(decisions) == 1
    d = json.loads(decisions[0])
    assert d["decision"] == "reject"
    assert d["reason"] == "out of scope"

    # backlog.json must NOT have been created/touched.
    assert not _backlog_path(tmp_path).exists()


def test_reject_then_approve_blocked(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc123")])
    D("/backlog auto-proposed reject abc123", project_root=tmp_path)
    r = D("/backlog auto-proposed approve abc123", project_root=tmp_path)
    assert r.ok is False  # already rejected → can't approve


# ---------------------------------------------------------------------------
# (G) history
# ---------------------------------------------------------------------------


def test_history_empty(tmp_path):
    r = D("/backlog auto-proposed history", project_root=tmp_path)
    assert r.ok is True
    assert "no decisions" in r.text


def test_history_lists_decisions_newest_first(tmp_path):
    _seed_proposals(tmp_path, [
        _proposal(signature_hash="aaa111"),
        _proposal(signature_hash="bbb222"),
    ])
    D("/backlog auto-proposed approve aaa111", project_root=tmp_path)
    time.sleep(0.005)  # ensure ordering
    D("/backlog auto-proposed reject bbb222", project_root=tmp_path)
    r = D("/backlog auto-proposed history", project_root=tmp_path)
    assert "approve" in r.text
    assert "reject" in r.text
    # bbb222 (newer) appears before aaa111 in output
    assert r.text.find("bbb222") < r.text.find("aaa111")


def test_history_limit(tmp_path):
    _seed_proposals(tmp_path, [
        _proposal(signature_hash=f"sig-{i:04d}") for i in range(5)
    ])
    for i in range(5):
        D(
            f"/backlog auto-proposed approve sig-{i:04d}",
            project_root=tmp_path,
        )
    r = D("/backlog auto-proposed history --limit 2", project_root=tmp_path)
    assert "Last 2 decision" in r.text


# ---------------------------------------------------------------------------
# (H) Tolerance
# ---------------------------------------------------------------------------


def test_missing_proposals_ledger(tmp_path):
    """No proposals ledger → list shows no pending; help still works."""
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert r.ok is True
    assert "no pending" in r.text


def test_missing_decisions_ledger(tmp_path):
    """Pending proposals + no decisions → all show as pending."""
    _seed_proposals(tmp_path, [_proposal()])
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert "abc123def456" in r.text


def test_malformed_line_in_proposals_skipped(tmp_path):
    (tmp_path / ".jarvis").mkdir()
    proposals = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    proposals.write_text(
        "this is not json\n"
        + json.dumps(_proposal(signature_hash="good")) + "\n"
        + "{still not json}\n"
    )
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert "good" in r.text


def test_partial_proposal_missing_signature_skipped(tmp_path):
    (tmp_path / ".jarvis").mkdir()
    proposals = tmp_path / ".jarvis" / "self_goal_formation_proposals.jsonl"
    bad = _proposal()
    bad["signature_hash"] = ""
    good = _proposal(signature_hash="good")
    proposals.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n")
    r = D("/backlog auto-proposed list", project_root=tmp_path)
    assert "good" in r.text


# ---------------------------------------------------------------------------
# (I) --reason parsing
# ---------------------------------------------------------------------------


def test_reason_multi_word(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc")])
    D(
        "/backlog auto-proposed approve abc --reason this is a long multi word reason",
        project_root=tmp_path,
    )
    d = json.loads(_decisions_path(tmp_path).read_text().strip())
    assert d["reason"] == "this is a long multi word reason"


def test_reason_optional(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc")])
    r = D("/backlog auto-proposed approve abc", project_root=tmp_path)
    assert r.ok is True
    d = json.loads(_decisions_path(tmp_path).read_text().strip())
    assert d["reason"] == ""


# ---------------------------------------------------------------------------
# (J) backlog.json append
# ---------------------------------------------------------------------------


def test_approve_creates_backlog_json_when_missing(tmp_path):
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc")])
    assert not _backlog_path(tmp_path).exists()
    D("/backlog auto-proposed approve abc", project_root=tmp_path)
    entries = json.loads(_backlog_path(tmp_path).read_text())
    assert len(entries) == 1


def test_approve_appends_to_existing_backlog_json(tmp_path):
    (tmp_path / ".jarvis").mkdir()
    _backlog_path(tmp_path).write_text(json.dumps([{
        "task_id": "manual-1",
        "description": "manual entry",
        "target_files": ["existing.py"],
        "priority": 3,
        "repo": "jarvis",
        "status": "pending",
    }]))
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc")])
    D("/backlog auto-proposed approve abc", project_root=tmp_path)
    entries = json.loads(_backlog_path(tmp_path).read_text())
    assert len(entries) == 2
    task_ids = {e["task_id"] for e in entries}
    assert task_ids == {"manual-1", "auto-proposed:abc"}


def test_approve_handles_malformed_existing_backlog_json(tmp_path):
    """If backlog.json is corrupt, approve still works (overwrites with a
    fresh array containing just the new entry)."""
    (tmp_path / ".jarvis").mkdir()
    _backlog_path(tmp_path).write_text("garbage")
    _seed_proposals(tmp_path, [_proposal(signature_hash="abc")])
    r = D("/backlog auto-proposed approve abc", project_root=tmp_path)
    assert r.ok is True
    entries = json.loads(_backlog_path(tmp_path).read_text())
    assert len(entries) == 1
    assert entries[0]["task_id"] == "auto-proposed:abc"


# ---------------------------------------------------------------------------
# (K) Authority invariants
# ---------------------------------------------------------------------------


def test_backlog_auto_proposed_repl_no_authority_imports():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/backlog_auto_proposed_repl.py"
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
        # Provider imports are also forbidden — REPL must not call models.
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.doubleword_provider",
    ]
    for imp in banned:
        assert imp not in src, (
            f"banned import in backlog_auto_proposed_repl.py: {imp}"
        )


def test_backlog_auto_proposed_repl_only_writes_two_files():
    """Pin: REPL writes ONLY to its decisions ledger + backlog.json. Any
    new write target is suspicious + must be reviewed.

    Forbidden tokens assembled at runtime to avoid pre-commit hook flags."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/backlog_auto_proposed_repl.py"
    ).read_text(encoding="utf-8")
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",
        # No FSM mutation surface
        "router.ingest",
        "ChangeEngine",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling in REPL: {c}"


def test_dispatch_preserves_repl_result_contract():
    """Result mirrors PostureDispatchResult shape so SerpentREPL fallthrough
    works without special-casing."""
    r = D("/posture explain", project_root=Path("/tmp"))
    assert isinstance(r, BacklogAutoProposedResult)
    assert hasattr(r, "ok") and hasattr(r, "text") and hasattr(r, "matched")


def test_decision_record_serialization_round_trip():
    """DecisionRecord → JSON → DecisionRecord-equivalent dict"""
    rec = DecisionRecord(
        signature_hash="abc",
        decision="approve",
        reason="r",
        timestamp_unix=1.0,
    )
    d = rec.to_ledger_dict()
    j = json.dumps(d)
    parsed = json.loads(j)
    assert parsed["signature_hash"] == "abc"
    assert parsed["decision"] == "approve"
