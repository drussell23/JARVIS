"""P0.4 — close the learning plane: reputation write + intake read-bias.

The reputation writer (memory_engine) and reader lived only inside the
harness-only ConsciousnessBridge, so on the default path O+V never learned
WHICH files historically fail / churn / carry blast radius, and never biased
perception toward them. P0.4 decouples the store into a disk-backed singleton
that BOTH the terminal-op write seam (orchestrator._record_ledger) and the
intake read-bias (_compute_priority) use.

Proof: record_file_outcome updates fragility; it persists cross-session; the
singleton is stable; the gates are §33.1-correct (bias requires the write
master); intake biases toward fragile files ONLY when enabled, bounded, and
fail-soft; and the orchestrator write seam is wired (guard re-severing).
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.consciousness import memory_engine as ME
from backend.core.ouroboros.governance.intake import unified_intake_router as R
from backend.core.ouroboros.governance.intake.intent_envelope import (
    make_envelope,
)


@pytest.fixture(autouse=True)
def _reset():
    ME.reset_default_memory_engine_for_tests()
    yield
    ME.reset_default_memory_engine_for_tests()


def _env(source, target_files, urgency="normal"):
    return make_envelope(
        source=source, description="x", target_files=tuple(target_files),
        repo="jarvis", confidence=0.5, urgency=urgency, evidence={},
        requires_human_ack=False,
    )


# ── the store: write, fragility, persistence, singleton ──────────────

def test_record_file_outcome_builds_fragility(tmp_path):
    e = ME.get_default_memory_engine(tmp_path)
    for _ in range(3):
        e.record_file_outcome(("hot.py",), success=False, blast_radius=5)
    e.record_file_outcome(("cold.py",), success=True)
    assert e.get_file_reputation("hot.py").fragility_score > 0.5
    assert e.get_file_reputation("cold.py").fragility_score < 0.1


def test_persists_cross_session(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_FLUSH_EVERY", "1")
    ME.get_default_memory_engine(tmp_path).record_file_outcome(
        ("hot.py",), success=False, blast_radius=5,
    )
    ME.reset_default_memory_engine_for_tests()  # simulate a new session
    frag = ME.get_default_memory_engine(tmp_path).get_file_reputation("hot.py").fragility_score
    assert frag > 0.0  # reloaded from disk


def test_singleton_stable_per_root(tmp_path):
    a = ME.get_default_memory_engine(tmp_path)
    b = ME.get_default_memory_engine(tmp_path)
    assert a is b


def test_record_ignores_empty_targets(tmp_path):
    e = ME.get_default_memory_engine(tmp_path)
    e.record_file_outcome((), success=False)  # no files → no-op, no raise
    e.record_file_outcome(None, success=True)


# ── gates: §33.1 default-off, bias requires the write master ─────────

def test_gates_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_MEMORY_REPUTATION_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", raising=False)
    assert ME.reputation_write_enabled() is False
    assert ME.reputation_bias_enabled() is False


def test_bias_requires_write_master(monkeypatch):
    """Bias without accumulation is inert — enabling the bias alone must not
    engage it."""
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "true")
    assert ME.reputation_bias_enabled() is False


# ── intake read-bias ─────────────────────────────────────────────────

@pytest.fixture
def _bias_on(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_FLUSH_EVERY", "1")


def _make_fragile(tmp_path, path, n=3):
    e = ME.get_default_memory_engine(tmp_path)
    for _ in range(n):
        e.record_file_outcome((path,), success=False, blast_radius=5)


def test_fragile_file_wins_the_queue(tmp_path, _bias_on):
    _make_fragile(tmp_path, "hot.py")
    p_hot, _ = R._compute_priority(_env("ai_miner", ("hot.py",)), repo_root=tmp_path)
    p_cold, _ = R._compute_priority(_env("ai_miner", ("cold.py",)), repo_root=tmp_path)
    assert p_hot < p_cold  # lower int = higher priority


def test_bias_stashes_evidence(tmp_path, _bias_on):
    _make_fragile(tmp_path, "hot.py")
    e = _env("ai_miner", ("hot.py",))
    R._compute_priority(e, repo_root=tmp_path)
    assert e.evidence.get("reputation_boost", 0) >= 1
    assert e.evidence.get("reputation_fragility", 0.0) > 0.5


def test_bias_bounded_by_max(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BOOST_MAX", "2")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_FLUSH_EVERY", "1")
    _make_fragile(tmp_path, "hot.py", n=20)  # push fragility toward 1.0
    e = _env("ai_miner", ("hot.py",))
    R._compute_priority(e, repo_root=tmp_path)
    assert e.evidence.get("reputation_boost", 99) <= 2  # capped


def test_no_bias_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_FLUSH_EVERY", "1")
    _make_fragile(tmp_path, "hot.py")
    p_hot, _ = R._compute_priority(_env("ai_miner", ("hot.py",)), repo_root=tmp_path)
    p_cold, _ = R._compute_priority(_env("ai_miner", ("cold.py",)), repo_root=tmp_path)
    assert p_hot == p_cold  # bias inert → same priority


def test_bias_failsoft(tmp_path, _bias_on, monkeypatch):
    """A reputation-scoring fault must never break intake."""
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ME, "get_default_memory_engine", _boom)
    # Must not raise.
    p, _ = R._compute_priority(_env("ai_miner", ("hot.py",)), repo_root=tmp_path)
    assert isinstance(p, int)


# ── write seam is wired into the terminal-op recorder ────────────────

def test_orchestrator_write_seam_wired():
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator.GovernedOrchestrator._record_ledger)
    assert "record_file_outcome" in src
    assert "reputation_write_enabled" in src
