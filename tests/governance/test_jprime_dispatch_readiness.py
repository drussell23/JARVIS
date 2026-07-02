"""Pre-SERVING dispatch readiness gate -- the last seam (bt-iso-1782959216).

With the node's lifetime finally pinned (handback off, on-demand, transport
sovereign), the census showed 10 routed dispatches but only 3 streams: 20
dispatches sealed `sovereign_route_sealed:gcp-jprime:LocalLatencyLockup`
because they fired while the node was still BOOTING -- /api/tags unanswered ->
num_ctx negotiation None -> non-streaming survival path -> 30s cold budget ->
terminal seal. A COMMITTED sovereign dispatch must WAIT for node readiness
(bounded, heartbeat-pulsing, suspend-aware) instead of dying on a 30s probe
against a node that is still loading its model.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import backend.core.ouroboros.governance.candidate_generator as cg
import backend.core.ouroboros.governance.cooperative_shutdown as coop
from backend.core.ouroboros.governance import stream_heartbeat as shb
from backend.core.ouroboros.governance.local_inference_director import (
    GracefulStreamInterruption,
)


def _run(coro):
    return asyncio.run(coro)


class TestAwaitJprimeReady:
    def test_ready_immediately(self, monkeypatch):
        calls = {"n": 0}

        async def probe(ep):
            calls["n"] += 1
            return 19_850_000_000          # model listed -> ready

        ok = _run(cg._await_jprime_ready("http://n:11434", probe_fn=probe))
        assert ok is True
        assert calls["n"] == 1

    def test_waits_until_ready(self, monkeypatch):
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_POLL_S", "0.02")
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_BUDGET_S", "5")
        calls = {"n": 0}

        async def probe(ep):
            calls["n"] += 1
            return 1 if calls["n"] >= 3 else None    # ready on 3rd poll

        ok = _run(cg._await_jprime_ready("http://n:11434", probe_fn=probe))
        assert ok is True
        assert calls["n"] == 3

    def test_budget_expiry_returns_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_POLL_S", "0.02")
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_BUDGET_S", "0.1")

        async def probe(ep):
            return None                    # never ready

        start = time.monotonic()
        ok = _run(cg._await_jprime_ready("http://n:11434", probe_fn=probe))
        assert ok is False
        assert time.monotonic() - start < 2.0        # bounded

    def test_pulses_heartbeat_while_waiting(self, monkeypatch):
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_POLL_S", "0.02")
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_BUDGET_S", "5")
        shb.reset()
        calls = {"n": 0}

        async def probe(ep):
            calls["n"] += 1
            return 1 if calls["n"] >= 4 else None

        _run(cg._await_jprime_ready("http://n:11434", probe_fn=probe))
        assert shb.pulse_count() >= 3      # waiting IS activity (deferral-visible)

    def test_cooperative_shutdown_freezes_the_wait(self, monkeypatch):
        """A suspend during the readiness wait must freeze (GSI -> checkpoint
        boundary), not burn the budget against a dead run."""
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_POLL_S", "0.02")
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_BUDGET_S", "30")
        coop.reset()

        async def probe(ep):
            return None                    # never ready; shutdown interrupts

        async def scenario():
            async def fire():
                await asyncio.sleep(0.1)
                coop.request("sigterm")
            asyncio.ensure_future(fire())
            with pytest.raises(GracefulStreamInterruption):
                await cg._await_jprime_ready("http://n:11434", probe_fn=probe)

        start = time.monotonic()
        _run(scenario())
        assert time.monotonic() - start < 2.0        # freeze was prompt

    def test_master_disable_skips_wait(self, monkeypatch):
        monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_ENABLED", "false")
        calls = {"n": 0}

        async def probe(ep):
            calls["n"] += 1
            return None

        ok = _run(cg._await_jprime_ready("http://n:11434", probe_fn=probe))
        assert ok is True                  # legacy: proceed immediately
        assert calls["n"] == 0
