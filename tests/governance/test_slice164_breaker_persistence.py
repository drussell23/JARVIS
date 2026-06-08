"""Slice 164 — durable Claude-breaker state (kill the cold-start, no funding).

The breaker is an in-memory singleton: it resets to CLOSED on every process boot. So
when Claude is credit-dead, the first ops each relaunch hammer Claude-direct and
exhaust BEFORE the breaker re-learns it's dead and the Slice 161/162 protections engage.

Fix: persist the economic-trip state to disk (.jarvis, host-local). On boot, if a
RECENT economic death is recorded (within a TTL), the breaker restores OPEN so the
DW-sovereignty protections are warm from op #1 — no cold-start, no funding. The
recovery window restarts from boot, so a since-funded Claude still self-heals via the
normal HALF_OPEN probe. Gated default-FALSE.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from backend.core.ouroboros.governance import claude_circuit_breaker as CB
from backend.core.ouroboros.governance.claude_circuit_breaker import (
    ClaudeCircuitBreaker,
    CircuitState,
)


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "claude_breaker_state.json")


class TestPersistHelpers(unittest.TestCase):
    def test_write_read_roundtrip(self):
        p = _tmp()
        CB._write_breaker_state("open", 2, reason="economic", path=p, now_wall=1000.0)
        self.assertEqual(CB._read_breaker_state(path=p, now_wall=1100.0, ttl_s=86400), 2)

    def test_ttl_expiry_returns_none(self):
        p = _tmp()
        CB._write_breaker_state("open", 2, reason="economic", path=p, now_wall=1000.0)
        self.assertIsNone(CB._read_breaker_state(path=p, now_wall=1000.0 + 90000, ttl_s=86400))

    def test_non_open_state_returns_none(self):
        p = _tmp()
        CB._write_breaker_state("closed", 0, reason="recovered", path=p, now_wall=1000.0)
        self.assertIsNone(CB._read_breaker_state(path=p, now_wall=1000.0, ttl_s=86400))

    def test_missing_file_returns_none(self):
        self.assertIsNone(CB._read_breaker_state(path="/no/such/file.json", now_wall=1.0, ttl_s=1))


class TestBreakerRestore(unittest.TestCase):
    def tearDown(self):
        for k in ("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", "JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"):
            os.environ.pop(k, None)

    def test_default_persist_disabled(self):
        os.environ.pop("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", None)
        self.assertFalse(CB._breaker_persist_enabled())

    def test_restores_open_from_recent_economic_death(self):
        os.environ["JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED"] = "1"
        p = _tmp()
        CB._write_breaker_state("open", 3, reason="economic", path=p, now_wall=time.time())
        b = ClaudeCircuitBreaker(persist_path=p)
        self.assertIs(b.state, CircuitState.OPEN)  # warm from boot — no cold-start

    def test_no_restore_when_disabled(self):
        os.environ.pop("JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED", None)
        p = _tmp()
        CB._write_breaker_state("open", 3, reason="economic", path=p, now_wall=time.time())
        b = ClaudeCircuitBreaker(persist_path=p)
        self.assertIs(b.state, CircuitState.CLOSED)  # gated off → legacy cold start

    def test_no_restore_when_stale(self):
        os.environ["JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED"] = "1"
        os.environ["JARVIS_CLAUDE_BREAKER_PERSIST_TTL_S"] = "60"
        try:
            p = _tmp()
            CB._write_breaker_state("open", 3, reason="economic", path=p, now_wall=time.time() - 3600)
            b = ClaudeCircuitBreaker(persist_path=p)
            self.assertIs(b.state, CircuitState.CLOSED)  # stale → re-probe Claude
        finally:
            os.environ.pop("JARVIS_CLAUDE_BREAKER_PERSIST_TTL_S", None)

    def test_economic_trip_persists_state(self):
        os.environ["JARVIS_CLAUDE_BREAKER_PERSIST_ENABLED"] = "1"
        os.environ["JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED"] = "1"
        p = _tmp()
        b = ClaudeCircuitBreaker(persist_path=p)
        b.record_economic_exhaustion("test")  # threshold default 1 → trips OPEN
        self.assertIs(b.state, CircuitState.OPEN)
        self.assertEqual(CB._read_breaker_state(path=p, now_wall=time.time(), ttl_s=86400), 1)


if __name__ == "__main__":
    unittest.main()
