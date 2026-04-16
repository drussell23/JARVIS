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
