"""Tests for Gap #7 Slice 5 graduation — master flags default-true,
FlagRegistry self-registration, 5 shipped_code_invariants AST pins.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.presentation_restraint import (
    MASTER_FLAG_ENV_VAR as RESTRAINT_FLAG,
    is_restraint_enabled,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.battle_test.repl_completion import (
    MASTER_FLAG_ENV_VAR as COMPLETION_FLAG,
    is_completion_enabled,
)
from backend.core.ouroboros.battle_test.repl_input_polish import (
    MASTER_FLAG_ENV_VAR as POLISH_FLAG,
    is_polish_enabled,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        RESTRAINT_FLAG,
        COMPLETION_FLAG,
        POLISH_FLAG,
        "JARVIS_TERMINAL_TITLE_ENABLED",
        "JARVIS_REPL_HISTORY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ===========================================================================
# All three master flags default-true post-graduation
# ===========================================================================


def test_restraint_master_default_on():
    assert is_restraint_enabled() is True


def test_completion_master_default_on():
    assert is_completion_enabled() is True


def test_polish_master_default_on():
    assert is_polish_enabled() is True


@pytest.mark.parametrize("flag,checker", [
    (RESTRAINT_FLAG, is_restraint_enabled),
    (COMPLETION_FLAG, is_completion_enabled),
    (POLISH_FLAG, is_polish_enabled),
])
def test_each_flag_explicit_off(monkeypatch, flag, checker):
    monkeypatch.setenv(flag, "false")
    assert checker() is False


# ===========================================================================
# register_flags — module-owned discovery
# ===========================================================================


class _StubRegistry:
    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_seeds_six_specs():
    """Three master flags + three sub-knobs = six FlagSpecs."""
    reg = _StubRegistry()
    count = register_flags(reg)
    assert count == 6
    names = {s.name for s in reg.registered}
    assert names == {
        "JARVIS_PRESENTATION_RESTRAINT_ENABLED",
        "JARVIS_REPL_COMPLETION_ENABLED",
        "JARVIS_REPL_HISTORY_ENABLED",
        "JARVIS_REPL_HISTORY_FILE",
        "JARVIS_REPL_INPUT_POLISH_ENABLED",
        "JARVIS_TERMINAL_TITLE_ENABLED",
    }


def test_master_flags_default_true_in_registry():
    reg = _StubRegistry()
    register_flags(reg)
    for name in (
        "JARVIS_PRESENTATION_RESTRAINT_ENABLED",
        "JARVIS_REPL_COMPLETION_ENABLED",
        "JARVIS_REPL_INPUT_POLISH_ENABLED",
    ):
        spec = next(s for s in reg.registered if s.name == name)
        assert spec.default is True


def test_history_file_spec_default_empty_string():
    """Empty string default → caller resolves to .jarvis/repl_history."""
    reg = _StubRegistry()
    register_flags(reg)
    spec = next(s for s in reg.registered if s.name == "JARVIS_REPL_HISTORY_FILE")
    assert spec.default == ""


# ===========================================================================
# register_shipped_invariants — 5 AST pins
# ===========================================================================


def test_register_shipped_invariants_returns_five():
    invs = register_shipped_invariants()
    assert len(invs) == 5
    names = {i.invariant_name for i in invs}
    assert names == {
        "presentation_restraint_default_true",
        "boot_banner_short_circuits_under_restraint",
        "repl_loop_wires_completion_and_polish",
        "op_lifecycle_sets_terminal_title",
        "status_line_uses_real_stdout_isatty",
    }


def _get_validator(name: str):
    invs = register_shipped_invariants()
    matches = [i for i in invs if i.invariant_name == name]
    assert matches
    return matches[0].validate


def _load(rel: str):
    repo = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
    src = (repo / rel).read_text()
    return ast.parse(src), src


# ===========================================================================
# Each pin passes against today's source
# ===========================================================================


def test_pin_presentation_default_true_passes_today():
    validator = _get_validator("presentation_restraint_default_true")
    tree, src = _load(
        "backend/core/ouroboros/battle_test/presentation_restraint.py"
    )
    assert validator(tree, src) == ()


def test_pin_boot_banner_passes_today():
    validator = _get_validator("boot_banner_short_circuits_under_restraint")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_repl_loop_polish_passes_today():
    validator = _get_validator("repl_loop_wires_completion_and_polish")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_op_lifecycle_title_passes_today():
    validator = _get_validator("op_lifecycle_sets_terminal_title")
    tree, src = _load("backend/core/ouroboros/battle_test/serpent_flow.py")
    assert validator(tree, src) == ()


def test_pin_status_line_real_stdout_passes_today():
    validator = _get_validator("status_line_uses_real_stdout_isatty")
    tree, src = _load("backend/core/ouroboros/battle_test/status_line.py")
    assert validator(tree, src) == ()


# ===========================================================================
# Synthetic-positives: each pin fires on broken source
# ===========================================================================


def test_pin_presentation_default_detects_reverted_default():
    validator = _get_validator("presentation_restraint_default_true")
    fake = '''
def is_restraint_enabled():
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "")  # reverted to default-off
    return raw.strip().lower() in ("1", "true", "yes", "on")
'''
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "default" in violations[0].lower() or "true" in violations[0].lower()


def test_pin_boot_banner_detects_missing_check():
    validator = _get_validator("boot_banner_short_circuits_under_restraint")
    fake = '''
def boot_banner(self, layers, n_sensors=0, log_path=""):
    self.console.print("OUROBOROS + VENOM")  # legacy only — short-circuit removed
'''
    violations = validator(ast.parse(fake), fake)
    assert violations


def test_pin_repl_loop_detects_missing_polish_call():
    validator = _get_validator("repl_loop_wires_completion_and_polish")
    fake = '''
async def _loop(self):
    line = await self._session.prompt_async()
    # extract_attachments removed — Slice 4 regressed
    # build_completion_wiring removed — Slice 3 regressed
'''
    violations = validator(ast.parse(fake), fake)
    assert len(violations) >= 2  # at least 2 missing


def test_pin_op_lifecycle_detects_missing_title_call():
    validator = _get_validator("op_lifecycle_sets_terminal_title")
    fake = '''
def op_started(self, op_id, goal, target_files, risk_tier, sensor=""):
    self._op_starts[op_id] = time.time()
def op_completed(self, op_id, files_changed, provider="", cost_usd=0.0, reasoning=""):
    pass
def op_failed(self, op_id, reason, phase=""):
    pass
'''
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert any("terminal_title" in v for v in violations)


def test_pin_status_line_detects_legacy_check():
    validator = _get_validator("status_line_uses_real_stdout_isatty")
    fake = '''
def should_render():
    if not status_line_enabled():
        return False
    return bool(sys.stdout.isatty())  # legacy — Slice 2 fix reverted
'''
    violations = validator(ast.parse(fake), fake)
    assert violations
    assert "real_stdout_isatty" in violations[0]


# ===========================================================================
# End-to-end production-boot integration
# ===========================================================================


def test_ensure_seeded_resolves_all_six_flags():
    """All Gap #7 flags must be reachable via the production
    seed-boot path."""
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    reg = ensure_seeded()
    for name in (
        "JARVIS_PRESENTATION_RESTRAINT_ENABLED",
        "JARVIS_REPL_COMPLETION_ENABLED",
        "JARVIS_REPL_HISTORY_ENABLED",
        "JARVIS_REPL_HISTORY_FILE",
        "JARVIS_REPL_INPUT_POLISH_ENABLED",
        "JARVIS_TERMINAL_TITLE_ENABLED",
    ):
        spec = reg.get_spec(name)
        assert spec is not None, f"missing seeded flag: {name}"


def test_shipped_invariants_pass_live_validation():
    """All 5 Gap #7 pins pass when validated against today's source."""
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants as sci,
    )
    results = sci.validate_all()
    ours = [
        r for r in results
        if r.invariant_name in {
            "presentation_restraint_default_true",
            "boot_banner_short_circuits_under_restraint",
            "repl_loop_wires_completion_and_polish",
            "op_lifecycle_sets_terminal_title",
            "status_line_uses_real_stdout_isatty",
        }
    ]
    assert ours == [], (
        f"Gap #7 pin violations: {[r.detail for r in ours]}"
    )
