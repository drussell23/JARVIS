"""Sovereign Cognitive Crucible — soak-evidence → engine PR wiring (2026-06-20)."""
from __future__ import annotations

import json

from backend.core.ouroboros.governance.graduation.live_fire_soak import (
    recent_clean_crucible_evidence,
)
from backend.core.ouroboros.governance.autonomous_graduation_engine import (
    GraduationDecision,
    GraduationDisposition,
    GraduationTier,
    _maybe_propose_source_pr,
)


def _row(flag, sid, outcome="clean", with_cruc=True):
    tel = {"recovered": True}
    if with_cruc:
        tel["crucible"] = {
            "ttft_n": 3, "ttft_mean_ms": 800.0, "ttft_max_ms": 1000.0,
            "ttft_degraded": False, "ast_corruption_signals": 0,
            "ast_corrupted": False, "recovered": True,
            "session_outcome": "complete",
        }
    return json.dumps({
        "flag_name": flag, "session_id": sid, "outcome": outcome,
        "telemetry": tel,
    })


def test_recent_clean_crucible_evidence_filters(tmp_path, monkeypatch):
    hist = tmp_path / "hist.jsonl"
    hist.write_text("\n".join([
        _row("JARVIS_A", "s1"),
        _row("JARVIS_B", "s2"),                       # other flag
        _row("JARVIS_A", "s3", outcome="runner"),     # not clean
        _row("JARVIS_A", "s4"),
        _row("JARVIS_A", "s5", with_cruc=False),      # no crucible bundle
        _row("JARVIS_A", "s6"),
    ]) + "\n")
    monkeypatch.setenv("JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH", str(hist))
    ev = recent_clean_crucible_evidence("JARVIS_A", limit=3)
    sids = [e["session_id"] for e in ev]
    assert sids == ["s1", "s4", "s6"]  # clean + has-crucible, most-recent 3
    assert all(e["ttft_degraded"] is False for e in ev)


def test_recent_clean_evidence_missing_history_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH",
        str(tmp_path / "nope.jsonl"),
    )
    assert recent_clean_crucible_evidence("JARVIS_A") == []


def _decision(flag="JARVIS_A"):
    return GraduationDecision(
        flag_name=flag,
        tier=GraduationTier.STANDARD,
        disposition=GraduationDisposition.AUTO_FLIP,
        evidence={},
        delta="",
        evidence_sha256="x",
    )


def test_pr_hook_gate_off_is_noop(monkeypatch):
    monkeypatch.delenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", raising=False)
    assert _maybe_propose_source_pr(_decision()) is False


def test_pr_hook_gate_on_no_evidence_abstains(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH",
        str(tmp_path / "empty.jsonl"),
    )
    # No clean evidence → manifest veto → no PR scheduled (safe; no empty PRs).
    assert _maybe_propose_source_pr(_decision()) is False
