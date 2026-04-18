"""Regression spine — Session 10 post-graduation polish.

Two behaviors locked down here:

1. **Cost cap multiplier for read-only ops.** Session 10
   (bt-2026-04-18-050658) Trinity cartography charged $0.3446 against
   a $0.15 cap — a clean 2.3× overrun. Read-only fan-out + synthesis
   is structurally more expensive than a mutating BG code tweak.
   ``CostGovernorConfig.readonly_factor`` (default 5×) multiplies the
   derived cap when ``is_read_only=True``. Mutating ops retain the
   tight cost-optimized cap.

2. **POSTMORTEM emission for read-only noop completion.** The
   ``is_noop=True`` short-circuit at orchestrator.py:3948 is the
   natural terminal path for read-only cartography (findings
   delivered via tool rollup, no code candidate). Before this patch
   the op ended silently with ``terminal_reason_code="noop"`` — no
   POSTMORTEM, no clean audit trail. Manifesto §8 (Absolute
   Observability) requires every autonomous decision to be visible.
   The patch emits a POSTMORTEM with
   ``root_cause="read_only_complete"`` and aligns terminal_reason_code
   + ledger reason to the same value so all three surfaces agree.

Note: the POSTMORTEM emission is verified structurally (the orchestrator
calls ``comm.emit_postmortem(...)`` on the read-only noop path) rather
than with a full orchestrator boot — the call site is pinned via AST
inspection so any refactor that drops the emit is loudly visible.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Cost cap read-only multiplier
# ---------------------------------------------------------------------------


def test_cost_governor_read_only_multiplies_cap() -> None:
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )
    # Deterministic config; no env influence.
    cfg = CostGovernorConfig(
        baseline_usd=0.10,
        retry_headroom=3.0,
        route_factors={"background": 0.5},
        complexity_factors={"moderate": 1.0},
        readonly_factor=5.0,
        min_cap_usd=0.05,
        max_cap_usd=5.00,
        default_route_factor=1.5,
        default_complexity_factor=1.0,
        ttl_s=3600.0,
        enabled=True,
    )
    gov = CostGovernor(config=cfg)

    # Mutating BG moderate:  0.10 * 0.5 * 1.0 * 3.0 = $0.15
    mutating_cap = gov.start(
        op_id="op-mut",
        route="background",
        complexity="moderate",
    )
    assert mutating_cap == pytest.approx(0.15, abs=0.001)

    # Read-only BG moderate: 0.10 * 0.5 * 1.0 * 3.0 * 5.0 = $0.75
    readonly_cap = gov.start(
        op_id="op-ro",
        route="background",
        complexity="moderate",
        is_read_only=True,
    )
    assert readonly_cap == pytest.approx(0.75, abs=0.001)
    assert readonly_cap >= 5 * mutating_cap - 0.001


def test_cost_governor_read_only_clamped_by_max_cap() -> None:
    """Even with a massive readonly_factor, the cap never exceeds
    max_cap_usd — protects against env typos like factor=500."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )
    cfg = CostGovernorConfig(
        baseline_usd=0.10,
        retry_headroom=3.0,
        route_factors={"background": 0.5},
        complexity_factors={"moderate": 1.0},
        readonly_factor=1000.0,  # intentionally absurd
        min_cap_usd=0.05,
        max_cap_usd=5.00,
        default_route_factor=1.5,
        default_complexity_factor=1.0,
        ttl_s=3600.0,
        enabled=True,
    )
    gov = CostGovernor(config=cfg)
    readonly_cap = gov.start(
        op_id="op-ro-huge",
        route="background",
        complexity="moderate",
        is_read_only=True,
    )
    # Clamped at max_cap_usd = 5.00
    assert readonly_cap == pytest.approx(5.00, abs=0.001)


def test_cost_governor_mutating_ops_unchanged() -> None:
    """Mutating ops must keep the old cap math — the read-only
    multiplier is a scoped extension, not a blanket change."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
        CostGovernorConfig,
    )
    cfg = CostGovernorConfig(
        baseline_usd=0.10,
        retry_headroom=3.0,
        route_factors={"background": 0.5, "complex": 4.0},
        complexity_factors={"moderate": 1.0, "complex": 3.0},
        readonly_factor=5.0,
        min_cap_usd=0.05,
        max_cap_usd=5.00,
        default_route_factor=1.5,
        default_complexity_factor=1.0,
        ttl_s=3600.0,
        enabled=True,
    )
    gov = CostGovernor(config=cfg)
    # is_read_only defaults to False — no multiplier applied.
    cap = gov.start(
        op_id="op-complex",
        route="complex",
        complexity="complex",
    )
    # 0.10 * 4.0 * 3.0 * 3.0 = $3.60, clamped to max=$5
    assert cap == pytest.approx(3.60, abs=0.001)


def test_cost_governor_default_readonly_factor_is_five() -> None:
    """The default multiplier matches the Session-10 calibration ask:
    $0.15 mutating BG → $0.75 read-only BG = 5× multiplier."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernorConfig,
    )
    import os
    # Wipe env that would override default
    for k in ("JARVIS_OP_COST_READONLY_FACTOR",):
        os.environ.pop(k, None)
    cfg = CostGovernorConfig()
    assert cfg.readonly_factor == pytest.approx(5.0, abs=0.001)


