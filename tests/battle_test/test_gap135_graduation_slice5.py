"""Tests for Gap #1+3+5 Slice 5 graduation — master flags default-true,
FlagRegistry self-registration, 4 shipped_code_invariants AST pins.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.live_status_line import (
    MASTER_FLAG_ENV_VAR,
    is_master_flag_enabled,
    register_flags,
    register_shipped_invariants,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv("JARVIS_OP_COLLAPSE_ENABLED", raising=False)
    yield


# ===========================================================================
# Master flags default — graduated to TRUE on 2026-05-04
# ===========================================================================


def test_status_line_master_flag_default_on():
    assert is_master_flag_enabled() is True


def test_status_line_master_flag_explicit_off(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert is_master_flag_enabled() is False


# ===========================================================================
# register_flags — module-owned discovery
# ===========================================================================


class _StubRegistry:
    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_seeds_three_specs():
    reg = _StubRegistry()
    count = register_flags(reg)
    assert count == 3
    names = {spec.name for spec in reg.registered}
    assert names == {
        "JARVIS_LIVE_STATUS_LINE_ENABLED",
        "JARVIS_OP_COLLAPSE_ENABLED",
        "JARVIS_OP_BLOCK_BUFFER_SIZE",
    }


def test_master_flags_default_true_in_registry():
    reg = _StubRegistry()
    register_flags(reg)
    for name in ("JARVIS_LIVE_STATUS_LINE_ENABLED", "JARVIS_OP_COLLAPSE_ENABLED"):
        spec = next(s for s in reg.registered if s.name == name)
        assert spec.default is True


def test_buffer_size_default_50():
    reg = _StubRegistry()
    register_flags(reg)
    spec = next(s for s in reg.registered if s.name == "JARVIS_OP_BLOCK_BUFFER_SIZE")
    assert spec.default == 50


# ===========================================================================
# register_shipped_invariants — 4 AST pins
# ===========================================================================


def test_register_shipped_invariants_returns_four():
    invariants = register_shipped_invariants()
    assert len(invariants) == 4
    names = {inv.invariant_name for inv in invariants}
    assert names == {
        "status_line_callable_wired_into_prompt_async",
        "op_block_state_taxonomy_frozen",
        "serpent_flow_op_lifecycle_buffer_hooks",
        "handle_expand_dispatches_three_prefixes",
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


def test_pin_status_line_wired_passes_today():
    validator = _get_validator("status_line_callable_wired_into_prompt_async")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_op_block_state_passes_today():
    validator = _get_validator("op_block_state_taxonomy_frozen")
    tree, src = _load("backend/core/ouroboros/battle_test/op_block_buffer.py")
    assert validator(tree, src) == ()


def test_pin_lifecycle_hooks_passes_today():
    validator = _get_validator("serpent_flow_op_lifecycle_buffer_hooks")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_handle_expand_passes_today():
    validator = _get_validator("handle_expand_dispatches_three_prefixes")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


# ===========================================================================
# Synthetic-positive: each pin fires on a deliberately broken source
# ===========================================================================


def test_pin_status_line_wired_detects_missing_call():
    validator = _get_validator("status_line_callable_wired_into_prompt_async")
    fake = "# no make_bottom_toolbar_callable here\n"
    violations = validator(ast.parse(fake), fake)
    assert violations
    # The validator may report either the missing function-name OR the
    # missing kwarg wiring; both are valid regression signals.
    combined = " ".join(violations).lower()
    assert (
        "make_bottom_toolbar_callable" in combined
        or "bottom_toolbar" in combined
    )


def test_pin_op_block_state_detects_missing_value():
    validator = _get_validator("op_block_state_taxonomy_frozen")
    fake = """
class OpBlockState(str, enum.Enum):
    BUFFERING = "buffering"
    # COMMITTED removed
    EXPANDED = "expanded"
"""
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "COMMITTED" in violations[0]


def test_pin_lifecycle_hooks_detects_missing_call():
    validator = _get_validator("serpent_flow_op_lifecycle_buffer_hooks")
    fake = """
class SerpentFlow:
    def op_started(self, op_id, goal, target_files, risk_tier, sensor=""):
        pass  # _maybe_buffer_op_start removed
    def _op_line(self, op_id, text):
        self._maybe_buffer_op_line(op_id, text)
    def op_completed(self, op_id, files_changed, provider="", cost_usd=0.0, reasoning=""):
        self._maybe_buffer_op_commit(op_id, "summary")
    def op_failed(self, op_id, reason, phase=""):
        self._maybe_buffer_op_commit(op_id, "summary")
"""
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "op_started" in violations[0]


def test_pin_handle_expand_detects_missing_prefix():
    validator = _get_validator("handle_expand_dispatches_three_prefixes")
    fake = """
class SerpentREPL:
    def _handle_expand(self, line):
        if line.startswith("t-"):
            pass
        # d- and o- branches removed
"""
    violations = validator(ast.parse(fake), fake)
    assert len(violations) >= 2  # missing d- and o-


# ===========================================================================
# End-to-end production-boot integration
# ===========================================================================


def test_ensure_seeded_resolves_all_three_flags():
    """Flags must be reachable via the production seed-boot path."""
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    reg = ensure_seeded()
    for name in (
        "JARVIS_LIVE_STATUS_LINE_ENABLED",
        "JARVIS_OP_COLLAPSE_ENABLED",
        "JARVIS_OP_BLOCK_BUFFER_SIZE",
    ):
        spec = reg.get_spec(name)
        assert spec is not None, f"missing seeded flag: {name}"


def test_shipped_invariants_pass_live_validation():
    """All 4 Gap #1+3+5 pins pass when validated against today's source."""
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants as sci,
    )
    results = sci.validate_all()
    ours = [
        r for r in results
        if r.invariant_name in {
            "status_line_callable_wired_into_prompt_async",
            "op_block_state_taxonomy_frozen",
            "serpent_flow_op_lifecycle_buffer_hooks",
            "handle_expand_dispatches_three_prefixes",
        }
    ]
    assert ours == [], (
        f"Gap #1+3+5 pin violations: {[r.detail for r in ours]}"
    )
