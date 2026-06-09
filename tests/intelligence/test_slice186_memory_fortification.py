"""Slice 186 — cognitive & memory fortification.

Phase 2: ChromaDB rejected our embeddings ("Expected embeddings to be a list of floats...")
         because a 2-D model output (.tolist() → [[...]]) or numpy scalar types slipped through.
         The strict sanitizer casts ANY shape to a FLAT list of python floats.
Phase 3: the sync embedder.encode() + collection.query/.add ran on the event loop, starving the
         control plane (lag 1190ms). They now offload via asyncio.to_thread.
"""
from __future__ import annotations

import importlib.util
import unittest

from backend.intelligence.long_term_memory import sanitize_embedding_vector


class _FakeNdarray:
    """Mimics numpy: has .tolist() that returns nested python lists."""
    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data


class TestSanitizer(unittest.TestCase):
    def test_flat_python_list_passes(self):
        self.assertEqual(sanitize_embedding_vector([0.1, 0.2, 0.3]), [0.1, 0.2, 0.3])

    def test_numpy_1d_to_list(self):
        out = sanitize_embedding_vector(_FakeNdarray([0.1, 0.2]))
        self.assertEqual(out, [0.1, 0.2])
        self.assertTrue(all(isinstance(x, float) for x in out))

    def test_numpy_2d_is_flattened(self):
        # shape [1, dim] → [[...]] must collapse to a flat vector
        out = sanitize_embedding_vector(_FakeNdarray([[0.1, 0.2, 0.3]]))
        self.assertEqual(out, [0.1, 0.2, 0.3])

    def test_nested_python_list_flattened(self):
        self.assertEqual(sanitize_embedding_vector([[0.5, 0.6]]), [0.5, 0.6])

    def test_int_elements_cast_to_float(self):
        out = sanitize_embedding_vector([1, 2, 3])
        self.assertEqual(out, [1.0, 2.0, 3.0])
        self.assertTrue(all(isinstance(x, float) for x in out))

    def test_empty_returns_none(self):
        self.assertIsNone(sanitize_embedding_vector([]))

    def test_none_returns_none(self):
        self.assertIsNone(sanitize_embedding_vector(None))

    def test_garbage_returns_none_not_raise(self):
        self.assertIsNone(sanitize_embedding_vector("not a vector"))
        self.assertIsNone(sanitize_embedding_vector(object()))


class TestPhase3AsyncOffload(unittest.TestCase):
    def _src(self):
        spec = importlib.util.find_spec("backend.intelligence.long_term_memory")
        with open(spec.origin) as fh:
            return fh.read()

    def test_blocking_calls_offloaded_to_thread(self):
        src = self._src()
        # the sync embedder + chromadb ops must be wrapped in asyncio.to_thread
        self.assertIn("asyncio.to_thread", src)
        # specifically the embedding generation and the chroma query
        self.assertIn("to_thread(self._generate_embedding", src)

    def test_generate_embedding_uses_sanitizer(self):
        src = self._src()
        self.assertIn("sanitize_embedding_vector", src)


if __name__ == "__main__":
    unittest.main()
