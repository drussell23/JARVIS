"""Task #11 regression spine — StrategicDirection injection wiring.

Root cause of the P3 soak FAIL (bt-2026-05-18-092457:
`[Strategic] dev-memory injected`=0, `Strategic direction
injected`=0, `…failed`=0 → block *skipped*, not raised):

    GovernedLoopService._attach_to_stack() bound
    orchestrator/generator/approval_provider onto the stack but
    NEVER `self._stack.governed_loop_service = self`. Only
    hud_governance_boot wrote that attr — and the battle-test
    harness doesn't use that boot path. So the harness set
    gls._strategic_direction (harness.py:1903) while the production
    read path (orchestrator._run_pipeline → getattr(stack,
    "governed_loop_service")._strategic_direction) saw None →
    the ENTIRE StrategicDirection injection was dark.

Fix: the GLS self-registers on attach (single source of truth — no
second StrategicDirection holder on the orchestrator).

Pins:
  * _attach_to_stack sets stack.governed_loop_service = self
  * _detach_from_stack clears it to None
  * AST pin: both assignments present (survives refactor)
  * end-to-end: wired stack + enabled flag → the production
    resolution snippet reaches format_for_prompt → dev-memory
    telemetry fires (the soak proof line, at unit speed)
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopService,
)
from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)

_REPO = Path(__file__).resolve().parents[2]
_GLS_SRC = _REPO / "backend/core/ouroboros/governance/governed_loop_service.py"
# Both genuine strategic-injection sites (the block is pre-duplicated
# across the orchestrator inline path AND the CLASSIFY phase runner —
# the battle-test ops traverse the latter). Observability + op_id
# parity must hold at BOTH or graduation greps silently miss.
_STRAT_INJECT_SITES = (
    _REPO / "backend/core/ouroboros/governance/orchestrator.py",
    _REPO / "backend/core/ouroboros/governance/phase_runners/classify_runner.py",
)
_STRAT_LOGGER = "backend.core.ouroboros.governance.strategic_direction"
_FLAG = "JARVIS_STRATEGIC_DEV_MEMORY_ENABLED"


def _bare_gls(stack) -> GovernedLoopService:
    """A GLS with just the attrs _attach_to_stack touches — no heavy
    __init__ (it pulls the whole governance world)."""
    g = GovernedLoopService.__new__(GovernedLoopService)
    g._stack = stack
    g._orchestrator = SimpleNamespace(name="orch")
    g._generator = SimpleNamespace(name="gen")
    g._approval_provider = SimpleNamespace(name="appr")
    return g


# ---------------------------------------------------------------------------
# Stack self-registration
# ---------------------------------------------------------------------------

def test_attach_sets_governed_loop_service_legacy_stack():
    stack = SimpleNamespace()  # no bind_orchestrator → legacy branch
    g = _bare_gls(stack)
    g._attach_to_stack()
    assert stack.governed_loop_service is g
    g._detach_from_stack()
    assert stack.governed_loop_service is None


def test_attach_sets_governed_loop_service_bind_contract_stack():
    stack = SimpleNamespace(bind_orchestrator=MagicMock())
    g = _bare_gls(stack)
    g._attach_to_stack()
    stack.bind_orchestrator.assert_called_once_with(g._orchestrator)
    assert stack.governed_loop_service is g
    g._detach_from_stack()
    assert stack.governed_loop_service is None


def test_attach_noop_when_stack_none():
    g = _bare_gls(None)
    g._attach_to_stack()   # must not raise
    g._detach_from_stack()  # must not raise


# ---------------------------------------------------------------------------
# AST pin — the assignment must survive refactors
# ---------------------------------------------------------------------------

def _assigns_gls(fn_name: str) -> bool:
    tree = ast.parse(_GLS_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == fn_name
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "governed_loop_service"
                ):
                    return True
    return False


def test_ast_pin_attach_and_detach_assign_governed_loop_service():
    assert _assigns_gls("_attach_to_stack"), (
        "_attach_to_stack MUST self-register stack.governed_loop_service"
    )
    assert _assigns_gls("_detach_from_stack"), (
        "_detach_from_stack MUST clear stack.governed_loop_service"
    )


# ---------------------------------------------------------------------------
# End-to-end: the production resolution snippet now reaches injection
# ---------------------------------------------------------------------------

def test_wired_stack_resolves_strategic_and_fires_dev_memory(
    monkeypatch, tmp_path, caplog,
):
    # Curated repo memory/ + enabled flag (the P3 conditions).
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "plan.md").write_text("# Plan\nSECRET_BODY", encoding="utf-8")
    monkeypatch.setenv(_FLAG, "true")

    svc = StrategicDirectionService(tmp_path)
    svc._digest = "PRINCIPLES"
    svc._loaded = True
    assert svc.is_loaded

    # GLS attaches → stack.governed_loop_service is the gls; the gls
    # carries _strategic_direction exactly as harness.py:1903 sets it.
    stack = SimpleNamespace()
    gls = _bare_gls(stack)
    gls._strategic_direction = svc
    gls._attach_to_stack()

    # Replay the orchestrator._run_pipeline read path verbatim.
    _gls = getattr(stack, "governed_loop_service", None)
    _strategic_svc = getattr(_gls, "_strategic_direction", None)
    assert _strategic_svc is not None and _strategic_svc.is_loaded, (
        "production read path must resolve the loaded service"
    )
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        out = _strategic_svc.format_for_prompt(op_id="op-int")

    assert "## Recent Developer Memory" in out
    line = next(
        (r.getMessage() for r in caplog.records
         if "dev-memory injected" in r.getMessage()), None,
    )
    assert line is not None and "op=op-int" in line, (
        "wiring fix must make the dev-memory telemetry reachable"
    )
    assert "SECRET_BODY" not in line  # counts-only discipline intact


# ---------------------------------------------------------------------------
# AST pin — BOTH strategic-injection sites: op_id threaded + INFO (not
# DEBUG). Pins the exact P3-soak-#1 defect (op= empty, DEBUG-only).
# ---------------------------------------------------------------------------

import pytest as _pytest


@_pytest.mark.parametrize("src_path", _STRAT_INJECT_SITES, ids=lambda p: p.name)
def test_ast_pin_strategic_injection_site_threads_op_id_and_logs_info(src_path):
    tree = ast.parse(src_path.read_text())
    # Find the format_for_prompt call that feeds the strategic block.
    fp_calls = [
        c for c in ast.walk(tree)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "format_for_prompt"
    ]
    strat_fp = [c for c in fp_calls if any(
        kw.arg == "op_id" for kw in c.keywords
    )]
    assert strat_fp, (
        f"{src_path.name}: a format_for_prompt(op_id=...) call must "
        f"exist — soak #1 fired with op= empty because this site "
        f"called format_for_prompt() bare"
    )
    # The success log for this injection must be logger.info, never
    # logger.debug (graduation greps must not depend on DEBUG).
    info_msgs = [
        ast.unparse(c.args[0])
        for c in ast.walk(tree)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "info"
        and c.args
        and isinstance(c.args[0], ast.Constant)
    ]
    debug_msgs = [
        ast.unparse(c.args[0])
        for c in ast.walk(tree)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "debug"
        and c.args
        and isinstance(c.args[0], ast.Constant)
    ]
    assert any("Strategic direction injected" in m for m in info_msgs), (
        f"{src_path.name}: 'Strategic direction injected' success "
        f"line must be logger.info (§7 graduation observability)"
    )
    assert not any(
        "Strategic direction injected (" in m for m in debug_msgs
    ), (
        f"{src_path.name}: the OLD DEBUG '(%d principles)' success "
        f"line must be gone (it hid the dark path in soak #1)"
    )
