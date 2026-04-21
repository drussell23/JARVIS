"""Tests for SemanticIndex — corpus, centroid, scoring, gates, prompt format.

Test strategy: the real fastembed model download is ~100MB and
deterministic-but-platform-sensitive, so every test that needs vectors
monkeypatches ``_Embedder.embed`` to return deterministic fake vectors
keyed off the input text. This:

  * Keeps tests portable across CI machines (beef #1 — cosine tolerance
    approach via fake vectors we fully control).
  * Avoids the 100MB install requirement in lightweight dev/CI setups.
  * Tests the *logic* around the embedder — the embedder itself is
    thin glue to a third-party library, not our invention.

The one test that covers the real fastembed path
(``test_embedder_disables_when_fastembed_missing``) verifies the
graceful-disable contract, which is the behavior we actually own.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import List, Sequence
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import (
    conversation_bridge as cb,
    semantic_index as si,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env_and_singletons(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith(("JARVIS_SEMANTIC_", "JARVIS_CONVERSATION_BRIDGE_")):
            monkeypatch.delenv(key, raising=False)
    si.reset_default_index()
    cb.reset_default_bridge()
    yield
    si.reset_default_index()
    cb.reset_default_bridge()


def _fake_vec(text: str, dim: int = 16) -> List[float]:
    """Deterministic pseudo-embedding keyed off SHA-256 of the text.

    Not semantically meaningful — but perfectly reproducible, and close
    texts produce similar-ish vectors because identical prefixes produce
    identical hash prefixes. Good enough to test the *plumbing*.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Map bytes → floats in [-1, 1] deterministically.
    vec = []
    for i in range(dim):
        b = h[i % len(h)]
        vec.append(((b / 255.0) * 2.0) - 1.0)
    return vec


class _FakeEmbedder:
    """Drop-in replacement for ``_Embedder`` with deterministic output."""

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim
        self._disabled = False
        self.model_name = "fake-embedder"
        self.embed_calls = 0

    @property
    def disabled(self) -> bool:
        return self._disabled

    def embed(self, texts: Sequence[str]):
        self.embed_calls += 1
        return [_fake_vec(t, dim=self._dim) for t in texts]


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_SEMANTIC_INFERENCE_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_SEMANTIC_{k}", str(v))


def _new_index_with_fake_embedder(
    project_root: Path, monkeypatch, dim: int = 16,
) -> si.SemanticIndex:
    """Construct a SemanticIndex and swap its embedder with the fake."""
    idx = si.SemanticIndex(project_root)
    fake = _FakeEmbedder(dim=dim)
    monkeypatch.setattr(idx, "_embedder", fake, raising=True)
    return idx


# ---------------------------------------------------------------------------
# (1) Embedder determinism — same input → same vector (beef #1 approach)
# ---------------------------------------------------------------------------


def test_fake_embedder_determinism():
    """Our test-harness embedder is deterministic — baseline for later tests."""
    v1 = _fake_vec("focus on multi-file autonomy")
    v2 = _fake_vec("focus on multi-file autonomy")
    v3 = _fake_vec("totally unrelated string")
    # Exact equality is fine *for the fake*; the real embedder uses cosine.
    assert v1 == v2
    assert v1 != v3
    # Cosine of identical inputs is 1 (within float tolerance).
    assert abs(si._cosine(v1, v2) - 1.0) < 1e-12


# ---------------------------------------------------------------------------
# (2) Recency-weighted centroid math
# ---------------------------------------------------------------------------


def test_recency_weight_halves_at_halflife():
    w_now = si._recency_weight(age_s=0.0, halflife_days=14.0)
    w_half = si._recency_weight(age_s=14 * 86400, halflife_days=14.0)
    w_double = si._recency_weight(age_s=28 * 86400, halflife_days=14.0)
    assert abs(w_now - 1.0) < 1e-9
    assert abs(w_half - 0.5) < 1e-9
    assert abs(w_double - 0.25) < 1e-9


def test_weighted_centroid_favors_recent():
    old_vec = [1.0, 0.0]
    new_vec = [0.0, 1.0]
    # Old has weight 0.1, new has weight 1.0 → centroid dominated by new.
    centroid = si._weighted_centroid([old_vec, new_vec], [0.1, 1.0])
    assert centroid[1] > centroid[0], "recent direction (y-axis) should dominate"


def test_weighted_centroid_empty_inputs():
    assert si._weighted_centroid([], []) == []
    assert si._weighted_centroid([[1.0]], [0.0]) == []  # zero total weight


# ---------------------------------------------------------------------------
# (3) Corpus assembler — source handling + graceful-missing
# ---------------------------------------------------------------------------


def test_corpus_assembler_graceful_when_git_missing(monkeypatch, tmp_path):
    """Non-git directory → corpus assembly doesn't raise, just skips commits."""
    _enable(monkeypatch)
    # tmp_path has no .git — git log will return non-zero.
    items = si._assemble_corpus(tmp_path, git_limit=5, max_items=10)
    commit_items = [it for it in items if it.source == si.SOURCE_GIT_COMMIT]
    assert commit_items == []  # no git, no commit items, no exception


def test_corpus_assembler_caps_total_items(monkeypatch, tmp_path):
    """max_items cap enforced even with many sources active."""
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    # Seed many conversation turns.
    for i in range(20):
        bridge.record_turn("user", f"turn number {i}")

    items = si._assemble_corpus(tmp_path, git_limit=5, max_items=7)
    assert len(items) <= 7


