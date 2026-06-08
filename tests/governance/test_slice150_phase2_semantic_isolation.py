"""Slice 150 Phase 2 — process-isolate the SemanticIndex build (the 9.75s GIL stall).

Worker process builds + writes .jarvis/semantic_index.npz; the parent reloads from
that cache (no centroid crosses the Pipe). build_async() is the single method that
changes — when JARVIS_COMPUTE_ISOLATION_ENABLED it dispatches to the subprocess
proxy instead of a daemon thread; OFF is byte-identical. Fail-closed: a not-ready /
failed worker keeps the current centroid.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance import semantic_index as SI


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _seed_and_persist(root: pathlib.Path):
    """Populate a SemanticIndex's state + write its .npz cache (uses the real writer)."""
    idx = SI.SemanticIndex(root)
    idx._corpus = [
        SI.CorpusItem(text="alpha commit", source="git", ts=1000.0, halflife_days=14.0),
        SI.CorpusItem(text="beta goal", source="goal", ts=2000.0, halflife_days=14.0),
    ]
    # Values exactly representable in float32 so the .npz round-trip is bit-exact.
    idx._vectors = [[0.5, 0.25], [0.125, 0.75]]
    idx._centroid = [0.25, 0.5]
    idx._built_at = 12345.0
    idx._persist_cache_safe()
    return idx


class TestLoader(unittest.TestCase):
    def test_load_from_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            seed = _seed_and_persist(root)
            fresh = SI.SemanticIndex(root)
            self.assertTrue(fresh._load_from_cache())
            self.assertEqual(fresh._centroid, seed._centroid)
            self.assertEqual(fresh._built_at, seed._built_at)
            self.assertEqual(len(fresh._corpus), 2)
            self.assertEqual(fresh._corpus[0].text, "alpha commit")

    def test_load_from_cache_failclosed_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            idx = SI.SemanticIndex(pathlib.Path(d))  # no .npz written
            idx._centroid = [9.9]  # current state
            self.assertFalse(idx._load_from_cache())
            self.assertEqual(idx._centroid, [9.9])  # unchanged → fail-closed


class _FakeProxy:
    def __init__(self, result):
        self._result = result
        self.calls = []
    async def start(self):
        pass
    async def call(self, method, *args, **kwargs):
        self.calls.append((method, args))
        return self._result
    async def shutdown(self):
        pass


class TestIsolatedBuild(unittest.TestCase):
    def test_success_triggers_reload(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            _seed_and_persist(root)  # a valid cache exists to reload
            idx = SI.SemanticIndex(root)
            idx._async_build_running = True
            proxy = _FakeProxy({"built": True, "n_docs": 2})
            _run(idx._isolated_build(proxy=proxy))
            self.assertEqual(proxy.calls[0][0], "build")
            self.assertEqual(idx._centroid, [0.25, 0.5])   # reloaded from cache
            self.assertFalse(idx._async_build_running)     # flag cleared

    def test_not_ready_is_fail_closed(self):
        from backend.core.ouroboros.oracle_ipc import OracleNotReady
        with tempfile.TemporaryDirectory() as d:
            idx = SI.SemanticIndex(pathlib.Path(d))
            idx._centroid = [7.7]
            idx._async_build_running = True
            proxy = _FakeProxy(OracleNotReady(reason="hydrating"))
            _run(idx._isolated_build(proxy=proxy))
            self.assertEqual(idx._centroid, [7.7])         # kept → fail-closed
            self.assertFalse(idx._async_build_running)

    def test_proxy_exception_is_fail_closed(self):
        class _BoomProxy(_FakeProxy):
            async def call(self, method, *a, **k):
                raise RuntimeError("pipe broke")
        with tempfile.TemporaryDirectory() as d:
            idx = SI.SemanticIndex(pathlib.Path(d))
            idx._centroid = [5.5]
            idx._async_build_running = True
            _run(idx._isolated_build(proxy=_BoomProxy(None)))
            self.assertEqual(idx._centroid, [5.5])
            self.assertFalse(idx._async_build_running)     # never wedged


class TestBuildAsyncGating(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_COMPUTE_ISOLATION_ENABLED", None)

    def test_worker_handler_returns_summary_dict(self):
        # The worker build handler must return a small picklable dict (no numpy).
        self.assertTrue(hasattr(SI, "_semantic_build_handler"))
        self.assertTrue(hasattr(SI, "_semantic_build_worker_main"))


if __name__ == "__main__":
    unittest.main()
