"""Tests for Gap #2 Slice 5 graduation — master flag default-true,
FlagRegistry self-registration, and the 3 shipped_code_invariants
AST pins.

These tests live alongside the substrate (battle_test/) so the
graduation contract is co-located with the code it pins.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.tool_render_view import (
    MASTER_FLAG_ENV_VAR,
    is_master_flag_enabled,
    register_flags,
    register_shipped_invariants,
)


# ===========================================================================
# Master-flag default — graduated to TRUE on 2026-05-04
# ===========================================================================


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    yield


def test_master_flag_default_is_true_post_graduation():
    assert is_master_flag_enabled() is True


def test_master_flag_explicit_off_disables(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert is_master_flag_enabled() is False


# ===========================================================================
# register_flags() — module-owned FlagSpec discovery
# ===========================================================================


class _StubRegistry:
    """Captures registered FlagSpecs without depending on the real
    FlagRegistry singleton (avoids cross-test pollution)."""

    def __init__(self):
        self.registered = []
        self.fail_for: set = set()

    def register(self, spec):
        if spec.name in self.fail_for:
            raise RuntimeError(f"simulated rejection of {spec.name}")
        self.registered.append(spec)


def test_register_flags_seeds_three_specs():
    reg = _StubRegistry()
    count = register_flags(reg)
    assert count == 3
    names = {spec.name for spec in reg.registered}
    assert names == {
        "JARVIS_TOOL_RENDER_REGISTRY_ENABLED",
        "JARVIS_TOOL_RENDER_DENSITY",
        "JARVIS_TOOL_RENDER_STORE_SIZE",
    }


def test_register_flags_master_default_is_true():
    reg = _StubRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered
        if s.name == "JARVIS_TOOL_RENDER_REGISTRY_ENABLED"
    )
    assert master.default is True


def test_register_flags_density_and_store_are_tuning():
    reg = _StubRegistry()
    register_flags(reg)
    density = next(
        s for s in reg.registered if s.name == "JARVIS_TOOL_RENDER_DENSITY"
    )
    store = next(
        s for s in reg.registered if s.name == "JARVIS_TOOL_RENDER_STORE_SIZE"
    )
    # Master is SAFETY; sub-knobs are TUNING.
    assert density.category.value.lower() == "tuning"
    assert store.category.value.lower() == "tuning"
    assert store.default == 50


def test_register_flags_swallows_registration_failure():
    """Defensive contract: registry rejection of one flag must not
    abort registration of the remaining flags."""
    reg = _StubRegistry()
    reg.fail_for = {"JARVIS_TOOL_RENDER_DENSITY"}
    count = register_flags(reg)
    assert count == 2  # density rejected; master + store registered
    names = {spec.name for spec in reg.registered}
    assert "JARVIS_TOOL_RENDER_DENSITY" not in names


# ===========================================================================
# register_shipped_invariants() — 3 AST pins
# ===========================================================================


def test_register_shipped_invariants_returns_three():
    invariants = register_shipped_invariants()
    assert len(invariants) == 3
    names = {inv.invariant_name for inv in invariants}
    assert names == {
        "tool_render_view_public_surface",
        "tool_render_registry_descriptor_completeness",
        "tool_render_policy_di_cage",
    }


def test_register_shipped_invariants_target_files_resolve():
    """Every pin's target_file must be a real path in the repo."""
    repo_root = Path(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"
    )
    for inv in register_shipped_invariants():
        full = repo_root / inv.target_file
        assert full.is_file(), f"{inv.invariant_name} → {full} not found"


# ===========================================================================
# Pin 1 — tool_render_view public surface
# ===========================================================================


def _load_ast(rel_path: str) -> tuple:
    """Helper: load (tree, source) for a repo-relative path."""
    repo_root = Path(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent"
    )
    src = (repo_root / rel_path).read_text()
    return ast.parse(src), src


def _get_validator(name: str):
    invariants = register_shipped_invariants()
    matches = [inv for inv in invariants if inv.invariant_name == name]
    assert matches, f"no invariant named {name!r}"
    return matches[0].validate


def test_pin_view_public_surface_passes_today():
    validator = _get_validator("tool_render_view_public_surface")
    tree, src = _load_ast(
        "backend/core/ouroboros/battle_test/tool_render_view.py"
    )
    violations = validator(tree, src)
    assert violations == ()


