"""Part B graduation root-cause regression — cache-reload cluster rehydration.

The npz cache (``_persist_cache_safe``) stores vectors/corpus/centroid but NO
cluster state. Before 2026-07-18, ``_load_from_cache`` restored symmetrically
and left ``self._clusters = []`` AND ``self._stats.clusters = ()`` — so every
cache-warm boot (the COMMON path, including the process-isolated build whose
parent reloads the worker's cache) served ``stats().clusters == ()`` forever.
The DomainEntropyEngine's ONLY data source is that cluster distribution, so the
proactive-curiosity stack was structurally dark on warm boots.

These tests pin the fix end-to-end:
  1. persist → fresh index → ``_load_from_cache`` → internal ``_clusters``
     non-empty (same auto-K k-means machinery as build);
  2. ``stats().clusters`` mirrors them (the SEPARATE ``self._stats`` record —
     the seam consumers actually read);
  3. the full consumer chain: ``domain_entropy_engine._load_clusters()`` sees
     the rehydrated set and ``compute_domain_entropy`` produces a live report;
  4. POSTMORTEM exclusion is honored on the rehydration path exactly as on
     build (failure-gravity avoidance);
  5. ``cluster_mode=centroid`` skips rehydration (legacy behavior intact).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import List, Sequence

import pytest

np = pytest.importorskip("numpy")

from backend.core.ouroboros.governance import semantic_index as si


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_semantic_index.py discipline)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env_and_singletons(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_SEMANTIC_"):
            monkeypatch.delenv(key, raising=False)
    si.reset_default_index()
    yield
    si.reset_default_index()


def _fake_vec(text: str, dim: int = 16) -> List[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(((h[i % len(h)]) / 255.0) * 2.0) - 1.0 for i in range(dim)]


def _seed_and_persist(
    root: Path,
    *,
    n_items: int = 12,
    postmortems: int = 0,
) -> si.SemanticIndex:
    """Populate an index's corpus/vectors directly, then persist its cache."""
    idx = si.SemanticIndex(root)
    now = time.time()
    corpus: List[si.CorpusItem] = []
    vectors: List[List[float]] = []
    for i in range(n_items):
        # Two well-separated lobes so k-means finds real structure.
        base = "goal alpha frontier" if i % 2 == 0 else "commit beta repair"
        text = f"{base} item {i}"
        src = si.SOURCE_GOAL if i % 2 == 0 else "git_commit"
        corpus.append(si.CorpusItem(
            text=text, source=src, ts=now - i * 60, halflife_days=14.0,
        ))
        vec = _fake_vec(base, dim=16)  # identical within a lobe
        vec[0] += i * 1e-3             # tiny jitter, keeps lobes separable
        vectors.append(vec)
    for j in range(postmortems):
        corpus.append(si.CorpusItem(
            text=f"postmortem failure {j}", source=si.SOURCE_POSTMORTEM,
            ts=now, halflife_days=14.0,
        ))
        vectors.append(_fake_vec(f"postmortem failure {j}", dim=16))
    with idx._lock:
        idx._corpus = corpus
        idx._vectors = vectors
        idx._centroid = [
            sum(col) / len(vectors) for col in zip(*vectors)
        ]
        idx._built_at = now
    idx._persist_cache_safe()
    assert (root / ".jarvis" / "semantic_index.npz").exists()
    return idx


# ---------------------------------------------------------------------------
# (1) + (2) — reload rehydrates both the internal set and the stats mirror
# ---------------------------------------------------------------------------


def test_cache_reload_rehydrates_clusters_and_stats(tmp_path):
    _seed_and_persist(tmp_path)

    fresh = si.SemanticIndex(tmp_path)
    assert fresh._clusters == []            # pre-reload: structurally dark
    assert fresh._load_from_cache() is True

    # Internal cluster set (scoring paths).
    assert len(fresh._clusters) >= 1
    assert len(fresh._cluster_labels) == len(fresh._corpus_centroid_members)

    # The stats mirror — the seam every external consumer reads. This is the
    # exact assertion that failed before the fix ("_clusters direct: 2 /
    # stats().clusters: 0").
    stats = fresh.stats()
    assert stats.cluster_mode == "kmeans"
    assert stats.cluster_count == len(fresh._clusters)
    assert len(stats.clusters) == len(fresh._clusters)
    for summary in stats.clusters:
        assert "size" in summary and summary["size"] >= 1
        assert "kind" in summary and "cluster_id" in summary


