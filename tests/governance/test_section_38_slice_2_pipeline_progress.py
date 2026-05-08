"""§38 Slice 2 (PRD v2.58→v2.59, 2026-05-07) — pipeline progress
bar regression spine.

Covers:

  * Master flag default-FALSE
  * forward_flow_phases composition (subset of canonical
    OperationPhase enum)
  * forward_flow_length pinned to 11
  * phase_index lookup (enum / string / out-of-flow / None)
  * format_pipeline_progress shape for each forward-flow phase
  * Out-of-flow + master-off render contracts
  * status_line _format_pipeline_progress_token integration
  * 5 AST pins (master_default_false / authority_asymmetry /
    forward_flow_subset_of_OperationPhase /
    forward_flow_length_eleven /
    composes_canonical_op_context)
  * Reachability proof — every forward-flow phase is reachable
    from CLASSIFY via PHASE_TRANSITIONS (no orphaned entries)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_2(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_PIPELINE_PROGRESS_FILLED_GLYPH", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_PIPELINE_PROGRESS_EMPTY_GLYPH", raising=False,
    )
    from backend.core.ouroboros.governance import (
        pipeline_progress as pp,
    )
    pp.reset_forward_flow_cache_for_tests()
    yield
    pp.reset_forward_flow_cache_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "on"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        master_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", value,
    )
    assert master_enabled() is True


# ---------------------------------------------------------------------------
# Canonical forward-flow tuple
# ---------------------------------------------------------------------------


def test_forward_flow_length_is_11():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        forward_flow_length,
    )
    assert forward_flow_length() == 11


def test_forward_flow_phases_are_canonical_OperationPhase_members():
    """Every entry MUST be a valid OperationPhase enum member."""
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        forward_flow_phases,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    flow = forward_flow_phases()
    for phase in flow:
        assert isinstance(phase, OperationPhase)


def test_forward_flow_canonical_order():
    """The 11-phase forward-flow order matches CLAUDE.md doc."""
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        forward_flow_phases,
    )
    expected = [
        "CLASSIFY",
        "ROUTE",
        "CONTEXT_EXPANSION",
        "PLAN",
        "GENERATE",
        "VALIDATE",
        "GATE",
        "APPROVE",
        "APPLY",
        "VERIFY",
        "COMPLETE",
    ]
    actual = [p.name for p in forward_flow_phases()]
    assert actual == expected


def test_forward_flow_excludes_retry_and_terminal_states():
    """Retry/terminal/error states (GENERATE_RETRY,
    VALIDATE_RETRY, VISUAL_VERIFY, CANCELLED, EXPIRED,
    POSTMORTEM) MUST NOT appear in forward-flow."""
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        forward_flow_phases,
    )
    excluded = {
        "GENERATE_RETRY",
        "VALIDATE_RETRY",
        "VISUAL_VERIFY",
        "CANCELLED",
        "EXPIRED",
        "POSTMORTEM",
    }
    flow_names = {p.name for p in forward_flow_phases()}
    assert flow_names.isdisjoint(excluded)


def test_forward_flow_reachable_from_CLASSIFY():
    """Every forward-flow phase MUST be reachable from CLASSIFY
    via canonical PHASE_TRANSITIONS — no orphaned entries.

    Operator binding "build cleanly on existing files" — the
    forward-flow tuple is structurally consistent with the
    canonical transition graph."""
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
        PHASE_TRANSITIONS,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        forward_flow_phases,
    )
    flow = forward_flow_phases()
    # BFS from CLASSIFY through PHASE_TRANSITIONS.
    reachable = {OperationPhase.CLASSIFY}
    queue = [OperationPhase.CLASSIFY]
    while queue:
        node = queue.pop(0)
        for nxt in PHASE_TRANSITIONS.get(node, set()):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    # Every forward-flow phase is reachable from CLASSIFY.
    for p in flow:
        assert p in reachable, (
            f"forward-flow phase {p.name} not reachable from "
            f"CLASSIFY"
        )


# ---------------------------------------------------------------------------
# phase_index
# ---------------------------------------------------------------------------


def test_phase_index_for_each_forward_flow_phase():
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
        forward_flow_phases,
    )
    flow = forward_flow_phases()
    for i, p in enumerate(flow):
        assert phase_index(p) == i


def test_phase_index_string_fallback():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
    )
    assert phase_index("GENERATE") == 4  # 5th phase, 0-indexed
    assert phase_index("complete") == 10  # case-insensitive


def test_phase_index_returns_none_for_out_of_flow():
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
    )
    assert phase_index(OperationPhase.GENERATE_RETRY) is None
    assert phase_index(OperationPhase.CANCELLED) is None
    assert phase_index(OperationPhase.POSTMORTEM) is None


def test_phase_index_returns_none_for_none():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
    )
    assert phase_index(None) is None


def test_phase_index_returns_none_for_unknown_string():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
    )
    assert phase_index("UNKNOWN_PHASE_NAME") is None


def test_phase_index_defensive_on_bad_input():
    """NEVER raises on bad inputs."""
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        phase_index,
    )
    for bad in (42, [], {}, object()):
        result = phase_index(bad)
        assert result is None or isinstance(result, int)


# ---------------------------------------------------------------------------
# format_pipeline_progress
# ---------------------------------------------------------------------------


def test_format_master_off_returns_empty():
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    assert format_pipeline_progress(OperationPhase.GENERATE) == ""


def test_format_classify_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    # CLASSIFY is index 0 → 1 filled / 10 empty.
    rendered = format_pipeline_progress(OperationPhase.CLASSIFY)
    assert rendered == "[●○○○○○○○○○○] CLASSIFY 1/11"


def test_format_complete_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    # COMPLETE is index 10 → 11 filled / 0 empty.
    rendered = format_pipeline_progress(OperationPhase.COMPLETE)
    assert rendered == "[●●●●●●●●●●●] COMPLETE 11/11"


def test_format_generate_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    # GENERATE is index 4 → 5 filled / 6 empty.
    rendered = format_pipeline_progress(OperationPhase.GENERATE)
    assert rendered == "[●●●●●○○○○○○] GENERATE 5/11"


def test_format_out_of_flow_phase_zero_filled(monkeypatch):
    """Out-of-flow phases (e.g., GENERATE_RETRY) render with
    zero filled glyphs (no name/position appended)."""
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    rendered = format_pipeline_progress(
        OperationPhase.GENERATE_RETRY,
    )
    # Just the bar with all empty glyphs.
    assert rendered == "[○○○○○○○○○○○]"


def test_format_string_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    # Status snapshot passes phase as a string ("GENERATE").
    rendered = format_pipeline_progress("GENERATE")
    assert "GENERATE" in rendered
    assert "5/11" in rendered


def test_format_show_position_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    rendered = format_pipeline_progress(
        OperationPhase.GENERATE,
        show_position=False,
    )
    # No "5/11" position token.
    assert "5/11" not in rendered
    assert "GENERATE" in rendered


def test_format_show_phase_name_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    rendered = format_pipeline_progress(
        OperationPhase.GENERATE,
        show_phase_name=False,
    )
    # No "GENERATE" phase name (only bar + position).
    assert "GENERATE" not in rendered
    assert "5/11" in rendered


def test_format_glyph_env_overridable(monkeypatch):
    """Operator binding "no hardcoding" — glyphs override via env."""
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_FILLED_GLYPH", "X",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_EMPTY_GLYPH", ".",
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationPhase,
    )
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        format_pipeline_progress,
    )
    rendered = format_pipeline_progress(
        OperationPhase.GENERATE,
    )
    assert "[XXXXX......]" in rendered  # 5 X + 6 .


# ---------------------------------------------------------------------------
# Status-line integration
# ---------------------------------------------------------------------------


def test_status_line_progress_token_appears_after_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="standard",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=False)
    # Phase token appears, then progress bar.
    phase_idx = rendered.find("Phase: GENERATE")
    bar_idx = rendered.find("[●")
    assert phase_idx >= 0
    assert bar_idx > phase_idx  # bar AFTER phase


def test_status_line_master_off_no_progress_bar(monkeypatch):
    """Master off → bar NOT in render."""
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", raising=False,
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=False)
    assert "[●" not in rendered
    assert "[○" not in rendered


def test_status_line_compact_omits_progress_bar(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=True)
    assert "[●" not in rendered


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def _progress_pins():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _progress_source():
    return Path(
        "backend/core/ouroboros/governance/pipeline_progress.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _progress_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _progress_pins()
    src = _progress_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _progress_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _progress_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_forward_flow_length_fires_on_wrong_count():
    pins = _progress_pins()
    pin = next(
        p for p in pins
        if "forward_flow_length_eleven" in p.invariant_name
    )
    bad_src = (
        "_FORWARD_FLOW_PHASE_NAMES = (\n"
        "    'CLASSIFY', 'ROUTE', 'GENERATE',\n"  # only 3
        ")\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("length is 3" in v for v in violations)


def test_pin_composes_op_context_fires_on_missing_compose():
    pins = _progress_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_op_context" in p.invariant_name
    )
    bad_src = "x = 1\n"  # no op_context reference
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("op_context" in v for v in violations)


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 4 glyph knobs.
    assert n == 5
    names = {c["name"] for c in reg.calls}
    assert "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED" in names
