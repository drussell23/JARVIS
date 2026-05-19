"""Slice 4 #2 spine — structural §1 closure pin.

Proves BOTH directions:
  * GREEN on the real shipped auto_committer.py + harness.py today
    (the §1 runtime defenses are structurally present)
  * RED on synthetic regressions (gate-after-staging / no-gate /
    missing-literal-autonomous / harness-without-owned-workspace)
    — a pin that cannot fail is worthless
"""
from __future__ import annotations

import ast
from pathlib import Path

from backend.core.ouroboros.governance import (
    harness_sovereignty_pin as pin,
)
from backend.core.ouroboros.governance import auto_committer as ac

import backend.core.ouroboros.battle_test.harness as hn


def _invs():
    invs = pin.register_shipped_invariants()
    return {i.invariant_name: i for i in invs}


def test_registers_two_invariants():
    invs = pin.register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "autocommitter_pre_stage_sovereignty_gate",
        "harness_owned_workspace_boot_phase",
    }


def test_green_on_real_autocommitter_source():
    src = Path(ac.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    inv = _invs()["autocommitter_pre_stage_sovereignty_gate"]
    assert inv.validate(tree, src) == (), (
        "§1 pre-stage gate must be structurally present today"
    )


def test_green_on_real_harness_source():
    src = Path(hn.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    inv = _invs()["harness_owned_workspace_boot_phase"]
    assert inv.validate(tree, src) == (), (
        "harness owned-workspace boot phase must be present today"
    )


# --------------------------------------------------------------------------
# Negative: the pin MUST catch §1 regressions
# --------------------------------------------------------------------------


def test_catches_gate_after_staging():
    bad = (
        "async def commit(self):\n"
        "    await self._run(['git', 'add', '.'])\n"
        "    verify_pre_commit(ctx)  # too late — index already touched\n"
        "    x = 'autonomous'\n"
    )
    inv = _invs()["autocommitter_pre_stage_sovereignty_gate"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("does NOT precede" in s for s in v), v


def test_catches_missing_gate():
    bad = (
        "async def commit(self):\n"
        "    await self._run(['git', 'add', '.'])\n"
        "    x = 'autonomous'\n"
    )
    inv = _invs()["autocommitter_pre_stage_sovereignty_gate"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("does NOT compose" in s for s in v), v


def test_catches_missing_literal_autonomous():
    bad = (
        "async def commit(self):\n"
        "    verify_pre_commit(ctx)\n"
        "    await self._run(['git', 'add', '.'])\n"
    )
    inv = _invs()["autocommitter_pre_stage_sovereignty_gate"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("LITERAL channel='autonomous'" in s for s in v), v


def test_catches_missing_commit_fn():
    bad = "def something_else():\n    pass\n"
    inv = _invs()["autocommitter_pre_stage_sovereignty_gate"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("commit() not found" in s for s in v), v


def test_catches_harness_without_owned_workspace_call():
    bad = (
        "class H:\n"
        "    async def _boot_ledger_sovereignty_workspace(self):\n"
        "        pass\n"
        "    async def run(self):\n"
        "        await self.boot_oracle()\n"
        "JARVIS_AUTO_COMMIT_WORKSPACE = 'x'\n"
    )
    inv = _invs()["harness_owned_workspace_boot_phase"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("never invoked" in s for s in v), v


def test_catches_harness_without_env_seam():
    bad = (
        "class H:\n"
        "    async def _boot_ledger_sovereignty_workspace(self):\n"
        "        pass\n"
        "    async def run(self):\n"
        "        await self._boot_ledger_sovereignty_workspace()\n"
    )
    inv = _invs()["harness_owned_workspace_boot_phase"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("JARVIS_AUTO_COMMIT_WORKSPACE" in s for s in v), v


def test_catches_harness_method_removed():
    bad = (
        "class H:\n"
        "    async def run(self):\n"
        "        pass\n"
        "JARVIS_AUTO_COMMIT_WORKSPACE = 'x'\n"
    )
    inv = _invs()["harness_owned_workspace_boot_phase"]
    v = inv.validate(ast.parse(bad), bad)
    assert any("method removed" in s for s in v), v
