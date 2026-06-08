"""Slice 153 — EmbeddingService fastembed fallback (Oracle embed_nodes via fastembed).

The Oracle-capable soak image ships fastembed, not sentence-transformers (which pulls
torch). Today EmbeddingService._load_model ImportErrors on missing sentence-transformers
→ encode() returns None → the Oracle's embed_nodes produces no embeddings. This wires
a SentenceTransformer.encode-compatible fastembed adapter into that ImportError branch,
so encode() (and all 14+ consumers) work UNCHANGED on fastembed. Config-sourced model
name (no hardcode); fastembed import injectable → deterministic tests.
"""
from __future__ import annotations

import unittest

import numpy as np

from backend.core import embedding_service as ES


class _FakeTextEmbedding:
    """Stand-in for fastembed.TextEmbedding: .embed(list[str]) -> iterator of vectors."""

    def __init__(self, model_name):
        self.model_name = model_name

    def embed(self, texts):
        # deterministic 4-dim vectors (unnormalized) per text length
        for t in texts:
            n = float(len(t)) + 1.0
            yield np.array([n, n * 2, n * 3, n * 4], dtype="float32")


class TestConfig(unittest.TestCase):
    def test_fastembed_model_name_default(self):
        cfg = ES.EmbeddingServiceConfig()
        self.assertTrue(getattr(cfg, "fastembed_model_name", None))

    def test_fastembed_model_name_from_env(self):
        import os
        os.environ["EMBEDDING_FASTEMBED_MODEL"] = "BAAI/bge-small-en-v1.5"
        try:
            cfg = ES.EmbeddingServiceConfig.from_env()
            self.assertEqual(cfg.fastembed_model_name, "BAAI/bge-small-en-v1.5")
        finally:
            os.environ.pop("EMBEDDING_FASTEMBED_MODEL", None)


class TestAdapter(unittest.TestCase):
    def _adapter(self):
        return ES._FastembedSTAdapter("m", factory=lambda name: _FakeTextEmbedding(name))

    def test_encode_returns_2d_float32_array(self):
        a = self._adapter()
        out = a.encode(["alpha", "beta"], convert_to_numpy=True)
        self.assertEqual(out.shape, (2, 4))
        self.assertEqual(out.dtype, np.float32)

    def test_encode_normalizes_when_requested(self):
        a = self._adapter()
        out = a.encode(["alpha"], normalize_embeddings=True)
        self.assertAlmostEqual(float(np.linalg.norm(out[0])), 1.0, places=5)

    def test_encode_unnormalized_when_disabled(self):
        a = self._adapter()
        out = a.encode(["alpha"], normalize_embeddings=False)
        self.assertGreater(float(np.linalg.norm(out[0])), 1.0)  # raw magnitude kept

    def test_encode_accepts_sentencetransformer_kwargs(self):
        # Must swallow the exact kwargs EmbeddingService.encode passes to model.encode.
        a = self._adapter()
        out = a.encode(
            ["x"], batch_size=32, normalize_embeddings=True,
            show_progress_bar=False, convert_to_numpy=True,
        )
        self.assertEqual(out.shape, (1, 4))


class TestServiceFallback(unittest.TestCase):
    def test_make_fastembed_model_uses_injected_factory(self):
        svc = ES.EmbeddingService()  # singleton
        model = svc._make_fastembed_model(factory=lambda name: _FakeTextEmbedding(name))
        self.assertIsNotNone(model)
        self.assertEqual(model.model_name, svc._config.fastembed_model_name)
        out = model.encode(["hello"], normalize_embeddings=True)
        self.assertEqual(out.shape, (1, 4))


if __name__ == "__main__":
    unittest.main()