def test_pin_view_public_surface_detects_missing_function():
    """Synthesize a tree missing one of the required exports —
    validator should catch it."""
    validator = _get_validator("tool_render_view_public_surface")
    fake_src = """
def compose():
    pass

def is_master_flag_enabled():
    pass
# compose_if_enabled deliberately removed
"""
    tree = ast.parse(fake_src)
    violations = validator(tree, fake_src)
    assert violations
    assert "compose_if_enabled" in violations[0]


# ===========================================================================
# Pin 2 — descriptor completeness
# ===========================================================================


def test_pin_descriptor_completeness_passes_today():
    validator = _get_validator(
        "tool_render_registry_descriptor_completeness",
    )
    tree, src = _load_ast(
        "backend/core/ouroboros/battle_test/tool_render_registry.py"
    )
    violations = validator(tree, src)
    assert violations == ()


def test_pin_descriptor_completeness_detects_missing_tool():
    """Synthesize a partial _DESCRIPTORS dict — validator catches it."""
    validator = _get_validator(
        "tool_render_registry_descriptor_completeness",
    )
    fake_src = """
_DESCRIPTORS = {
    "read_file": "x",
    "edit_file": "x",
    # all 16 others missing
}
"""
    tree = ast.parse(fake_src)
    violations = validator(tree, fake_src)
    assert violations
    # The error message should list the missing tools.
    assert "missing entries" in violations[0]
    assert "bash" in violations[0]


# ===========================================================================
# Pin 3 — DI cage on tool_render_policy
# ===========================================================================


def test_pin_di_cage_passes_today():
    validator = _get_validator("tool_render_policy_di_cage")
    tree, src = _load_ast(
        "backend/core/ouroboros/battle_test/tool_render_policy.py"
    )
    violations = validator(tree, src)
    assert violations == ()


def test_pin_di_cage_detects_top_level_import():
    """Synthesize a top-level forbidden import — validator catches it."""
    validator = _get_validator("tool_render_policy_di_cage")
    fake_src = (
        "from backend.core.ouroboros.governance.posture_observer "
        "import get_default_store\n"
        "\n"
        "def f():\n"
        "    pass\n"
    )
    tree = ast.parse(fake_src)
    violations = validator(tree, fake_src)
    assert violations
    assert "posture_observer" in violations[0]


def test_pin_di_cage_allows_lazy_imports_in_function_bodies():
    """The whole point of the DI cage is to allow lazy imports
    inside ``Default*Provider.current()`` methods. Top-level
    inspection only — function-body imports must NOT trigger."""
    validator = _get_validator("tool_render_policy_di_cage")
    fake_src = (
        "def current(self):\n"
        "    from backend.core.ouroboros.governance.posture_observer "
        "import get_default_store\n"
        "    return None\n"
    )
    tree = ast.parse(fake_src)
    violations = validator(tree, fake_src)
    assert violations == ()


# ===========================================================================
# Discovery integration — battle_test entry in _INVARIANT_PROVIDER_PACKAGES
# ===========================================================================


def test_discovery_finds_view_register_function():
    """Smoke check that the standard battle_test discovery loop
    can introspect tool_render_view's registration hooks."""
    from importlib import import_module
    mod = import_module(
        "backend.core.ouroboros.battle_test.tool_render_view"
    )
    assert callable(getattr(mod, "register_flags", None))
    assert callable(getattr(mod, "register_shipped_invariants", None))


def test_end_to_end_ensure_seeded_resolves_our_flags():
    """Production boot path verification: a fresh ensure_seeded()
    call must result in all 3 of our FlagSpecs being reachable via
    ``registry.get_spec(name)`` — proves the
    ``register_flags`` hook is wired into the standard seed boot,
    not just discoverable as a callable."""
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    reg = ensure_seeded()
    master = reg.get_spec("JARVIS_TOOL_RENDER_REGISTRY_ENABLED")
    density = reg.get_spec("JARVIS_TOOL_RENDER_DENSITY")
    store = reg.get_spec("JARVIS_TOOL_RENDER_STORE_SIZE")
    assert master is not None and master.default is True
    assert density is not None
    assert store is not None and store.default == 50


def test_end_to_end_shipped_invariants_pass_live_validation():
    """Production AST-pin verification: validate_all() over the
    real shipped registry must return zero violations for our 3
    pins right now — proves the substrate's actual source matches
    the structural contract."""
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants as sci,
    )
    results = sci.validate_all()
    ours = [
        r for r in results
        if r.invariant_name.startswith("tool_render_")
    ]
    assert ours == [], (
        f"shipped_code_invariants violations: {[r.detail for r in ours]}"
    )