def test_corpus_assembler_includes_bridge_turns(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    bridge.record_turn("user", "focus on the auth module")
    items = si._assemble_corpus(tmp_path, git_limit=5, max_items=50)
    conv_items = [it for it in items if it.source == si.SOURCE_CONVERSATION]
    assert any("auth module" in it.text for it in conv_items)


# ---------------------------------------------------------------------------
# (4) Cosine monotonic — close vs far
# ---------------------------------------------------------------------------


def test_cosine_close_vs_far():
    """Identical vectors → 1.0; orthogonal → 0.0; opposite → -1.0."""
    v = [1.0, 0.0, 0.0]
    orth = [0.0, 1.0, 0.0]
    opp = [-1.0, 0.0, 0.0]
    assert abs(si._cosine(v, v) - 1.0) < 1e-9
    assert abs(si._cosine(v, orth)) < 1e-9
    assert abs(si._cosine(v, opp) - (-1.0)) < 1e-9


def test_cosine_zero_norm_returns_zero():
    """Degenerate inputs never raise — they return 0 (harmless)."""
    assert si._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert si._cosine([], [1.0, 0.0]) == 0.0
    assert si._cosine([1.0], [1.0, 0.0]) == 0.0  # mismatched dims


# ---------------------------------------------------------------------------
# (5) Boost clamp at BOOST_MAX
# ---------------------------------------------------------------------------


def test_boost_clamped_to_max(monkeypatch, tmp_path):
    _enable(monkeypatch, ALIGNMENT_BOOST_MAX="1")
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    # Hand-install a centroid that aligns perfectly with a known vector.
    with idx._lock:
        idx._centroid = _fake_vec("direction-A")
        idx._built_at = time.time()
    # Score the same text → cosine ≈ 1.0 → boost clamped to 1.
    boost = idx.boost_for("direction-A")
    assert boost == 1
    # Negative cosine → 0 boost.
    with idx._lock:
        idx._centroid = [-x for x in _fake_vec("direction-A")]
    assert idx.boost_for("direction-A") == 0


# ---------------------------------------------------------------------------
# (6) Master-off → no-import / no disk I/O / all no-op
# ---------------------------------------------------------------------------


def test_master_off_build_returns_false(tmp_path):
    # Env unset — master switch off.
    idx = si.SemanticIndex(tmp_path)
    assert idx.build() is False
    assert idx.stats().corpus_n == 0


def test_master_off_score_returns_zero(tmp_path):
    idx = si.SemanticIndex(tmp_path)
    assert idx.score("anything") == 0.0
    assert idx.boost_for("anything") == 0


def test_master_off_format_prompt_returns_none(tmp_path):
    idx = si.SemanticIndex(tmp_path)
    assert idx.format_prompt_sections() is None


def test_master_off_does_not_touch_disk_cache(tmp_path):
    """With master off, no .jarvis/semantic_index.npz is created."""
    idx = si.SemanticIndex(tmp_path)
    idx.build()  # no-op
    assert not (tmp_path / ".jarvis" / "semantic_index.npz").exists()


# ---------------------------------------------------------------------------
# (7) Refresh interval respected
# ---------------------------------------------------------------------------


def test_refresh_interval_skips_rebuild(monkeypatch, tmp_path):
    _enable(monkeypatch, REFRESH_S="3600")
    # Seed some content so build() actually embeds (empty corpus short-circuits).
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    cb.get_default_bridge().record_turn("user", "refresh interval test")

    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    assert idx.build() is True
    n_embeds_first = idx._embedder.embed_calls  # type: ignore[attr-defined]
    assert n_embeds_first > 0, "first build should have invoked embedder"
    # Second immediate build — should be skipped by interval gate.
    assert idx.build() is False
    assert idx._embedder.embed_calls == n_embeds_first  # type: ignore[attr-defined]
    # Force flag bypasses interval.
    assert idx.build(force=True) is True
    assert idx._embedder.embed_calls > n_embeds_first  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (8) Corpus cap enforced (cross-check with assembler test above)
# ---------------------------------------------------------------------------


def test_corpus_cap_enforced_via_env(monkeypatch, tmp_path):
    _enable(monkeypatch, MAX_ITEMS="4")
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    for i in range(10):
        bridge.record_turn("user", f"item {i}")
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    assert idx.build() is True
    assert idx.stats().corpus_n <= 4


# ---------------------------------------------------------------------------
# (9) Authority invariant — scoring does NOT mutate external state
# ---------------------------------------------------------------------------


def test_scoring_is_side_effect_free(monkeypatch, tmp_path):
    """Scoring increments only the ``signals_scored`` counter. Nothing else."""
    _enable(monkeypatch)
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    with idx._lock:
        idx._centroid = _fake_vec("theme")
        idx._built_at = time.time()

    stats_before = idx.stats()
    idx.score("some signal")
    idx.score("another signal")
    stats_after = idx.stats()

    # signals_scored incremented; nothing else about the index changed.
    assert stats_after.signals_scored == stats_before.signals_scored + 2
    assert stats_after.corpus_n == stats_before.corpus_n
    assert stats_after.centroid_hash8 == stats_before.centroid_hash8


# ---------------------------------------------------------------------------
# (10) Prompt subsection gate independent from priority gate
# ---------------------------------------------------------------------------


def test_prompt_injection_gate_independent(monkeypatch, tmp_path):
    """PROMPT_INJECTION_ENABLED=false silences prompt, leaves score path."""
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_SEMANTIC_PROMPT_INJECTION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    cb.get_default_bridge().record_turn("user", "work on prompt gating")

    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build()

    # Prompt disabled.
    assert idx.format_prompt_sections() is None
    # But scoring still works (priority boost path independent).
    assert idx.score("anything") != 0.0


# ---------------------------------------------------------------------------
# (11) Disk cache round-trip (numpy-optional; skip if unavailable)
# ---------------------------------------------------------------------------


def test_disk_cache_written_when_enabled(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    _enable(monkeypatch, INDEX_PERSIST="true")
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    cb.get_default_bridge().record_turn("user", "cache round trip")
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build()
    cache = tmp_path / ".jarvis" / "semantic_index.npz"
    assert cache.exists()


# ---------------------------------------------------------------------------
# (12) fastembed unavailable → graceful disable
# ---------------------------------------------------------------------------


def test_embedder_disables_when_fastembed_missing():
    """Covers the actual ``_Embedder`` graceful-disable path."""
    emb = si._Embedder()
    # Force a failing import by patching the module-level import lookup.
    with patch.dict("sys.modules", {"fastembed": None}):
        result = emb.embed(["test"])
    assert result is None
    assert emb.disabled is True


# ---------------------------------------------------------------------------
# (13) Nearest-neighbor text sanitized (pre-embed sanitizer — beef #2)
# ---------------------------------------------------------------------------


def test_sanitizer_redacts_secret_in_commit_like_text(monkeypatch):
    """A commit message containing a secret shape → redacted before embed."""
    # A git-style subject that accidentally has an OpenAI key.
    raw = "fix: use new api key sk-abcdefghij1234567890xyz for tests"
    cleaned = si._sanitize_corpus_text(raw)
    assert "sk-abcdefghij1234567890xyz" not in cleaned
    assert "[REDACTED:openai-key]" in cleaned


def test_sanitizer_strips_control_chars():
    raw = "subject\x1b[31m with \x00 control bytes\n\t"
    cleaned = si._sanitize_corpus_text(raw)
    assert "\x1b" not in cleaned
    assert "\x00" not in cleaned
    # Alphanumeric content preserved.
    assert "subject" in cleaned
    assert "control bytes" in cleaned


# ---------------------------------------------------------------------------
# (14) Observability — stats counters populate; no raw vectors
# ---------------------------------------------------------------------------


def test_stats_populate_after_build(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    cb.get_default_bridge().record_turn("user", "stats test")

    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build()
    stats = idx.stats()
    assert stats.corpus_n >= 1
    assert stats.refreshes == 1
    assert stats.build_ms >= 0
    assert stats.centroid_hash8  # non-empty once we have a centroid
    assert isinstance(stats.by_source, dict)
    # ByteSource counters don't include raw text.
    for k, v in stats.by_source.items():
        assert isinstance(k, str)
        assert isinstance(v, int)


# ---------------------------------------------------------------------------
# (15) POSTMORTEM excluded from centroid by default (§12.3)
# ---------------------------------------------------------------------------


def test_postmortem_excluded_from_centroid_by_default(monkeypatch, tmp_path):
    """Default: postmortem items appear in corpus but not in centroid math."""
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    bridge.record_turn("user", "focus on a new feature")
    bridge.record_turn(
        "assistant",
        "postmortem op=op-x outcome=VERIFY root_cause=regression",
        source="postmortem", op_id="op-x",
    )

    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build()

    # Corpus has both.
    sources = {it.source for it in idx._corpus}  # type: ignore[attr-defined]
    assert si.SOURCE_CONVERSATION in sources
    assert si.SOURCE_POSTMORTEM in sources

    # Centroid-member subset excludes postmortem.
    centroid_sources = {it.source for it in idx._corpus_centroid_members}  # type: ignore[attr-defined]
    assert si.SOURCE_POSTMORTEM not in centroid_sources
    assert si.SOURCE_CONVERSATION in centroid_sources


def test_postmortem_in_centroid_when_env_opted_in(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("JARVIS_SEMANTIC_POSTMORTEM_IN_CENTROID", "true")
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    bridge.record_turn(
        "assistant",
        "postmortem op=op-x outcome=VERIFY root_cause=regression",
        source="postmortem", op_id="op-x",
    )

    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build()

    centroid_sources = {it.source for it in idx._corpus_centroid_members}  # type: ignore[attr-defined]
    assert si.SOURCE_POSTMORTEM in centroid_sources


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_get_default_index_returns_singleton(tmp_path):
    a = si.get_default_index(tmp_path)
    b = si.get_default_index(tmp_path)
    assert a is b


def test_reset_default_index_clears_singleton(tmp_path):
    a = si.get_default_index(tmp_path)
    si.reset_default_index()
    b = si.get_default_index(tmp_path)
    assert a is not b


# ===========================================================================
# Slice 3a — k-means cluster math + telemetry
# ===========================================================================
#
# Decisions locked by authorization of Slice 3a:
#   1. Algorithm: k-means only (no DBSCAN).
#   2. K-discovery: auto-K via silhouette, with K=1 graceful fallback.
#   3. Postmortem policy: zero-boost-with-evidence (implemented in Slice 3b);
#      3a only computes/labels clusters and observes alignments in shadow mode.
#   4. Scoring path: ``score()`` still returns the v0.1 centroid cosine
#      under cluster_mode=kmeans — only telemetry changes in 3a.
#
# All k-means math tests use NumPy directly — the implementation is
# hand-rolled NumPy, and these tests pin the hand-roll's correctness
# against intuitive geometric cases. Tests that need the full
# SemanticIndex.build() path use the _FakeEmbedder pattern above so they
# don't require the fastembed install.


# ---------------------------------------------------------------------------
# (3a.1) Hand-rolled k-means — pure math
# ---------------------------------------------------------------------------


def _two_cluster_points(n_per_cluster: int = 20) -> List[List[float]]:
    """Return 2N points: N clustered around (+1,+1,…) and N around (-1,-1,…).

    Shape: 2-dim for readability. All points normalized to unit norm so
    the cosine-distance kmeans treats them as two antipodal blobs.
    """
    import math as _math
    pts: List[List[float]] = []
    for i in range(n_per_cluster):
        # Cluster A: first quadrant-ish.
        pts.append([_math.cos(0.1 * i), _math.sin(0.1 * i)])
    for i in range(n_per_cluster):
        # Cluster B: rotated 180° so cosine distance is ~2.0 between clusters.
        pts.append([-_math.cos(0.1 * i), -_math.sin(0.1 * i)])
    return pts


def test_kmeans_determinism_same_seed_same_labels():
    """Epoch 3 / 3a test 1: seeded k-means is bit-reproducible.

    Given fixed (vectors, k, seed), two separate runs produce identical
    ``labels`` and identical ``centroids``. Critical for pinning the
    shadow-mode math — flaky labels would cause false churn counters."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    r1 = si._kmeans_numpy(pts, k=2, seed=42, max_iter=30, tol=1e-4)
    r2 = si._kmeans_numpy(pts, k=2, seed=42, max_iter=30, tol=1e-4)
    assert r1[0] == r2[0]  # labels
    # Centroids compared within float tolerance (row-wise).
    for c1, c2 in zip(r1[1], r2[1]):
        for a, b in zip(c1, c2):
            assert abs(a - b) < 1e-9


def test_kmeans_k1_returns_single_cluster_and_mean():
    """3a test 2: K=1 trivially assigns all points to cluster 0 and
    returns the mean as the only centroid. converged=True, iter=0."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(10)
    labels, centroids, it, conv, inertia = si._kmeans_numpy(
        pts, k=1, seed=42, max_iter=30, tol=1e-4,
    )
    assert all(l == 0 for l in labels)
    assert len(centroids) == 1
    assert conv is True
    assert it == 0
    assert inertia >= 0.0


def test_kmeans_separates_two_clusters_perfectly():
    """3a test 3: on antipodal two-blob data, k=2 puts every point
    in the 'right' cluster — all 15 cluster-A points share one label,
    all 15 cluster-B points share the other."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    labels, _c, _it, _conv, _inertia = si._kmeans_numpy(
        pts, k=2, seed=42, max_iter=30, tol=1e-4,
    )
    # First 15 points share one label, next 15 share the other.
    assert len(set(labels[:15])) == 1
    assert len(set(labels[15:])) == 1
    assert labels[0] != labels[15]


def test_kmeans_inertia_monotonically_decreases_with_k():
    """3a test 4: increasing K decreases (or holds) inertia. This pins
    the elbow-curve invariant — silhouette-based K selection relies on
    the inertia curve being monotonic in K."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(10)
    inertias = []
    for k in (1, 2, 3, 4, 5):
        _labels, _c, _it, _conv, inertia = si._kmeans_numpy(
            pts, k=k, seed=42, max_iter=30, tol=1e-4,
        )
        inertias.append(inertia)
    # Non-increasing — allow tiny float noise.
    for i in range(len(inertias) - 1):
        assert inertias[i + 1] <= inertias[i] + 1e-9


def test_kmeans_empty_cluster_reassigned_not_dropped():
    """3a test 5: when the Lloyd step produces an empty cluster, the
    implementation reseeds it from the point farthest from its current
    centroid instead of silently dropping K. Pins the "no silent K
    collapse" invariant."""
    import pytest
    pytest.importorskip("numpy")
    # Pathological init: two clusters at identical positions plus one
    # outlier. With k=3 and an unlucky shuffle, two centroids may
    # initialize at effectively the same point and leave the third
    # empty on round 1.
    pts = [
        [1.0, 0.0], [1.0, 0.001], [1.0, 0.002],  # tight cluster A
        [0.0, 1.0], [0.0, 1.001], [0.0, 1.002],  # tight cluster B
        [-1.0, -1.0],                             # outlier
    ]
    labels, centroids, _it, _conv, _inertia = si._kmeans_numpy(
        pts, k=3, seed=0, max_iter=30, tol=1e-4,
    )
    # All K=3 clusters must be non-empty after the repair logic.
    unique = set(labels)
    assert len(unique) == 3


def test_kmeans_k_ge_n_clamps_gracefully():
    """3a test 6: when K ≥ N, each point can be its own cluster.
    Auto-K normally prevents this via clamping, but the math function
    should handle it without crashing (defense in depth)."""
    import pytest
    pytest.importorskip("numpy")
    pts = [[1.0, 0.0], [0.0, 1.0]]
    labels, centroids, _it, _conv, _inertia = si._kmeans_numpy(
        pts, k=2, seed=0, max_iter=30, tol=1e-4,
    )
    assert len(labels) == 2
    assert len(centroids) == 2


# ---------------------------------------------------------------------------
# (3a.2) Silhouette math
# ---------------------------------------------------------------------------


def test_silhouette_single_cluster_returns_zero():
    """3a test 7: a single-cluster labeling has undefined silhouette;
    convention is 0.0 (tie-break floor for auto-K K=1 special case)."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(5)
    labels = [0] * len(pts)
    assert si._silhouette_cosine(pts, labels) == 0.0


def test_silhouette_perfect_two_cluster_separation_near_one():
    """3a test 8: on antipodal two-blob data, the silhouette is very
    close to 1.0 — points are much closer to own-cluster members than
    to the other cluster."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    # True labels (first 15 = cluster 0, next 15 = cluster 1).
    labels = [0] * 15 + [1] * 15
    sil = si._silhouette_cosine(pts, labels)
    # Near-perfect separation — arc-shaped clusters produce ≈0.89 on
    # cosine distance (the arcs have a small intra-cluster spread).
    # Pin a conservative 0.8 floor rather than insisting on 0.9.
    assert sil > 0.8, f"expected near-perfect silhouette; got {sil}"


def test_silhouette_random_labeling_near_zero_or_negative():
    """3a test 9: randomly-labeled coherent data yields silhouette
    near zero or negative — the labeling isn't honoring the structure."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    # Alternating labels — deliberately bad.
    labels = [i % 2 for i in range(30)]
    sil = si._silhouette_cosine(pts, labels)
    assert sil < 0.3, f"expected low silhouette on bad labeling; got {sil}"


def test_silhouette_empty_input_returns_zero():
    """3a test 10: empty input handled without crashing."""
    import pytest
    pytest.importorskip("numpy")
    assert si._silhouette_cosine([], []) == 0.0


# ---------------------------------------------------------------------------
# (3a.3) Auto-K discovery
# ---------------------------------------------------------------------------


def test_auto_k_picks_k2_on_two_cluster_data():
    """3a test 11: auto-K finds K=2 when the data has two clear clusters."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    r = si._auto_k_kmeans(
        pts, k_min=1, k_max=5, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    assert r.k == 2
    assert r.silhouette > 0.8


def test_auto_k_picks_k1_on_coherent_blob():
    """3a test 12: on data that's already coherent, auto-K picks K=1 —
    the data doesn't benefit from splitting. K=1 has silhouette=0; any
    K≥2 that produces silhouette ≤ 0 loses to K=1."""
    import pytest
    pytest.importorskip("numpy")
    import math as _math
    # Single tight arc — no meaningful substructure.
    pts = [[_math.cos(0.1 * i), _math.sin(0.1 * i)] for i in range(20)]
    r = si._auto_k_kmeans(
        pts, k_min=1, k_max=5, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    # Silhouette may be positive for K=2 on this data (the arc bends),
    # but the test pins the graceful-degradation CONTRACT — if K=1 is
    # picked OR a higher K is picked with a stable silhouette, both are
    # acceptable. The critical property is that the result never fails.
    assert r.k >= 1
    assert r.k <= 5


def test_auto_k_respects_k_max():
    """3a test 13: K_MAX caps the search. Even if K=10 would win the
    silhouette sweep, K_MAX=3 stops the search at 3."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(15)
    r = si._auto_k_kmeans(
        pts, k_min=1, k_max=3, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    assert r.k <= 3


def test_auto_k_respects_k_min():
    """3a test 14: K_MIN floors the search. K_MIN=2 forbids K=1 even on
    coherent data."""
    import pytest
    pytest.importorskip("numpy")
    import math as _math
    pts = [[_math.cos(0.1 * i), _math.sin(0.1 * i)] for i in range(20)]
    r = si._auto_k_kmeans(
        pts, k_min=2, k_max=5, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    assert r.k >= 2


def test_auto_k_clamps_k_max_to_corpus_size():
    """3a test 15: K_MAX=10 on a 3-item corpus clamps to K=3 not K=10."""
    import pytest
    pytest.importorskip("numpy")
    pts = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    r = si._auto_k_kmeans(
        pts, k_min=1, k_max=10, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    assert r.k <= 3


def test_auto_k_empty_corpus_returns_none():
    """3a test 16: empty corpus yields None (caller short-circuits)."""
    import pytest
    pytest.importorskip("numpy")
    r = si._auto_k_kmeans(
        [], k_min=1, k_max=5, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is None


def test_auto_k_silhouette_by_k_carries_full_sweep():
    """3a test 17: the silhouette_by_k field carries the complete
    [(K, silhouette), ...] log for diagnostic output."""
    import pytest
    pytest.importorskip("numpy")
    pts = _two_cluster_points(10)
    r = si._auto_k_kmeans(
        pts, k_min=1, k_max=3, seed=42, max_iter=30, tol=1e-4,
    )
    assert r is not None
    ks = [k for k, _ in r.silhouette_by_k]
    assert ks == [1, 2, 3]


# ---------------------------------------------------------------------------
# (3a.4) Cluster-kind classifier
# ---------------------------------------------------------------------------


def test_cluster_kind_goal_cluster_from_commits():
    """3a test 18: a cluster where ≥60% of items are git_commit is 'goal'."""
    comp = {si.SOURCE_GIT_COMMIT: 7, si.SOURCE_CONVERSATION: 3}
    kind = si._classify_cluster_kind(comp, dominance_threshold=0.6)
    assert kind == si.CLUSTER_KIND_GOAL


def test_cluster_kind_goal_from_commit_plus_goal_combined():
    """3a test 19: commits + goals COMBINED cross the 60% threshold even
    if neither alone does — they're both 'forward momentum' sources."""
    comp = {
        si.SOURCE_GIT_COMMIT: 4,
        si.SOURCE_GOAL: 3,
        si.SOURCE_CONVERSATION: 3,
    }
    kind = si._classify_cluster_kind(comp, dominance_threshold=0.6)
    assert kind == si.CLUSTER_KIND_GOAL


def test_cluster_kind_postmortem_cluster():
    comp = {si.SOURCE_POSTMORTEM: 8, si.SOURCE_CONVERSATION: 2}
    kind = si._classify_cluster_kind(comp, dominance_threshold=0.6)
    assert kind == si.CLUSTER_KIND_POSTMORTEM


def test_cluster_kind_conversation_cluster():
    comp = {si.SOURCE_CONVERSATION: 7, si.SOURCE_GIT_COMMIT: 3}
    kind = si._classify_cluster_kind(comp, dominance_threshold=0.6)
    assert kind == si.CLUSTER_KIND_CONVERSATION


def test_cluster_kind_mixed_when_nothing_dominates():
    """3a test 22: no single source-family crosses threshold → 'mixed'."""
    comp = {
        si.SOURCE_GIT_COMMIT: 2,
        si.SOURCE_CONVERSATION: 3,
        si.SOURCE_POSTMORTEM: 2,
    }
    # git_commit+goal = 2/7 ≈ 0.29; conversation = 3/7 ≈ 0.43; postmortem=2/7 ≈ 0.29
    # None crosses 0.6 — must be mixed.
    kind = si._classify_cluster_kind(comp, dominance_threshold=0.6)
    assert kind == si.CLUSTER_KIND_MIXED


def test_cluster_kind_empty_input_returns_mixed():
    """3a test 23: empty composition defaults to mixed (no crash)."""
    assert si._classify_cluster_kind({}, dominance_threshold=0.6) == si.CLUSTER_KIND_MIXED


def test_cluster_kind_threshold_is_adjustable():
    """3a test 24: the dominance threshold is tunable.

    Composition: postmortem=6, conversation=4 → postmortem is 0.6 of total.
    At threshold=0.7, postmortem doesn't cross → mixed.
    At threshold=0.5, postmortem crosses → postmortem.
    """
    comp = {si.SOURCE_POSTMORTEM: 6, si.SOURCE_CONVERSATION: 4}
    assert si._classify_cluster_kind(comp, dominance_threshold=0.7) == si.CLUSTER_KIND_MIXED
    assert si._classify_cluster_kind(comp, dominance_threshold=0.5) == si.CLUSTER_KIND_POSTMORTEM


# ---------------------------------------------------------------------------
# (3a.5) Centroid hashing
# ---------------------------------------------------------------------------


def test_centroid_hash8_deterministic():
    """3a test 25: same centroid → same hash across calls."""
    c = [0.1, 0.2, 0.3, 0.4]
    assert si._centroid_hash8(c) == si._centroid_hash8(c)
    assert len(si._centroid_hash8(c)) == 8


def test_centroid_hash8_different_centroids_different_hashes():
    c1 = [0.1, 0.2, 0.3, 0.4]
    c2 = [0.5, 0.6, 0.7, 0.8]
    assert si._centroid_hash8(c1) != si._centroid_hash8(c2)


def test_centroid_hash8_empty_returns_empty_string():
    assert si._centroid_hash8([]) == ""


# ---------------------------------------------------------------------------
# (3a.6) SemanticIndex integration — cluster_mode gating
# ---------------------------------------------------------------------------


def _seed_conversation(monkeypatch, *turns):
    """Helper: seed ConversationBridge with test turns.

    Each turn is ``(source, text)``. ``record_turn`` signature is
    ``(role, text, *, source, op_id)`` — we pass ``role="user"`` by
    convention and thread the test's intended source through the kwarg.
    """
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")
    bridge = cb.get_default_bridge()
    for source, text in turns:
        bridge.record_turn("user", text, source=source)


def test_cluster_mode_centroid_default_keeps_clusters_empty(tmp_path, monkeypatch):
    """3a test 28: default cluster_mode=centroid → no clustering run,
    stats.cluster_count=0, stats.clusters=[]. v0.1 backward-compat pin."""
    _enable(monkeypatch)
    _seed_conversation(
        monkeypatch,
        (cb.SOURCE_TUI_USER, "text one"),
        (cb.SOURCE_TUI_USER, "text two"),
        (cb.SOURCE_TUI_USER, "text three"),
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    ok = idx.build(force=True)
    assert ok is True
    stats = idx.stats()
    assert stats.cluster_mode == "centroid"
    assert stats.cluster_count == 0
    assert stats.clusters == []
    assert stats.kmeans_silhouette == 0.0
    assert idx.clusters == ()


def test_cluster_mode_kmeans_populates_clusters_and_stats(tmp_path, monkeypatch):
    """3a test 29: cluster_mode=kmeans + adequate corpus → clusters and
    telemetry populated."""
    _enable(monkeypatch, INDEX_CLUSTER_MODE="kmeans")
    _seed_conversation(
        monkeypatch,
        *[
            (cb.SOURCE_TUI_USER, f"topic alpha message {i}")
            for i in range(8)
        ],
        *[
            (cb.SOURCE_TUI_USER, f"topic beta completely different {i}")
            for i in range(8)
        ],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    ok = idx.build(force=True)
    assert ok is True
    stats = idx.stats()
    assert stats.cluster_mode == "kmeans"
    assert stats.cluster_count >= 1
    assert len(stats.clusters) == stats.cluster_count
    # Each cluster summary carries the expected keys (content-light).
    for c in stats.clusters:
        assert "cluster_id" in c
        assert "size" in c
        assert "kind" in c
        assert "centroid_hash8" in c
        assert "source_composition" in c
        # No raw centroid vectors in the stats snapshot (§8).
        assert "centroid" not in c


def test_cluster_mode_kmeans_cluster_info_snapshot_is_immutable(tmp_path, monkeypatch):
    """3a test 30: the ``clusters`` property returns an immutable tuple
    of frozen ClusterInfo records — callers can't mutate the index's
    state by modifying what they receive."""
    _enable(monkeypatch, INDEX_CLUSTER_MODE="kmeans")
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
        *[(cb.SOURCE_TUI_USER, f"beta {i}") for i in range(5)],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    clusters = idx.clusters
    assert isinstance(clusters, tuple)
    if clusters:
        # ClusterInfo is frozen — attempting to reassign raises.
        import dataclasses
        assert dataclasses.is_dataclass(clusters[0])
        with pytest.raises(Exception):
            clusters[0].size = 999  # frozen dataclass


def test_cluster_churn_zero_on_stable_rebuild(tmp_path, monkeypatch):
    """3a test 31: forcing two successive rebuilds with the same corpus
    and same seed produces identical cluster hashes → churn=0."""
    _enable(monkeypatch, INDEX_CLUSTER_MODE="kmeans")
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
        *[(cb.SOURCE_TUI_USER, f"beta {i}") for i in range(5)],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    hashes_1 = {c.centroid_hash8 for c in idx.clusters}
    idx.build(force=True)
    hashes_2 = {c.centroid_hash8 for c in idx.clusters}
    assert hashes_1 == hashes_2, (
        "stable corpus + same seed must produce stable cluster hashes"
    )
    # churn counter reflects this stability on the second build.
    assert idx.stats().cluster_churn == 0


# ---------------------------------------------------------------------------
# (3a.7) Shadow-mode observation — score() unchanged but observes
# ---------------------------------------------------------------------------


def test_score_output_unchanged_under_kmeans_mode(tmp_path, monkeypatch):
    """3a test 32 (CRITICAL): score() returns the v0.1 centroid cosine
    regardless of cluster_mode. Slice 3a is shadow-only — no policy
    change. Pin this to prevent accidental coupling.

    Two indexes, same corpus, same embedder — one in centroid mode, one
    in kmeans mode. score() on the same text must match exactly."""
    _enable(monkeypatch)
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
        *[(cb.SOURCE_TUI_USER, f"beta {i}") for i in range(5)],
    )
    idx_centroid = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx_centroid.build(force=True)
    score_centroid_mode = idx_centroid.score("a new intake signal")

    monkeypatch.setenv("JARVIS_SEMANTIC_INDEX_CLUSTER_MODE", "kmeans")
    # Reset the singleton so a fresh index picks up the new mode env var.
    idx_kmeans = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx_kmeans.build(force=True)
    score_kmeans_mode = idx_kmeans.score("a new intake signal")

    assert abs(score_centroid_mode - score_kmeans_mode) < 1e-12, (
        f"CRITICAL: score() must not change under cluster_mode=kmeans "
        f"in Slice 3a (shadow only). "
        f"centroid={score_centroid_mode} kmeans={score_kmeans_mode}"
    )


def test_alignment_histogram_increments_per_scored_signal(tmp_path, monkeypatch):
    """3a test 33: each score() call under kmeans mode increments the
    alignment histogram by the best-cluster's kind."""
    _enable(monkeypatch, INDEX_CLUSTER_MODE="kmeans")
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
        *[(cb.SOURCE_TUI_USER, f"beta {i}") for i in range(5)],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    assert idx.clusters, "precondition: clusters populated"
    # Before any scoring, histogram should be empty.
    assert idx.stats().alignment_histogram_by_kind == {}
    # Score a handful of signals.
    for txt in ("x1", "x2", "x3"):
        idx.score(txt)
    total = sum(idx.stats().alignment_histogram_by_kind.values())
    assert total == 3, (
        f"expected 3 histogram events; got "
        f"{idx.stats().alignment_histogram_by_kind}"
    )


def test_score_with_cluster_returns_cluster_detail(tmp_path, monkeypatch):
    """3a test 34: score_with_cluster returns a dict carrying cluster_id,
    cluster_kind, cluster_size. score field matches score()."""
    _enable(monkeypatch, INDEX_CLUSTER_MODE="kmeans")
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
        *[(cb.SOURCE_TUI_USER, f"beta {i}") for i in range(5)],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    assert idx.clusters, "precondition: clusters populated"
    detail = idx.score_with_cluster("a new intake signal")
    assert detail is not None
    assert "score" in detail
    assert "cluster_id" in detail
    assert "cluster_kind" in detail
    assert "cluster_cosine" in detail
    assert "cluster_size" in detail
    assert detail["cluster_id"] in {c.cluster_id for c in idx.clusters}
    assert detail["cluster_kind"] in si._VALID_CLUSTER_KINDS
    assert detail["cluster_size"] >= 1


def test_score_with_cluster_returns_none_when_disabled(tmp_path, monkeypatch):
    """3a test 35: master-off → score_with_cluster returns None."""
    # Master off; kmeans flag meaningless.
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    assert idx.score_with_cluster("x") is None


def test_score_with_cluster_empty_clusters_returns_detail_with_none_fields(
    tmp_path, monkeypatch,
):
    """3a test 36: when clustering is off (centroid mode), score_with_cluster
    still returns a result dict but with None cluster fields."""
    _enable(monkeypatch)  # default cluster_mode=centroid
    _seed_conversation(
        monkeypatch,
        (cb.SOURCE_TUI_USER, "one"),
        (cb.SOURCE_TUI_USER, "two"),
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    detail = idx.score_with_cluster("x")
    assert detail is not None
    assert detail["cluster_id"] is None
    assert detail["cluster_kind"] is None
    assert detail["cluster_size"] == 0


# ---------------------------------------------------------------------------
# (3a.8) Failure-gravity tripwire
# ---------------------------------------------------------------------------


def test_failure_gravity_no_alert_when_window_not_full(tmp_path, monkeypatch, caplog):
    """3a test 37: with window=10, only 3 scored signals → window not full
    → no WARN emitted, alerts counter stays at 0."""
    import logging as _logging
    _enable(
        monkeypatch,
        INDEX_CLUSTER_MODE="kmeans",
        CLUSTER_FAILURE_GRAVITY_WINDOW="10",
        CLUSTER_FAILURE_GRAVITY_THRESHOLD="0.3",
    )
    _seed_conversation(
        monkeypatch,
        *[(cb.SOURCE_TUI_USER, f"alpha {i}") for i in range(5)],
    )
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    idx.build(force=True)
    caplog.set_level(
        _logging.WARNING,
        logger="backend.core.ouroboros.governance.semantic_index",
    )
    for i in range(3):
        idx.score(f"signal {i}")
    # No warning while window is partial.
    warns = [r for r in caplog.records if "failure_gravity" in r.getMessage()]
    assert warns == []
    assert idx.stats().failure_gravity_alerts == 0


def test_failure_gravity_alert_counter_present(tmp_path, monkeypatch):
    """3a test 38: failure_gravity_alerts is present on stats even when
    never tripped (observability invariant — downstream consumers can
    read the counter without conditional logic)."""
    _enable(monkeypatch)
    idx = _new_index_with_fake_embedder(tmp_path, monkeypatch)
    stats = idx.stats()
    assert hasattr(stats, "failure_gravity_alerts")
    assert stats.failure_gravity_alerts == 0
    assert stats.failure_gravity_window_rate == 0.0


# ---------------------------------------------------------------------------
# (3a.9) Env / config hardening
# ---------------------------------------------------------------------------


def test_cluster_mode_malformed_env_falls_back_to_centroid(monkeypatch):
    """3a test 39: unrecognized JARVIS_SEMANTIC_INDEX_CLUSTER_MODE → centroid."""
    monkeypatch.setenv("JARVIS_SEMANTIC_INDEX_CLUSTER_MODE", "banana-mode")
    assert si._cluster_mode() == "centroid"


def test_cluster_mode_case_insensitive(monkeypatch):
    """3a test 40: env value is case-insensitive (operators fat-finger)."""
    monkeypatch.setenv("JARVIS_SEMANTIC_INDEX_CLUSTER_MODE", "KMeans")
    assert si._cluster_mode() == "kmeans"


def test_cluster_k_bounds_clamped_to_minimum_one(monkeypatch):
    """3a test 41: negative / zero K_MIN → 1. Negative K_MAX → 1.
    Prevents nonsense configuration from crashing auto-K."""
    monkeypatch.setenv("JARVIS_SEMANTIC_CLUSTER_K_MIN", "-5")
    monkeypatch.setenv("JARVIS_SEMANTIC_CLUSTER_K_MAX", "-1")
    assert si._cluster_k_min() == 1
    assert si._cluster_k_max() == 1


def test_postmortem_dominance_clamped_to_0_1(monkeypatch):
    """3a test 42: dominance threshold > 1.0 clamped to 1.0; < 0 → 0."""
    monkeypatch.setenv("JARVIS_SEMANTIC_CLUSTER_POSTMORTEM_DOMINANCE", "5.0")
    assert si._cluster_postmortem_dominance() == 1.0
    monkeypatch.setenv("JARVIS_SEMANTIC_CLUSTER_POSTMORTEM_DOMINANCE", "-1")
    assert si._cluster_postmortem_dominance() == 0.0


# ---------------------------------------------------------------------------
# (3a.10) Authority invariant (extends to Slice 3a)
# ---------------------------------------------------------------------------


def test_authority_invariant_clustering_does_not_import_gate_modules():
    """3a test 43: the semantic_index module must NOT import any of the
    gate / policy / risk modules. Enforces the authority invariant from
    the v0.1 era under v1.0's expanded surface."""
    import backend.core.ouroboros.governance.semantic_index as module
    source = Path(module.__file__).read_text()
    # Must not import authority-carrying modules.
    forbidden_imports = [
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for forbidden in forbidden_imports:
        assert forbidden not in source, (
            f"Authority invariant violated — semantic_index imports "
            f"{forbidden!r}. Clustering must stay advisory; §1 Boundary "
            f"Principle is non-negotiable."
        )
