"""Sovereign Cognitive Crucible — TTFT + AST math-veto tests (2026-06-20)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.graduation.telemetry_parse import (
    parse_metrics,
)
from backend.core.ouroboros.governance.graduation.crucible_verdict import (
    ast_corrupted,
    crucible_evidence,
    ttft_degraded,
    ttft_stats,
)


# ── parse extensions ────────────────────────────────────────────────────────

def test_parse_collects_ttft_samples():
    log = "x ttft_ms=800 y\nz ttft_ms=1200 q\nttft_ms=0 fail\n"
    m = parse_metrics(log, None)
    assert m.ttft_samples_ms == [800, 1200, 0]


def test_parse_counts_ast_corruption_signals():
    log = (
        "PhaseRunnerASTValidationError: bad\n"
        "failure_class=ast_invalid here\n"
        "candidate produced invalid syntax\n"
        "placeholder_detected in patch\n"
    )
    m = parse_metrics(log, None)
    assert m.ast_corruption_signals == 4


def test_parse_no_signals_defaults_empty():
    m = parse_metrics("phase=GENERATE state=applied", None)
    assert m.ttft_samples_ms == []
    assert m.ast_corruption_signals == 0


# ── ttft stats / degradation ────────────────────────────────────────────────

def test_ttft_stats_ignores_zero_sentinel():
    m = parse_metrics("ttft_ms=1000 ttft_ms=3000 ttft_ms=0", None)
    s = ttft_stats(m)
    assert s["n"] == 2
    assert s["mean_ms"] == 2000.0
    assert s["max_ms"] == 3000.0


def test_ttft_no_samples_fails_open():
    m = parse_metrics("no latency here", None)
    bad, detail = ttft_degraded(m)
    assert bad is False
    assert detail == "no_ttft_samples"


def test_ttft_over_absolute_ceiling_vetoes(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_CEILING_MS", "5000")
    m = parse_metrics("ttft_ms=8000 ttft_ms=9000", None)
    bad, detail = ttft_degraded(m)
    assert bad is True
    assert "ceiling" in detail


def test_ttft_under_ceiling_passes(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_CEILING_MS", "5000")
    m = parse_metrics("ttft_ms=800 ttft_ms=1200", None)
    bad, _ = ttft_degraded(m)
    assert bad is False


def test_ttft_baseline_ratio_vetoes(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_CEILING_MS", "100000")
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_DEGRADE_RATIO", "1.5")
    m = parse_metrics("ttft_ms=2000 ttft_ms=2000", None)
    # baseline 1000 → 1.5x = 1500; mean 2000 > 1500 → degraded
    bad, detail = ttft_degraded(m, baseline_ms=1000.0)
    assert bad is True
    assert "baseline" in detail


def test_ttft_baseline_within_ratio_passes(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_CEILING_MS", "100000")
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_DEGRADE_RATIO", "2.0")
    m = parse_metrics("ttft_ms=1800", None)
    bad, _ = ttft_degraded(m, baseline_ms=1000.0)  # 1800 < 2000
    assert bad is False


# ── ast corruption ──────────────────────────────────────────────────────────

def test_ast_corrupted_when_signals_present():
    m = parse_metrics("PhaseRunnerASTValidationError boom", None)
    bad, detail = ast_corrupted(m)
    assert bad is True
    assert "corruption" in detail


def test_ast_clean_when_no_signals():
    m = parse_metrics("phase=COMPLETE state=applied", None)
    bad, _ = ast_corrupted(m)
    assert bad is False


# ── evidence bundle (manifest input) ────────────────────────────────────────

def test_crucible_evidence_shape(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_TTFT_CEILING_MS", "5000")
    m = parse_metrics(
        "ttft_ms=800 ttft_ms=1200 state=applied", None,
    )
    ev = crucible_evidence(m)
    assert ev["ttft_n"] == 2
    assert ev["ttft_mean_ms"] == 1000.0
    assert ev["ttft_degraded"] is False
    assert ev["ast_corrupted"] is False
    assert ev["recovered"] is True
    assert ev["ttft_ceiling_ms"] == 5000.0


def test_crucible_evidence_never_raises_on_garbage():
    class _Bad:
        ttft_samples_ms = "not-a-list"
        ast_corruption_signals = "nope"
    ev = crucible_evidence(_Bad())
    assert ev["ttft_n"] == 0
    assert ev["ast_corruption_signals"] == 0
