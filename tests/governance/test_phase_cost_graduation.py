"""Graduation pins — Per-Phase Cost Drill-Down arc.

The critical pin is the backward-compat guarantee on
:meth:`CostGovernor.charge`: charges without a ``phase`` kwarg
must produce byte-for-byte identical budget state vs the
pre-Slice-2 behavior (cumulative_usd, cap_usd, remaining_usd,
exceeded, call_count, provider_totals).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# §1 Authority — new modules import no gate/execution code
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/phase_cost.py",
    "backend/core/ouroboros/governance/cost_repl.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_phase_cost_schema_version_pinned():
    from backend.core.ouroboros.governance.phase_cost import (
        PHASE_COST_SCHEMA_VERSION,
    )
    assert PHASE_COST_SCHEMA_VERSION == "phase_cost.v1"


# ===========================================================================
# CRITICAL: backward-compat budget behavior
# ===========================================================================


def test_charge_without_phase_produces_identical_budget_state():
    """The core backward-compat guarantee: pre-Slice-2 callers that
    invoke `charge(op_id, cost, provider)` without the phase kwarg
    MUST see byte-for-byte the same budget state."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    cfg = CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    )
    g1 = CostGovernor(cfg)
    g2 = CostGovernor(cfg)
    g1.start("op-1", route="standard", complexity="light")
    g2.start("op-1", route="standard", complexity="light")

    charges = [(0.05, "claude"), (0.12, "claude"), (0.33, "doubleword")]
    for amount, provider in charges:
        g1.charge("op-1", amount, provider)  # no phase
        g2.charge("op-1", amount, provider, phase="GENERATE")

    s1 = g1.summary("op-1")
    s2 = g2.summary("op-1")
    # Budget-critical fields — must match.
    for key in (
        "cumulative_usd", "cap_usd", "remaining_usd",
        "call_count", "exceeded", "provider_totals",
    ):
        assert s1[key] == s2[key], (
            f"key={key} diverges: {s1[key]} vs {s2[key]}"
        )


def test_is_exceeded_unchanged_across_phased_and_unphased():
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    cfg = CostGovernorConfig(
        enabled=True, baseline_usd=0.05,
        max_cap_usd=0.05, min_cap_usd=0.05,
    )
    g1 = CostGovernor(cfg)
    g2 = CostGovernor(cfg)
    g1.start("op-1", route="standard", complexity="light")
    g2.start("op-1", route="standard", complexity="light")
    g1.charge("op-1", 0.10, "claude")
    g2.charge("op-1", 0.10, "claude", phase="GENERATE")
    assert g1.is_exceeded("op-1") == g2.is_exceeded("op-1") is True


def test_remaining_unchanged_across_phased_and_unphased():
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    g1 = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g2 = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g1.start("op-1", route="standard", complexity="light")
    g2.start("op-1", route="standard", complexity="light")
    g1.charge("op-1", 0.25, "claude")
    g2.charge("op-1", 0.25, "claude", phase="VERIFY")
    assert g1.remaining("op-1") == g2.remaining("op-1")


# ===========================================================================
# Observer registry stability
# ===========================================================================


