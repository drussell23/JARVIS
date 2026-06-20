"""Sovereign Telemetry Manifest — PR-body proof renderer tests (2026-06-20)."""
from __future__ import annotations

from backend.core.ouroboros.governance.graduation.telemetry_manifest import (
    evidence_digest,
    manifest_recommends_merge,
    render_graduation_manifest,
)


def _clean_ev(ttft_mean=800.0, ttft_max=1200.0):
    return {
        "ttft_n": 3, "ttft_mean_ms": ttft_mean, "ttft_max_ms": ttft_max,
        "ttft_degraded": False, "ast_corruption_signals": 0,
        "ast_corrupted": False, "recovered": True,
        "session_outcome": "complete",
    }


def _render(evs, required=3):
    return render_graduation_manifest(
        "JARVIS_CURIOSITY_ENGINE_ENABLED",
        soak_evidence=evs,
        session_ids=["bt-1", "bt-2", "bt-3"],
        required_clean=required,
        source_file="backend/.../flag_registry_seed.py",
        ttft_ceiling_ms=30000.0,
        revert_sha="abc1234",
    )


def test_clean_three_soaks_recommends_merge():
    evs = [_clean_ev(), _clean_ev(), _clean_ev()]
    assert manifest_recommends_merge(evs, required_clean=3) is True
    body = _render(evs)
    assert "[SOVEREIGN GRADUATION] Activated `JARVIS_CURIOSITY_ENGINE_ENABLED`" in body
    assert "Recommend merge." in body
    assert "Empirical proof (per soak)" in body
    assert "| Soak 1 |" in body and "| Soak 3 |" in body  # 3 evidence rows
    assert "| Soak 4 |" not in body
    assert "✅ zero parse errors" in body
    assert "0%" in body  # FSM exhaustion rate


def test_ttft_degraded_vetoes_merge():
    bad = _clean_ev()
    bad["ttft_degraded"] = True
    evs = [_clean_ev(), _clean_ev(), bad]
    assert manifest_recommends_merge(evs, required_clean=3) is False
    body = _render(evs)
    assert "Do NOT merge" in body
    assert "TTFT degraded" in body
    assert "❌ DEGRADED" in body


def test_ast_corruption_vetoes_merge():
    bad = _clean_ev()
    bad["ast_corrupted"] = True
    bad["ast_corruption_signals"] = 2
    evs = [_clean_ev(), _clean_ev(), bad]
    assert manifest_recommends_merge(evs, required_clean=3) is False
    body = _render(evs)
    assert "Do NOT merge" in body
    assert "AST corruption detected" in body


def test_insufficient_soaks_vetoes_merge():
    evs = [_clean_ev(), _clean_ev()]  # only 2 of 3
    assert manifest_recommends_merge(evs, required_clean=3) is False
    body = _render(evs, required=3)
    assert "Do NOT merge" in body
    assert "2/3 clean soaks" in body


def test_rollback_strategy_always_present():
    body = _render([_clean_ev()] * 3)
    assert "🛟 Rollback Strategy" in body
    assert "export JARVIS_CURIOSITY_ENGINE_ENABLED=false" in body
    assert "git revert abc1234" in body


def test_manifest_is_deterministic():
    evs = [_clean_ev(), _clean_ev(), _clean_ev()]
    assert _render(evs) == _render(evs)  # no wall-clock / randomness


def test_evidence_digest_stable_and_order_sensitive():
    a = [_clean_ev(ttft_mean=800.0)]
    b = [_clean_ev(ttft_mean=900.0)]
    assert evidence_digest(a) == evidence_digest(a)
    assert evidence_digest(a) != evidence_digest(b)
    assert len(evidence_digest(a)) == 16


def test_render_never_raises_on_garbage():
    body = render_graduation_manifest(
        "JARVIS_X",
        soak_evidence=["not-a-dict", None, {"ttft_n": "bad"}],
        session_ids=[],
        required_clean=3,
        source_file="x.py",
        ttft_ceiling_ms=30000.0,
    )
    assert "[SOVEREIGN GRADUATION]" in body
    assert "Rollback Strategy" in body