def test_cache_reload_kmeans_telemetry_mirrored(tmp_path):
    _seed_and_persist(tmp_path)
    fresh = si.SemanticIndex(tmp_path)
    assert fresh._load_from_cache() is True
    stats = fresh.stats()
    # Telemetry fields come from the SAME _last_cluster_telemetry the build
    # path fills — inertia/iters are run-dependent, but a successful k-means
    # run always reports a non-negative inertia and >=1 iteration.
    assert stats.kmeans_inertia >= 0.0
    assert stats.kmeans_iter_count >= 1


# ---------------------------------------------------------------------------
# (3) — full consumer chain: DomainEntropyEngine sees the rehydrated zones
# ---------------------------------------------------------------------------


def test_entropy_engine_consumes_rehydrated_clusters(tmp_path, monkeypatch):
    _seed_and_persist(tmp_path)
    fresh = si.SemanticIndex(tmp_path)
    assert fresh._load_from_cache() is True
    # The engine imports get_default_index at call time — route it to ours.
    monkeypatch.setattr(si, "get_default_index", lambda *a, **k: fresh)

    from backend.core.ouroboros.governance import domain_entropy_engine as dee

    loaded: Sequence = dee._load_clusters()
    assert len(loaded) == len(fresh._clusters)

    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "true")
    report = dee.compute_domain_entropy(clusters=loaded)
    assert report.cluster_count == len(loaded)
    # With >=2 clusters there is real distribution entropy + ranked zones.
    if len(loaded) >= 2:
        assert report.total_entropy_bits > 0.0
        assert len(report.sparse_zones) >= 1


# ---------------------------------------------------------------------------
# (4) — POSTMORTEM exclusion honored on the rehydration path
# ---------------------------------------------------------------------------


def test_rehydration_excludes_postmortem_like_build(tmp_path):
    _seed_and_persist(tmp_path, n_items=10, postmortems=4)
    fresh = si.SemanticIndex(tmp_path)
    assert fresh._load_from_cache() is True
    # The full corpus (incl. postmortems) is restored...
    assert len(fresh._corpus) == 14
    # ...but the cluster subset excludes SOURCE_POSTMORTEM by default.
    assert len(fresh._corpus_centroid_members) == 10
    assert all(
        it.source != si.SOURCE_POSTMORTEM
        for it in fresh._corpus_centroid_members
    )


# ---------------------------------------------------------------------------
# (5) — centroid mode skips rehydration entirely (legacy path intact)
# ---------------------------------------------------------------------------


def test_centroid_mode_skips_rehydration(tmp_path, monkeypatch):
    _seed_and_persist(tmp_path)
    monkeypatch.setenv("JARVIS_SEMANTIC_INDEX_CLUSTER_MODE", "centroid")
    fresh = si.SemanticIndex(tmp_path)
    assert fresh._load_from_cache() is True
    assert fresh._clusters == []
    assert fresh.stats().clusters == () or list(fresh.stats().clusters) == []
    # But the core reload still worked.
    assert len(fresh._corpus) == 12
    assert fresh._centroid


# ---------------------------------------------------------------------------
# (6) — rehydration failure NEVER breaks the reload (fail-open enhancement)
# ---------------------------------------------------------------------------


def test_rehydration_failure_does_not_break_reload(tmp_path, monkeypatch):
    _seed_and_persist(tmp_path)
    fresh = si.SemanticIndex(tmp_path)
    monkeypatch.setattr(
        fresh, "_compute_clusters_for_build",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=True,
    )
    assert fresh._load_from_cache() is True   # reload still succeeds
    assert fresh._clusters == []              # graceful centroid-only degrade
    assert len(fresh._corpus) == 12
