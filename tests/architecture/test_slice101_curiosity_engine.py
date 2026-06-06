"""Slice 101 Phase 8 — the Dynamic Entropy Engine (compositional curiosity).

Proves the proactive loop: the engine computes Shannon entropy over the semantic-
index domain distribution, identifies the sparsest (least-explored) zone, allocates
a recoverable compute budget that shrinks under load, and emits the zone as a
GOVERNED low-urgency exploration op — never executing a probe directly, so the
Antivenom cage is preserved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.core.ouroboros.governance import domain_entropy_engine as DEE


def _load(verdict: str):
    return SimpleNamespace(verdict=SimpleNamespace(value=verdict))


class _FakeRouter:
    """Records every governed envelope handed to ingest(). This is the ONLY
    side-effect channel the engine has — proving it never runs a probe itself."""

    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return "enqueued"


# === Mathematical certainty: Shannon entropy over the domain distribution ===

def test_shannon_entropy_is_mathematically_exact(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    # sizes [2, 50, 48] → P=[0.02, 0.50, 0.48]
    # H = -(0.02·log2 0.02 + 0.50·log2 0.50 + 0.48·log2 0.48) = 1.121146 bits
    clusters = [
        {"cluster_id": "auth", "kind": "goal", "size": 2},
        {"cluster_id": "core", "kind": "git_commit", "size": 50},
        {"cluster_id": "vision", "kind": "conversation", "size": 48},
    ]
    report = DEE.compute_domain_entropy(clusters=clusters)
    assert report.cluster_count == 3
    assert report.total_samples == 100
    assert abs(report.total_entropy_bits - 1.121146) < 1e-4
    # max = log2(3) = 1.584962 → normalized = 0.707366
    assert abs(report.normalized_entropy - 0.707366) < 1e-4


# === The knowledge gap: the sparsest domain is identified first =============

def test_sparse_domain_identified(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    clusters = [
        {"cluster_id": "well_known", "kind": "git_commit", "size": 80},
        {"cluster_id": "uncharted", "kind": "goal", "size": 1},   # the gap
        {"cluster_id": "moderate", "kind": "conversation", "size": 19},
    ]
    report = DEE.compute_domain_entropy(clusters=clusters)
    # Sparsest (fewest samples) ranked first.
    assert report.sparse_zones[0].cluster_id == "uncharted"
    assert report.sparse_zones[0].size == 1
    # Highest sparsity score (1 - 1/80 ≈ 0.9875).
    assert report.sparse_zones[0].sparsity_score > 0.95


def test_engine_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", raising=False)
    report = DEE.compute_domain_entropy(clusters=[{"cluster_id": "x", "size": 5}])
    assert report.master_enabled is False
    assert report.sparse_zones == ()


def test_empty_index_is_safe(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    report = DEE.compute_domain_entropy(clusters=[])
    assert report.total_samples == 0
    assert report.sparse_zones == ()


# === Recoverable budget: shrinks under load, recovers automatically =========

def test_budget_is_recoverable_function_of_load(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    monkeypatch.delenv("JARVIS_DOMAIN_ENTROPY_BASE_BUDGET", raising=False)  # default 3
    assert DEE.exploration_budget(load_report=_load("normal")) == 3
    assert DEE.exploration_budget(load_report=_load("elevated")) == 1   # 3 // 2
    assert DEE.exploration_budget(load_report=_load("overloaded")) == 0
    # Recovery is automatic (pure function of CURRENT load — no latch):
    assert DEE.exploration_budget(load_report=_load("normal")) == 3


# === The cage invariant: probes are GOVERNED ops, never direct execution ====

def test_scan_emits_only_governed_envelopes(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    clusters = [
        {"cluster_id": "big", "kind": "git_commit", "size": 90},
        {"cluster_id": "gap_a", "kind": "goal", "size": 2},
        {"cluster_id": "gap_b", "kind": "conversation", "size": 8},
    ]
    router = _FakeRouter()
    report = asyncio.run(DEE.run_curiosity_scan_once(
        router=router, clusters=clusters, load_report=_load("normal"),
    ))
    assert report.emitted >= 1
    # Every emission is a GOVERNED, cage-eligible envelope — NOT a direct probe.
    for env in router.ingested:
        assert env.source == "exploration"
        assert env.urgency == "low"          # sheddable → cognitive-shed can throttle
        assert env.requires_human_ack is False
    # The sparsest zone was scheduled for exploration.
    descs = " ".join(e.description for e in router.ingested)
    assert "gap_a" in descs


def test_overload_throttles_to_zero_emission(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    clusters = [
        {"cluster_id": "big", "kind": "git_commit", "size": 90},
        {"cluster_id": "gap", "kind": "goal", "size": 2},
    ]
    router = _FakeRouter()
    report = asyncio.run(DEE.run_curiosity_scan_once(
        router=router, clusters=clusters, load_report=_load("overloaded"),
    ))
    assert report.budget == 0
    assert report.emitted == 0
    assert router.ingested == []   # recoverably throttled — nothing scheduled


def test_scan_inert_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", raising=False)
    router = _FakeRouter()
    report = asyncio.run(DEE.run_curiosity_scan_once(
        router=router, clusters=[{"cluster_id": "x", "size": 5}],
        load_report=_load("normal"),
    ))
    assert report.master_enabled is False
    assert report.emitted == 0
    assert router.ingested == []


def test_build_envelopes_pure_no_router_needed(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    report = DEE.compute_domain_entropy(clusters=[
        {"cluster_id": "a", "kind": "goal", "size": 3},
        {"cluster_id": "b", "kind": "git_commit", "size": 60},
    ])
    envs = DEE.build_exploration_envelopes(report, budget=1)
    assert len(envs) == 1
    assert envs[0].source == "exploration"
    assert envs[0].urgency == "low"
