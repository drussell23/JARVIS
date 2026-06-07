"""Slice 131 Phase 3 — the Asynchronous Semantic Response Cache.

Exact-match caching (Phase 1) never hits on semantic *variations* of the same
intent. This layer closes the near-match gap by composing the dormant
``SemanticIndex`` bge-small embedder + ``_cosine`` with the Phase-1
``provider_response_cache`` (``CachedTrajectory`` + the ``repo_state_digest``
fail-closed git-diff guard) — it builds NO new vectorizer and NO new store.

Invariants under test:
  * gated ``JARVIS_SEMANTIC_CACHE_ENABLED`` default-FALSE (OFF byte-identical).
  * near-match (cosine ≥ strict threshold) → serve the cached trajectory.
  * fully async + FAIL-CLOSED: embedder error OR timeout → drop the cache
    attempt → return None → caller falls back to standard generation.
  * repo-state fail-closed EVEN on a semantic match: a near-match whose repo
    state has drifted is never served (no stale code).
  * write-through: a completed generation is embedded + pushed immediately.
  * bounded (LRU eviction); no hardcoded embedder/model string (delegates).
"""
from __future__ import annotations

import asyncio
import math
import os
import pathlib
import time
import unittest

from backend.core.ouroboros.governance import semantic_cache as SC
from backend.core.ouroboros.governance.provider_response_cache import CachedTrajectory


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _traj(key: str = "k1") -> CachedTrajectory:
    return CachedTrajectory(
        full_key=key, prefix_key="p", candidates=({"file_path": "x.py"},),
        provider_name="doubleword", model_id="m", is_noop=False,
        prompt_preloaded_files=(), total_input_tokens=10,
        total_output_tokens=20, n_bytes=100,
    )


class _FakeEmbedder:
    """Maps text→vector; unknown text → orthogonal default. Synchronous, like
    SemanticIndex's _Embedder.embed."""

    def __init__(self, mapping):
        self._m = mapping

    def embed(self, texts):
        return [self._m.get(t, [0.0, 0.0, 1.0]) for t in texts]


class _BoomEmbedder:
    def embed(self, texts):
        raise RuntimeError("vectorizer exploded")


class _SlowEmbedder:
    def embed(self, texts):
        time.sleep(0.5)
        return [[1.0, 0.0, 0.0] for _ in texts]


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_SEMANTIC_CACHE_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(SC.semantic_cache_enabled())

    def test_lookup_none_when_disabled(self):
        c = SC.SemanticResponseCache(embedder=_FakeEmbedder({"a": [1, 0, 0]}))
        _run(c.store("a", _traj(), repo_root=None, repo_digest="R"))  # store no-ops when off
        out = _run(c.lookup("a", repo_root=None, repo_digest="R"))
        self.assertIsNone(out)


