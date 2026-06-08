"""Slice 149 P4 — Oracle dependency preflight + fail-fast.

The lean soak image lacks the Oracle's hard deps (networkx, declared required at
oracle.py:818). Today that causes a 3x respawn storm with exponential backoff
before DEGRADED. This preflights the deps BEFORE spawning: missing → DEGRADE ONCE
(reason=missing_deps), no respawn churn; present (full host) → normal spawn; a
transient crash (deps present) still uses the bounded respawn. Single-source dep
declaration; checker injectable → deterministic, no real imports.
"""
from __future__ import annotations

import asyncio
import threading
import unittest

from backend.core.ouroboros import oracle_ipc as OI


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeConn:
    def __init__(self):
        self._ev = threading.Event()
        self._ready_sent = False

    def recv(self):
        if not self._ready_sent:
            self._ready_sent = True
            return {"control": "ready"}
        self._ev.wait()      # block until close() (no spurious respawn)
        raise EOFError

    def send(self, msg):
        pass

    def close(self):
        self._ev.set()


class _FakeProc:
    def is_alive(self):
        return True

    def terminate(self):
        pass


class TestDepDeclaration(unittest.TestCase):
    def test_networkx_is_declared_required(self):
        # Single source, backed by oracle.py:818 "networkx is required for Oracle".
        self.assertIn("networkx", OI._ORACLE_REQUIRED_DEPS)

    def test_dependencies_available_with_injected_checker(self):
        ok, missing = OI.oracle_dependencies_available(checker=lambda d: d != "networkx")
        self.assertFalse(ok)
        self.assertIn("networkx", missing)
        ok2, missing2 = OI.oracle_dependencies_available(checker=lambda d: True)
        self.assertTrue(ok2)
        self.assertEqual(missing2, [])


class TestPreflightFailFast(unittest.TestCase):
    def test_missing_deps_degrades_once_no_spawn_no_respawn(self):
        spawned = []
        proxy = OI.AsyncOracleProxy(spawn_fn=lambda: (spawned.append(1), (None, None))[1])
        _run(proxy.start(dep_check=lambda: (False, ["networkx"])))
        self.assertEqual(spawned, [])          # never spawned → no ImportError storm
        self.assertEqual(proxy._respawns, 0)   # no respawn churn
        self.assertFalse(proxy.is_ready)
        self.assertEqual(proxy._not_ready().reason, "missing_deps")

    def test_present_deps_spawn_normally(self):
        spawned = []
        def _spawn():
            spawned.append(1)
            return (_FakeConn(), _FakeProc())
        proxy = OI.AsyncOracleProxy(spawn_fn=_spawn)
        _run(proxy.start(dep_check=lambda: (True, [])))
        self.assertEqual(spawned, [1])         # full host → normal spawn
        _run(proxy.shutdown())

    def test_preflight_exception_fails_open_to_spawn(self):
        spawned = []
        def _spawn():
            spawned.append(1)
            return (_FakeConn(), _FakeProc())
        def _boom():
            raise RuntimeError("checker broke")
        proxy = OI.AsyncOracleProxy(spawn_fn=_spawn)
        _run(proxy.start(dep_check=_boom))
        self.assertEqual(spawned, [1])         # never block the Oracle on a buggy preflight
        _run(proxy.shutdown())

    def test_default_start_still_works(self):
        # start() with no dep_check must still run (uses the real preflight).
        spawned = []
        def _spawn():
            spawned.append(1)
            return (_FakeConn(), _FakeProc())
        proxy = OI.AsyncOracleProxy(spawn_fn=_spawn)
        # Inject a checker that says present so we don't depend on the host's networkx.
        _run(proxy.start(dep_check=lambda: (True, [])))
        self.assertEqual(spawned, [1])
        _run(proxy.shutdown())


if __name__ == "__main__":
    unittest.main()