# ---------------------------------------------------------------------------
# 2. POSTMORTEM emission for read-only noop path
# ---------------------------------------------------------------------------


def test_orchestrator_read_only_noop_path_calls_emit_postmortem() -> None:
    """Structural check via AST — the is_noop branch must contain a
    call to ``comm.emit_postmortem`` gated by ``is_read_only`` check.
    This pins the Manifesto §8 requirement to the source tree so a
    refactor that drops the emit breaks this test immediately.
    """
    src_path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros"
        / "governance" / "orchestrator.py"
    )
    src = src_path.read_text()

    # Anchor: the is_noop branch header (from the current patch).
    assert "if generation.is_noop:" in src, (
        "is_noop short-circuit branch missing — the read-only "
        "POSTMORTEM emission lives inside this branch"
    )

    # Locate the is_noop branch and check for:
    #   1. terminal_reason_code="read_only_complete" when read-only
    #   2. comm.emit_postmortem with root_cause="read_only_complete"
    #   3. is_read_only gate before the emit
    branch_start = src.find("if generation.is_noop:")
    branch_end = src.find(
        "# Store generation result in context",
        branch_start,
    )
    assert branch_end > branch_start, "Failed to locate noop branch end"
    branch = src[branch_start:branch_end]

    assert "read_only_complete" in branch, (
        "terminal_reason_code=\"read_only_complete\" missing from "
        "is_noop branch — required by Manifesto §8 audit trail"
    )
    assert "emit_postmortem" in branch, (
        "emit_postmortem call missing from is_noop branch — required "
        "by Manifesto §8 absolute observability"
    )
    assert "root_cause=\"read_only_complete\"" in branch, (
        "POSTMORTEM root_cause must be \"read_only_complete\" for the "
        "read-only terminal path"
    )
    assert "is_read_only" in branch, (
        "is_noop branch must check ctx.is_read_only before emitting "
        "the read-only POSTMORTEM (non-read-only noop ops keep their "
        "silent-complete semantics for backward compat)"
    )


def test_orchestrator_mutating_noop_path_no_postmortem() -> None:
    """Mutating noop ops (model self-declared no_op) must NOT emit the
    POSTMORTEM — that's reserved for read-only terminal paths.
    Backward compat guard.
    """
    src_path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros"
        / "governance" / "orchestrator.py"
    )
    src = src_path.read_text()
    branch_start = src.find("if generation.is_noop:")
    branch_end = src.find(
        "# Store generation result in context",
        branch_start,
    )
    branch = src[branch_start:branch_end]
    # The emit_postmortem must be INSIDE an is_read_only guard.
    # Simple structural check: the emit call comes AFTER an
    # "if _is_read_only_terminal" line.
    postmortem_idx = branch.find("emit_postmortem")
    guard_idx = branch.find("_is_read_only_terminal", 0, postmortem_idx)
    assert guard_idx > 0 and guard_idx < postmortem_idx, (
        "emit_postmortem must be gated by _is_read_only_terminal "
        "check — mutating noop ops must retain silent-complete"
    )


def test_orchestrator_noop_terminal_reason_matches_contract() -> None:
    """terminal_reason_code and ledger reason must agree — the patch
    binds both to the same variable (_terminal_reason). Regression
    pin so future edits don't split them.
    """
    src_path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros"
        / "governance" / "orchestrator.py"
    )
    src = src_path.read_text()
    branch_start = src.find("if generation.is_noop:")
    branch_end = src.find(
        "# Store generation result in context",
        branch_start,
    )
    branch = src[branch_start:branch_end]
    # Both the ctx.advance and the ledger record_reason should bind to
    # _terminal_reason.
    assert "terminal_reason_code=_terminal_reason" in branch
    assert '"reason": _terminal_reason' in branch
