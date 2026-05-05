"""Tests for Gap #6 Slice 5 graduation."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.narrative_channel import (
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.intent_prompter import (
    MASTER_FLAG_ENV_VAR as INTENT_FLAG,
    is_master_flag_enabled as intent_enabled,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(INTENT_FLAG, raising=False)
    monkeypatch.delenv("JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED", raising=False)
    yield


# ===========================================================================
# Master flags graduated default-true
# ===========================================================================


def test_intent_master_flag_default_on():
    assert intent_enabled() is True


def test_intent_master_flag_explicit_off(monkeypatch):
    monkeypatch.setenv(INTENT_FLAG, "false")
    assert intent_enabled() is False


# ===========================================================================
# register_flags — 5 specs
# ===========================================================================


class _StubRegistry:
    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_seeds_five_specs():
    reg = _StubRegistry()
    count = register_flags(reg)
    assert count == 5
    names = {spec.name for spec in reg.registered}
    assert names == {
        "JARVIS_NARRATIVE_INTENT_ENABLED",
        "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED",
        "JARVIS_NARRATIVE_BUFFER_SIZE",
        "JARVIS_NARRATIVE_INTENT_TIMEOUT_S",
        "JARVIS_NARRATIVE_INTENT_MAX_TOKENS",
    }


def test_master_flags_default_true():
    reg = _StubRegistry()
    register_flags(reg)
    for name in (
        "JARVIS_NARRATIVE_INTENT_ENABLED",
        "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED",
    ):
        spec = next(s for s in reg.registered if s.name == name)
        assert spec.default is True


def test_buffer_size_default_200():
    reg = _StubRegistry()
    register_flags(reg)
    spec = next(s for s in reg.registered if s.name == "JARVIS_NARRATIVE_BUFFER_SIZE")
    assert spec.default == 200


def test_intent_defaults_match_module_constants():
    reg = _StubRegistry()
    register_flags(reg)
    timeout = next(s for s in reg.registered if s.name == "JARVIS_NARRATIVE_INTENT_TIMEOUT_S")
    max_t = next(s for s in reg.registered if s.name == "JARVIS_NARRATIVE_INTENT_MAX_TOKENS")
    assert timeout.default == 5.0
    assert max_t.default == 50


# ===========================================================================
# register_shipped_invariants — 4 AST pins
# ===========================================================================


def test_register_shipped_invariants_returns_four():
    invariants = register_shipped_invariants()
    assert len(invariants) == 4
    names = {inv.invariant_name for inv in invariants}
    assert names == {
        "narrative_kind_taxonomy_frozen",
        "narrative_renderer_visual_hierarchy",
        "op_tool_start_synthesizer_wired",
        "op_started_intent_prompt_wired",
    }


def _get_validator(name: str):
    invariants = register_shipped_invariants()
    matches = [inv for inv in invariants if inv.invariant_name == name]
    assert matches
    return matches[0].validate


def _load(rel_path: str):
    repo = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
    src = (repo / rel_path).read_text()
    return ast.parse(src), src


# ===========================================================================
# Each pin passes against today's source
# ===========================================================================


def test_pin_kind_taxonomy_passes_today():
    validator = _get_validator("narrative_kind_taxonomy_frozen")
    tree, src = _load("backend/core/ouroboros/battle_test/narrative_channel.py")
    assert validator(tree, src) == ()


def test_pin_renderer_visual_hierarchy_passes_today():
    validator = _get_validator("narrative_renderer_visual_hierarchy")
    tree, src = _load("backend/core/ouroboros/battle_test/narrative_renderer.py")
    assert validator(tree, src) == ()


def test_pin_op_tool_start_synthesizer_passes_today():
    validator = _get_validator("op_tool_start_synthesizer_wired")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_op_started_intent_prompt_passes_today():
    validator = _get_validator("op_started_intent_prompt_wired")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


# ===========================================================================
# Synthetic-positives: each pin fires on broken source
# ===========================================================================


def test_pin_kind_taxonomy_detects_missing_value():
    validator = _get_validator("narrative_kind_taxonomy_frozen")
    fake = """
class NarrativeKind(str, enum.Enum):
    INTENT = "intent"
    PLAN_PROSE = "plan_prose"
    TOOL_PREAMBLE = "tool_preamble"
    THINKING = "thinking"
    # L2_REPAIR_PROSE removed
    POSTMORTEM_PROSE = "postmortem_prose"
"""
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "L2_REPAIR_PROSE" in violations[0]


def test_pin_renderer_detects_missing_kind_in_table():
    validator = _get_validator("narrative_renderer_visual_hierarchy")
    fake = "# only THINKING reference, no others\nNarrativeKind.THINKING\n"
    violations = validator(ast.parse(fake), fake)
    assert len(violations) >= 5  # missing 5 of 6 kinds


def test_pin_op_tool_start_detects_missing_synthesizer():
    validator = _get_validator("op_tool_start_synthesizer_wired")
    fake = """
def op_tool_start(self, op_id, tool_name, args_summary, round_index, preamble):
    pass  # synthesize_preamble removed
"""
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "synthesize_preamble" in violations[0]


def test_pin_op_started_detects_missing_intent_prompt():
    validator = _get_validator("op_started_intent_prompt_wired")
    fake = """
def op_started(self, op_id, goal, target_files, risk_tier, sensor=""):
    pass  # _maybe_fire_intent_prompt removed
"""
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "_maybe_fire_intent_prompt" in violations[0]


# ===========================================================================
# End-to-end production-boot integration
# ===========================================================================


def test_ensure_seeded_resolves_all_five_flags():
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    reg = ensure_seeded()
    for name in (
        "JARVIS_NARRATIVE_INTENT_ENABLED",
        "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED",
        "JARVIS_NARRATIVE_BUFFER_SIZE",
        "JARVIS_NARRATIVE_INTENT_TIMEOUT_S",
        "JARVIS_NARRATIVE_INTENT_MAX_TOKENS",
    ):
        spec = reg.get_spec(name)
        assert spec is not None, f"missing seeded flag: {name}"


def test_shipped_invariants_pass_live_validation():
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants as sci,
    )
    results = sci.validate_all()
    ours = [
        r for r in results
        if r.invariant_name in {
            "narrative_kind_taxonomy_frozen",
            "narrative_renderer_visual_hierarchy",
            "op_tool_start_synthesizer_wired",
            "op_started_intent_prompt_wired",
        }
    ]
    assert ours == [], (
        f"Gap #6 pin violations: {[r.detail for r in ours]}"
    )
