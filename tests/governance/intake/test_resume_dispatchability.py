"""A resumed op is re-asked whether its work can still be done.

2026-09-20: discovery learned to refuse a goal whose subject runs a program
when imported. The next boot hydrated the PREVIOUS session's suspended op for
that same goal straight into VALIDATE_RETRY — resume is the one entry that
skips every gate the original decision passed through — and three sandboxes
started the launcher script again while the Sentinel, correctly, had moved on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import environment_integrity as ei
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)
from tests.support.ast_contract import assert_calls_in_order, parse_module


def _router(tmp_path) -> UnifiedIntakeRouter:
    return UnifiedIntakeRouter(
        gls=MagicMock(), config=IntakeRouterConfig(project_root=tmp_path),
    )


def _checkpoint(targets, description=""):
    return SimpleNamespace(
        op_id="op-suspended", phase="VALIDATE_RETRY",
        target_files=list(targets), goal_description=description,
    )


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.mark.asyncio
async def test_an_op_whose_subject_runs_a_program_is_not_resumed(tmp_path):
    _write(tmp_path, "backend/start_thing.py", (
        "import subprocess\nprint('go')\nsubprocess.run(['tail', '-f', 'x'])\n"
    ))
    why = await _router(tmp_path)._resume_refusal(_checkpoint(
        ["tests/test_start_thing.py"],
        "`backend/start_thing.py` has no test module. CREATE `tests/test_start_thing.py`",
    ))
    assert why.startswith(ei.IMPORT_EXECUTES_PROGRAM)


@pytest.mark.asyncio
async def test_a_healthy_op_resumes(tmp_path):
    _write(tmp_path, "backend/good.py", "def f():\n    return 1\n")
    why = await _router(tmp_path)._resume_refusal(
        _checkpoint(["backend/good.py"], "harden `backend/good.py`"),
    )
    assert why == ""


@pytest.mark.asyncio
async def test_an_unprovisioned_dependency_does_not_strand_the_op(tmp_path):
    """An install reverses it, and the op's exploration is worth keeping."""
    _write(tmp_path, "backend/needs.py",
           "import a_package_that_is_not_installed_anywhere\n\ndef f():\n    return 1\n")
    why = await _router(tmp_path)._resume_refusal(
        _checkpoint(["backend/needs.py"], "fix `backend/needs.py`"),
    )
    assert why == ""


@pytest.mark.asyncio
async def test_a_broken_check_resumes(tmp_path, monkeypatch):
    """The recovery path fails OPEN: a gate that fails closed would strand
    every suspended op on the day it broke."""
    def boom(*_a, **_k):
        raise RuntimeError("verdict exploded")

    monkeypatch.setattr(ei, "target_import_verdict", boom)
    why = await _router(tmp_path)._resume_refusal(_checkpoint(["backend/x.py"], "x"))
    assert why == ""


@pytest.mark.parametrize("cp", [
    SimpleNamespace(), SimpleNamespace(target_files=None, goal_description=None),
    _checkpoint([], ""),
])
@pytest.mark.asyncio
async def test_degenerate_checkpoints_resume(tmp_path, cp):
    assert await _router(tmp_path)._resume_refusal(cp) == ""


def test_the_resume_loop_asks_before_it_reinjects():
    """Reachability: the check existing is not the check being consulted."""
    tree = parse_module(Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ))
    assert_calls_in_order(tree, first="_resume_refusal", then="build_resume_envelope")
    assert_calls_in_order(tree, first="_resume_refusal", then="_reinject")
