"""Dual-Module Peer Review + QoS Diagnostic spine.

Mandate 4 verbatim (2026-07-19): Daniel receives a technical prompt,
yields to Karen via the RPC handshake, but Karen's response triggers a
SECOND yield from Daniel → the Depth-Bounded Consensus Lock intercepts
the second yield, PREVENTS the API call, and forces a graceful
user-facing fallback.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex.peer_consensus import (
    OUTCOME_LOCK_INTERCEPTED,
    OUTCOME_RESOLVED,
    OUTCOME_TIMEOUT,
    PeerConsensus,
)
from backend.core.ouroboros.governance.comms.duplex.qos_sensor import (
    UX_DEGRADATION_EVENT,
    QoSSensor,
    _semantic_key,
)


# ---------------------------------------------------------------------------
# QoS Sensor — implicit frustration
# ---------------------------------------------------------------------------


class TestImplicitFrustration:
    def test_semantic_equivalence_key(self):
        assert _semantic_key("fix the bug") == _semantic_key(
            "can you fix the bug please",
        )
        assert _semantic_key("fix the bug") != _semantic_key("run the tests")

    def test_rapid_repeat_emits_ux_degradation(self):
        emitted = []
        clock = [1000.0]
        qos = QoSSensor(
            emit_signal=emitted.append,
            context_provider=lambda: ["user: fix the crash", "daniel: ..."],
            clock=lambda: clock[0],
        )
        assert qos.observe_command("fix the crash") is False   # first ask
        clock[0] += 5.0
        # Re-asked within the window → the first answer missed:
        assert qos.observe_command("can you fix the crash please") is True
        assert len(emitted) == 1
        env = emitted[0]
        assert UX_DEGRADATION_EVENT in env.description
        assert env.source == "performance_regression"          # DRY intake
        assert env.evidence["ux_degradation"] is True
        assert env.evidence["dialogue_context"]                # bundled

    def test_sigint_override_emits(self):
        emitted = []
        qos = QoSSensor(emit_signal=emitted.append)
        assert qos.observe_override("sigint") is True
        assert "override" in emitted[0].evidence["cause"]

    def test_no_explicit_feedback_needed_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/qos_sensor.py"
        ).read_text()
        # Triggers are IMPLICIT — repetition + override, never a typed
        # "you failed".
        assert "rapid_repeat" in src and "override" in src

    def test_cooldown_collapses_a_frustration_storm(self):
        emitted = []
        clock = [1000.0]
        qos = QoSSensor(emit_signal=emitted.append, clock=lambda: clock[0])
        qos.observe_override("sigint")
        qos.observe_override("sigint")               # within cooldown
        qos.observe_override("sigint")
        assert len(emitted) == 1                     # ONE signal
        assert qos.stats["cooldown_suppressed"] == 2

    def test_distinct_commands_do_not_trigger(self):
        emitted = []
        qos = QoSSensor(emit_signal=emitted.append)
        qos.observe_command("open the file")
        qos.observe_command("run the tests")
        qos.observe_command("commit the change")
        assert emitted == []


# ---------------------------------------------------------------------------
# Depth-Bounded Consensus Lock — MANDATE 4 VERBATIM
# ---------------------------------------------------------------------------


class TestDepthBoundedConsensus:
    async def test_second_yield_intercepted_no_api_call(self):
        """MANDATE 4 VERBATIM."""
        api_calls = {"n": 0}

        async def _karen(query, context):
            api_calls["n"] += 1
            # Karen's answer implies Daniel needs to yield AGAIN:
            return "This needs a deeper trace — yield again for the stack."

        pc = PeerConsensus(karen_diagnose=_karen)
        session = pc.new_session()

        # Daniel's FIRST yield (a hard technical prompt) — runs:
        r1 = await pc.daniel_yields_to_karen(
            session, "why does the orchestrator deadlock under load?",
        )
        assert r1["outcome"] == OUTCOME_RESOLVED
        assert api_calls["n"] == 1

        # Daniel tries to yield AGAIN in the SAME interaction — the
        # lock MUST intercept before any provider call:
        r2 = await pc.daniel_yields_to_karen(
            session, "ok now trace the deadlock deeper",
        )
        assert r2["outcome"] == OUTCOME_LOCK_INTERCEPTED
        assert api_calls["n"] == 1                    # API NOT called again
        assert "couldn't fully resolve" in r2["payload"]  # graceful fallback
        assert pc.stats["lock_intercepted"] == 1

    async def test_new_interaction_gets_fresh_budget(self):
        calls = {"n": 0}
        async def _karen(q, c):
            calls["n"] += 1
            return "diagnosis"
        pc = PeerConsensus(karen_diagnose=_karen)
        # Interaction 1:
        await pc.daniel_yields_to_karen(pc.new_session(), "q1")
        # Interaction 2 — a NEW session, fresh single round-trip:
        r = await pc.daniel_yields_to_karen(pc.new_session(), "q2")
        assert r["outcome"] == OUTCOME_RESOLVED
        assert calls["n"] == 2                        # both allowed

    async def test_timeout_falls_back_gracefully(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CONSENSUS_TIMEOUT_S", "2")
        import asyncio
        async def _slow(q, c):
            await asyncio.sleep(60)
            return "never"
        pc = PeerConsensus(karen_diagnose=_slow)
        r = await pc.daniel_yields_to_karen(pc.new_session(), "hard q")
        assert r["outcome"] == OUTCOME_TIMEOUT
        assert "couldn't fully resolve" in r["payload"]

    async def test_karen_fault_falls_back_not_crash(self):
        async def _boom(q, c):
            raise RuntimeError("provider down")
        pc = PeerConsensus(karen_diagnose=_boom)
        r = await pc.daniel_yields_to_karen(pc.new_session(), "q")
        assert r["outcome"] == "error_fallback"
        assert r["payload"]                           # graceful message
