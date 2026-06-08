"""Slice 167 — Sovereign Payload Threading Matrix.

A governance floor that a mid-pipeline target overwrite can blind is a critical
vulnerability: the live soak proved a cage-touching op declares its target at intake,
but the generation phase overwrites ctx.target_files (with the candidate's files), so
by the time the floor runs the declared governance target is gone → the boundary never
fires → the op auto-applies past the approval gate.

Fix: the targets declared at inception are IMMUTABLE (ctx.declared_targets, set in
OperationContext.create and never overwritten). ctx.effective_targets() returns the
UNION of declared + current target_files, so the governance floor evaluates BOTH — a
generated candidate cannot mask a declared cage target to bypass approval.
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance.op_context import OperationContext

_CAGE = "backend/core/ouroboros/governance/semantic_guardian.py"


def _ctx(targets):
    return OperationContext.create(target_files=tuple(targets), description="d")


class TestDeclaredTargetsImmutable(unittest.TestCase):
    def test_declared_targets_set_at_inception(self):
        ctx = _ctx([_CAGE])
        self.assertEqual(ctx.declared_targets, (_CAGE,))

    def test_declared_targets_survive_a_context_update(self):
        ctx = _ctx([_CAGE])
        # any with_* uses dataclasses.replace → declared_targets is preserved
        ctx2 = ctx.with_expanded_files(("some/other/file.py",))
        self.assertEqual(ctx2.declared_targets, (_CAGE,))


class TestEffectiveTargetsUnion(unittest.TestCase):
    def test_union_includes_declared_even_if_target_files_overwritten(self):
        import dataclasses
        ctx = _ctx([_CAGE])
        # simulate the generation phase overwriting target_files with candidate files
        ctx_overwritten = dataclasses.replace(ctx, target_files=("generated/candidate.py",))
        eff = ctx_overwritten.effective_targets()
        self.assertIn(_CAGE, eff)                       # declared cage target survives
        self.assertIn("generated/candidate.py", eff)    # + the candidate files
        # the masking attack fails: the floor will see the cage target
        self.assertEqual(ctx_overwritten.target_files, ("generated/candidate.py",))

    def test_union_dedups(self):
        ctx = _ctx([_CAGE])
        self.assertEqual(ctx.effective_targets().count(_CAGE), 1)


class TestSlice4bUsesUnion(unittest.TestCase):
    def test_slice4b_floor_evaluates_effective_targets(self):
        import backend.core.ouroboros.governance.phase_runners.slice4b_runner as S4B
        src = open(S4B.__file__).read()
        self.assertIn("effective_targets", src)


if __name__ == "__main__":
    unittest.main()
