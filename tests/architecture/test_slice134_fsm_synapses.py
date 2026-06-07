"""Slice 134 — Autonomous FSM Synapse Integration (the write-side).

The orchestrator's terminal hook fires ``note_transition_nowait`` — a
fire-and-forget, NON-BLOCKING scheduler that records an episode (with a context
snapshot) into the episodic ledger without awaiting on the hot path or starving
the event loop. Gated by JARVIS_EPISODIC_CORE_ENABLED; fail-soft.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import episodic_core as EC


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestNoteTransitionNowait(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def test_disabled_noop(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.note_transition_nowait(op_id="o", phase_from="A", phase_to="B")
        self.assertEqual(EC.get_episodic_ledger().recent(10), [])

    def test_schedules_on_running_loop_nonblocking(self):
        async def go():
            # Returns immediately (no await) — the record runs as a scheduled task.
            EC.note_transition_nowait(
                op_id="o1", phase_from="START", phase_to="COMPLETE",
                summary="cycle done", context={"reason": "ok", "route": "background"},
            )
            await asyncio.sleep(0.05)  # let the scheduled task run
            eps = EC.get_episodic_ledger().recent(10)
            self.assertTrue(eps)
            self.assertEqual(eps[-1].op_id, "o1")
            self.assertEqual(eps[-1].context.get("reason"), "ok")
            self.assertEqual(eps[-1].context.get("phase_to"), "COMPLETE")
        _run(go())

    def test_nonblocking_returns_before_task_completes(self):
        async def go():
            EC.note_transition_nowait(op_id="o2", phase_from="GENERATE", phase_to="VALIDATE")
            # Immediately after the call, the task has NOT yet run (we never awaited).
            self.assertEqual(EC.get_episodic_ledger().recent(10), [])
            await asyncio.sleep(0.05)
            self.assertTrue(EC.get_episodic_ledger().recent(10))
        _run(go())

    def test_fail_soft_never_raises(self):
        # Bizarre inputs in a sync context must not raise.
        EC.note_transition_nowait(op_id=None, phase_from=None, phase_to=None)


class TestOrchestratorSynapse(unittest.TestCase):
    """The orchestrator terminal hook fires the synapse — recorder-INDEPENDENT,
    capturing the context snapshot (reason code + route)."""

    def setUp(self):
        os.environ["JARVIS_EPISODIC_CORE_ENABLED"] = "1"
        EC.reset_episodic_ledger()

    def tearDown(self):
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        EC.reset_episodic_ledger()

    def test_terminal_records_episode_without_recorder(self):
        import types
        from backend.core.ouroboros.governance import orchestrator as ORC
        ctx = types.SimpleNamespace(
            op_id="op-T", terminal_reason_code="IRON_GATE_BLOCKED",
            provider_route="immediate",
        )
        state = types.SimpleNamespace(value="BLOCKED")
        # No active SessionRecorder in this test → proves the synapse is
        # independent of the battle-test recorder.
        ORC._slice12q_record_terminal(ctx, state, {"route": "immediate"})
        eps = EC.get_episodic_ledger().recent(10)
        self.assertTrue(eps)
        self.assertEqual(eps[-1].op_id, "op-T")
        self.assertEqual(eps[-1].kind, "transition")
        self.assertEqual(eps[-1].context.get("terminal_reason_code"), "IRON_GATE_BLOCKED")
        self.assertEqual(eps[-1].context.get("phase_to"), "BLOCKED")

    def test_terminal_noop_when_disabled(self):
        import types
        from backend.core.ouroboros.governance import orchestrator as ORC
        os.environ.pop("JARVIS_EPISODIC_CORE_ENABLED", None)
        ctx = types.SimpleNamespace(op_id="op-D", terminal_reason_code="", provider_route="")
        ORC._slice12q_record_terminal(ctx, types.SimpleNamespace(value="COMPLETE"), {})
        self.assertEqual(EC.get_episodic_ledger().recent(10), [])


if __name__ == "__main__":
    unittest.main()
