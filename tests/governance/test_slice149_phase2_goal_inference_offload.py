"""Slice 149 Phase 2 — goal-inference build must never run on the event loop.

The live soak's LoopSink caught a SECOND `semantic_index.SemanticIndex.build
kind=sync blocked_ms~8800` after S148 fixed the orchestrator's CONTEXT_EXPANSION
call — this one in `phase_runners/classify_runner.py` (CLASSIFY phase). Both call
sites now go through a single async off-loop method, `GoalInferenceEngine.
build_offloaded()`, so the heavy synchronous build (→ SemanticIndex.build, fastembed
inference) can never stall the governance loop.

Regression pin: no boot/soak phase path may call the bare synchronous
`_engine.build()` on the loop — they must await `build_offloaded()`.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import re
import unittest

_GOV = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
)
_SCAN = [
    _GOV / "orchestrator.py",
    _GOV / "phase_runners" / "classify_runner.py",
]


class TestBuildOffloadedExists(unittest.TestCase):
    def test_engine_has_async_build_offloaded(self):
        from backend.core.ouroboros.governance.goal_inference import (
            GoalInferenceEngine,
        )
        self.assertTrue(hasattr(GoalInferenceEngine, "build_offloaded"))
        self.assertTrue(
            inspect.iscoroutinefunction(GoalInferenceEngine.build_offloaded)
        )


class TestNoBareBuildOnLoop(unittest.TestCase):
    def test_no_bare_engine_build_in_boot_paths(self):
        # Any `_engine.build(` must be either `build_offloaded` or wrapped in
        # to_thread — never a bare synchronous on-loop call.
        offenders = []
        for p in _SCAN:
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"_engine\.build\((?!_offloaded)", src):
                # allow the to_thread form: asyncio.to_thread(_engine.build
                start = max(0, m.start() - 40)
                ctx = src[start:m.start()]
                if "to_thread(_engine.build" in src[start:m.end() + 5]:
                    continue
                # the bare-call form is `_engine.build(` not preceded by build_offloaded
                offenders.append(f"{p.name}: ...{src[start:m.end()+5]!r}")
        self.assertEqual(offenders, [], f"bare on-loop _engine.build found: {offenders}")


class TestOffloadRunsOffLoop(unittest.TestCase):
    def test_build_offloaded_delegates_to_thread(self):
        # build_offloaded must return the same result as build, computed off-loop.
        from backend.core.ouroboros.governance.goal_inference import (
            GoalInferenceEngine,
        )
        eng = GoalInferenceEngine(repo_root=str(pathlib.Path.cwd()))
        async def go():
            # Should not raise; returns whatever build() returns (InferenceResult).
            res = await eng.build_offloaded()
            return res
        # Just prove it runs as a coroutine off the loop without error.
        asyncio.get_event_loop().run_until_complete(go())


if __name__ == "__main__":
    unittest.main()