def test_observer_dispatch_survives_raising_observer():
    """Pin: a bad observer never breaks finalize() — essential since
    finalize is the cost-persistence lifeline."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
        register_finalize_observer, reset_finalize_observers,
    )
    reset_finalize_observers()

    def _boom(op, s):
        raise RuntimeError("observer exploded")

    register_finalize_observer(_boom)
    good = []
    register_finalize_observer(lambda op, s: good.append(op))
    g = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g.start("op-1", route="standard", complexity="light")
    g.charge("op-1", 0.05, "claude")
    summary = g.finish("op-1")
    # Bad observer didn't kill finalize; good observer still ran
    assert summary is not None
    assert good == ["op-1"]
    reset_finalize_observers()


# ===========================================================================
# Summary JSON schema keys additive — old consumers unaffected
# ===========================================================================


def test_summary_omits_cost_keys_when_no_ops_observed(tmp_path: Path):
    """Pre-Slice-3 consumers (old battle-test sessions, archived
    summary.json) should never see the new keys injected."""
    import json
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )
    from backend.core.ouroboros.governance.cost_governor import (
        reset_finalize_observers,
    )
    reset_finalize_observers()
    recorder = SessionRecorder(session_id="bt-pin-empty")
    recorder.save_summary(
        output_dir=tmp_path,
        stop_reason="complete",
        duration_s=1.0,
        cost_total=0.0,
        cost_breakdown={},
        branch_stats={},
        convergence_state="IMPROVING",
        convergence_slope=0.0,
        convergence_r2=0.0,
    )
    raw = json.loads((tmp_path / "summary.json").read_text())
    for forbidden_key in (
        "cost_by_phase", "cost_by_op_phase",
        "cost_by_op_phase_provider", "cost_unknown_phase_by_op",
    ):
        assert forbidden_key not in raw, (
            f"empty session leaked key: {forbidden_key}"
        )


# ===========================================================================
# SessionRecord projection additive — safe defaults
# ===========================================================================


def test_session_record_cost_fields_default_empty():
    from backend.core.ouroboros.governance.session_record import (
        SessionRecord,
    )
    rec = SessionRecord(session_id="bt-default")
    assert rec.cost_by_phase == {}
    assert rec.cost_by_op_phase == {}


def test_session_record_project_exposes_has_phase_cost_data_flag():
    from backend.core.ouroboros.governance.session_record import (
        SessionRecord,
    )
    rec_empty = SessionRecord(session_id="bt-1")
    rec_has = SessionRecord(
        session_id="bt-2", cost_by_phase={"GENERATE": 0.5},
    )
    assert rec_empty.project()["has_phase_cost_data"] is False
    assert rec_has.project()["has_phase_cost_data"] is True


# ===========================================================================
# /cost REPL — verb surface stable
# ===========================================================================


@pytest.mark.parametrize("verb,expect_ok", [
    ("help", True),
    ("session foo", False),  # unknown session id -> ok=False error
    ("op-ghost", False),     # no governor -> ok=False error
])
def test_cost_repl_verbs_stable(verb: str, expect_ok: bool):
    from backend.core.ouroboros.governance.cost_repl import (
        dispatch_cost_command,
    )
    res = dispatch_cost_command(f"/cost {verb}")
    assert res.matched is True
    assert res.ok is expect_ok


def test_cost_repl_help_names_every_surface():
    """The help text is the discoverability surface — pin its content."""
    from backend.core.ouroboros.governance.cost_repl import (
        dispatch_cost_command,
    )
    res = dispatch_cost_command("/cost help")
    assert res.ok
    for keyword in ("session", "op-id", "phase"):
        assert keyword in res.text.lower()


# ===========================================================================
# Docstring bit-rot
# ===========================================================================


def test_phase_cost_module_docstring_mentions_drill_down():
    import backend.core.ouroboros.governance.phase_cost as m
    doc = (m.__doc__ or "").lower()
    assert "drill-down" in doc or "per-phase" in doc


def test_cost_repl_module_docstring_mentions_why_question():
    import backend.core.ouroboros.governance.cost_repl as m
    doc = (m.__doc__ or "").lower()
    # The gap statement is "why did this op cost $X" — the module
    # that answers it should name that question in its docstring.
    assert "why" in doc and "cost" in doc


# ===========================================================================
# Canonical phase order stable
# ===========================================================================


def test_canonical_phase_order_has_expected_members():
    from backend.core.ouroboros.governance.phase_cost import (
        CANONICAL_PHASE_ORDER,
    )
    # Every op_context.OperationPhase.name must appear (best-effort
    # check against the enum so we catch rename drift).
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    missing = [
        p.name for p in OperationPhase
        if p.name not in CANONICAL_PHASE_ORDER
    ]
    assert missing == [], (
        f"phases missing from CANONICAL_PHASE_ORDER: {missing}"
    )
