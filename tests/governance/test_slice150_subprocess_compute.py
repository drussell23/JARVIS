"""Slice 150 Phase 1 — reusable subprocess-compute substrate.

Composes the proven oracle_ipc parent machinery (AsyncOracleProxy: spawn → bounded
respawn → async recv via executor → fail-closed) so GIL-bound CPU work (SemanticIndex
k-means build, posture collectors) can run in a dedicated spawn-process. Threads
can't escape the GIL; processes can. Gated default-FALSE; fail-closed.

These tests are deterministic (fake Pipe / no real subprocess) — the real
subprocess path is the SAME mechanism oracle_ipc already exercises in production.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance import subprocess_compute as SC


class _FakeConn:
    """Minimal duplex-Pipe stand-in for the worker side."""

    def __init__(self, inbox):
        self._inbox = list(inbox)
        self.sent = []

    def recv(self):
        if not self._inbox:
            raise EOFError
        return self._inbox.pop(0)

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_COMPUTE_ISOLATION_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(SC.compute_isolation_enabled())


class TestWorkerLoop(unittest.TestCase):
    def test_signals_ready_then_dispatches(self):
        conn = _FakeConn([
            {"id": 1, "method": "double", "args": [5]},
            {"control": "shutdown"},
        ])
        SC.run_worker_loop(conn, {"double": lambda x: x * 2})
        self.assertEqual(conn.sent[0], {"control": "ready"})
        self.assertEqual(conn.sent[1], {"id": 1, "ok": True, "result": 10})

    def test_unknown_method_reported_not_fatal(self):
        conn = _FakeConn([
            {"id": 2, "method": "nope"},
            {"id": 3, "method": "double", "args": [4]},
            {"control": "shutdown"},
        ])
        SC.run_worker_loop(conn, {"double": lambda x: x * 2})
        self.assertFalse(conn.sent[1]["ok"])
        self.assertIn("no handler", conn.sent[1]["error"])
        self.assertEqual(conn.sent[2], {"id": 3, "ok": True, "result": 8})  # loop survived

    def test_handler_exception_is_fail_closed(self):
        def _boom():
            raise RuntimeError("kmeans exploded")
        conn = _FakeConn([
            {"id": 4, "method": "boom"},
            {"id": 5, "method": "ok"},
            {"control": "shutdown"},
        ])
        SC.run_worker_loop(conn, {"boom": _boom, "ok": lambda: "fine"})
        self.assertFalse(conn.sent[1]["ok"])
        self.assertIn("kmeans exploded", conn.sent[1]["error"])
        self.assertEqual(conn.sent[2]["result"], "fine")  # survived the crash

    def test_eof_breaks_cleanly(self):
        conn = _FakeConn([])  # immediate EOF
        SC.run_worker_loop(conn, {})
        self.assertEqual(conn.sent, [{"control": "ready"}])  # ready then clean exit


class TestProxyComposition(unittest.TestCase):
    def test_make_compute_proxy_reuses_async_oracle_proxy(self):
        from backend.core.ouroboros.oracle_ipc import AsyncOracleProxy

        def _worker(conn):  # pragma: no cover - not run here
            SC.run_worker_loop(conn, {})

        proxy = SC.make_compute_proxy(_worker)
        self.assertIsInstance(proxy, AsyncOracleProxy)
        # the injected spawn_fn must be wired (callable) — composition, not reinvention
        self.assertTrue(callable(proxy._spawn_fn))

    def test_is_not_ready_sentinel(self):
        from backend.core.ouroboros.oracle_ipc import OracleNotReady
        self.assertTrue(SC.is_not_ready(OracleNotReady(reason="hydrating")))
        self.assertFalse(SC.is_not_ready("a result"))


if __name__ == "__main__":
    unittest.main()
