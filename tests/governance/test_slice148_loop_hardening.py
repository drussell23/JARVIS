"""Slice 148 — Event-loop hardening (goal-inference build offload).

The live soak's LoopSink caught `semantic_index.SemanticIndex.build kind=sync
blocked_ms=8044` — the orchestrator's CONTEXT_EXPANSION called the synchronous
`GoalInferenceEngine.build()` (→ SemanticIndex.build, fastembed inference) directly
on the event loop, stalling the whole governance loop for 8s. The heavy build must
run off-loop via asyncio.to_thread so other coroutines keep running.

This is a call-site wiring pin (the orchestrator pipeline is too large to unit-test
directly — same convention as the economic-breaker wiring pin).
"""
from __future__ import annotations

import pathlib
import unittest

_ORCH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "orchestrator.py"
)


class TestGoalInferenceBuildOffloaded(unittest.TestCase):
    def setUp(self):
        self.src = _ORCH.read_text(encoding="utf-8")

    def test_build_runs_off_loop_via_to_thread(self):
        # The heavy build must be offloaded. Slice 149 Phase 2 converged the
        # orchestrator + CLASSIFY call sites onto GoalInferenceEngine.build_offloaded()
        # (which wraps asyncio.to_thread) — either form keeps it off the loop.
        self.assertTrue(
            "await _engine.build_offloaded()" in self.src
            or "asyncio.to_thread(_engine.build" in self.src
        )

    def test_no_bare_sync_build_on_loop(self):
        # The bare synchronous on-loop call must be gone.
        self.assertNotIn("_inf_result = _engine.build()", self.src)


if __name__ == "__main__":
    unittest.main()