class TestSemanticHitMiss(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_SEMANTIC_CACHE_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("JARVIS_SEMANTIC_CACHE_ENABLED", None)
        os.environ.pop("JARVIS_SEMANTIC_CACHE_EMBED_TIMEOUT_S", None)

    def test_near_match_hits(self):
        emb = _FakeEmbedder({"add a foo function": [1, 0, 0],
                             "please add a foo function": [1, 0, 0]})
        c = SC.SemanticResponseCache(embedder=emb)
        _run(c.store("add a foo function", _traj("hit"), None, repo_digest="R"))
        out = _run(c.lookup("please add a foo function", None, repo_digest="R"))
        self.assertIsNotNone(out)
        self.assertEqual(out.full_key, "hit")

    def test_distant_prompt_misses(self):
        emb = _FakeEmbedder({"add a foo function": [1, 0, 0],
                             "delete the database": [0, 1, 0]})
        c = SC.SemanticResponseCache(embedder=emb)
        _run(c.store("add a foo function", _traj(), None, repo_digest="R"))
        out = _run(c.lookup("delete the database", None, repo_digest="R"))
        self.assertIsNone(out)  # orthogonal → cosine 0 < threshold

    def test_boundary_threshold(self):
        # cosine([1,0,0],[0.97,x,0]) ≈ 0.97 ≥ 0.95 → hit.
        v = [0.97, math.sqrt(max(0.0, 1 - 0.97 ** 2)), 0.0]
        emb = _FakeEmbedder({"orig": [1, 0, 0], "variant": v})
        c = SC.SemanticResponseCache(embedder=emb)
        _run(c.store("orig", _traj("b"), None, repo_digest="R"))
        self.assertIsNotNone(_run(c.lookup("variant", None, repo_digest="R")))

    def test_repo_state_fail_closed_even_on_match(self):
        # Identical vector, but repo drifted → must NOT serve stale code.
        emb = _FakeEmbedder({"x": [1, 0, 0]})
        c = SC.SemanticResponseCache(embedder=emb)
        _run(c.store("x", _traj(), None, repo_digest="R_old"))
        out = _run(c.lookup("x", None, repo_digest="R_new"))
        self.assertIsNone(out)

    def test_fail_closed_on_embedder_error(self):
        c = SC.SemanticResponseCache(embedder=_BoomEmbedder())
        # store fails soft, lookup returns None — caller falls back to API.
        self.assertFalse(_run(c.store("x", _traj(), None, repo_digest="R")))
        self.assertIsNone(_run(c.lookup("x", None, repo_digest="R")))

    def test_fail_closed_on_embedder_timeout(self):
        os.environ["JARVIS_SEMANTIC_CACHE_EMBED_TIMEOUT_S"] = "0.05"
        c = SC.SemanticResponseCache(embedder=_SlowEmbedder())
        self.assertIsNone(_run(c.lookup("x", None, repo_digest="R")))

    def test_write_through_then_hit(self):
        emb = _FakeEmbedder({"q": [1, 0, 0]})
        c = SC.SemanticResponseCache(embedder=emb)
        self.assertTrue(_run(c.store("q", _traj("wt"), None, repo_digest="R")))
        out = _run(c.lookup("q", None, repo_digest="R"))
        self.assertEqual(out.full_key, "wt")

    def test_bounded_eviction(self):
        emb = _FakeEmbedder({"a": [1, 0, 0], "b": [0, 1, 0], "c": [0, 0, 1]})
        c = SC.SemanticResponseCache(embedder=emb, max_entries=2)
        _run(c.store("a", _traj("a"), None, repo_digest="R"))
        _run(c.store("b", _traj("b"), None, repo_digest="R"))
        _run(c.store("c", _traj("c"), None, repo_digest="R"))  # evicts "a"
        self.assertEqual(len(c), 2)
        self.assertIsNone(_run(c.lookup("a", None, repo_digest="R")))  # evicted
        self.assertIsNotNone(_run(c.lookup("c", None, repo_digest="R")))


class TestCachedOrGenerateWiring(unittest.TestCase):
    """The semantic tier fused into cached_or_generate: on exact MISS a
    near-match returns SEMANTIC_HIT and the provider (produce) is skipped."""

    def setUp(self):
        os.environ["JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED"] = "1"
        os.environ["JARVIS_SEMANTIC_CACHE_ENABLED"] = "1"
        from backend.core.ouroboros.governance import provider_response_cache as PRC
        self.PRC = PRC
        self._saved = {
            "gdc": PRC.get_default_cache,
            "cck": PRC.compute_cache_key,
            "rgr": PRC.reconstruct_generation_result,
        }

        class _StubCache:
            def lookup(self, full, pref):
                return (PRC.CacheLookupOutcome.MISS, None)  # force exact MISS

            def store(self, t):
                pass

        PRC.get_default_cache = lambda: _StubCache()
        PRC.compute_cache_key = lambda *a, **k: ("full", "pref")  # avoid git

    def tearDown(self):
        self.PRC.get_default_cache = self._saved["gdc"]
        self.PRC.compute_cache_key = self._saved["cck"]
        self.PRC.reconstruct_generation_result = self._saved["rgr"]
        os.environ.pop("JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED", None)
        os.environ.pop("JARVIS_SEMANTIC_CACHE_ENABLED", None)

    def test_semantic_hit_intercepts_before_produce(self):
        PRC = self.PRC
        sentinel_gr = object()
        PRC.reconstruct_generation_result = lambda t: sentinel_gr

        saved_lookup = SC.semantic_cache_lookup
        async def _fake_lookup(prompt, repo_root):
            return _traj("sem")  # near-match found
        SC.semantic_cache_lookup = _fake_lookup

        called = {"produce": False}
        async def _produce():
            called["produce"] = True
            return object()

        try:
            gr, outcome = _run(PRC.cached_or_generate(
                prompt="x", model="m", route="r",
                repo_root=pathlib.Path("."), produce=_produce,
            ))
        finally:
            SC.semantic_cache_lookup = saved_lookup

        self.assertIs(gr, sentinel_gr)
        self.assertEqual(outcome, PRC.CacheLookupOutcome.SEMANTIC_HIT)
        self.assertFalse(called["produce"])  # provider skipped → $0.00


class TestNoHardcode(unittest.TestCase):
    def test_no_hardcoded_embedder_model_string(self):
        src = pathlib.Path(
            "backend/core/ouroboros/governance/semantic_cache.py"
        ).read_text()
        self.assertNotIn("bge", src.lower())  # delegates to SemanticIndex factory


if __name__ == "__main__":
    unittest.main()
