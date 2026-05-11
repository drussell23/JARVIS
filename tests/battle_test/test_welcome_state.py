"""Regression spine for §41.3 Slice 2 welcome_state + tutorial substrate."""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc
from backend.core.ouroboros.battle_test import welcome_state as ws
from backend.core.ouroboros.battle_test.welcome_state import (
    WELCOME_STATE_SCHEMA_VERSION,
    WelcomePhase,
    WelcomeState,
    _ENV_FORCE_SHOW,
    _ENV_MASTER,
    _ENV_SENTINEL_PATH,
    evaluate,
    force_show,
    master_enabled,
    mark_seen,
    phase_glyph,
    register_flags,
    register_shipped_invariants,
    render_first_launch_banner,
    render_tutorial,
    sentinel_path,
)


# --- Schema + taxonomy -----------------------------------------------------


def test_schema_version_stamp():
    assert WELCOME_STATE_SCHEMA_VERSION == "welcome_state.1"


def test_welcome_phase_closed():
    assert {v.value for v in WelcomePhase} == {
        "first_launch", "returning", "forced", "disabled",
    }


# --- Env knobs -------------------------------------------------------------


def test_master_default_true(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is True


def test_master_disable(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    assert master_enabled() is False


def test_force_show_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_FORCE_SHOW, raising=False)
    assert force_show() is False


def test_force_show_enable(monkeypatch):
    monkeypatch.setenv(_ENV_FORCE_SHOW, "true")
    assert force_show() is True


def test_sentinel_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_SENTINEL_PATH, raising=False)
    p = sentinel_path()
    assert ".jarvis" in str(p)
    assert "welcome_seen.flag" in str(p)


def test_sentinel_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.flag"
    monkeypatch.setenv(_ENV_SENTINEL_PATH, str(custom))
    assert sentinel_path() == custom


# --- Glyphs ----------------------------------------------------------------


def test_phase_glyph_enum():
    assert phase_glyph(WelcomePhase.FIRST_LAUNCH) == "🌱"
    assert phase_glyph(WelcomePhase.RETURNING) == "↻"
    assert phase_glyph(WelcomePhase.FORCED) == "🔁"
    assert phase_glyph(WelcomePhase.DISABLED) == "◌"


def test_phase_glyph_string():
    assert phase_glyph("first_launch") == "🌱"


def test_phase_glyph_unknown():
    assert phase_glyph("bogus") == "?"


def test_phase_glyph_none():
    assert phase_glyph(None) == "?"


# --- evaluate --------------------------------------------------------------


def test_evaluate_first_launch(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.delenv(_ENV_FORCE_SHOW, raising=False)
    sentinel = tmp_path / "x.flag"
    state = evaluate(path_override=sentinel)
    assert state.phase is WelcomePhase.FIRST_LAUNCH
    assert state.should_show_expanded_banner() is True
    assert state.sentinel_existed is False


def test_evaluate_returning(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.delenv(_ENV_FORCE_SHOW, raising=False)
    sentinel = tmp_path / "x.flag"
    sentinel.touch()
    state = evaluate(path_override=sentinel)
    assert state.phase is WelcomePhase.RETURNING
    assert state.should_show_expanded_banner() is False
    assert state.sentinel_existed is True


def test_evaluate_forced_overrides_returning(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FORCE_SHOW, "true")
    sentinel = tmp_path / "x.flag"
    sentinel.touch()
    state = evaluate(path_override=sentinel)
    assert state.phase is WelcomePhase.FORCED
    assert state.should_show_expanded_banner() is True


def test_evaluate_master_off_returns_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "false")
    sentinel = tmp_path / "x.flag"
    state = evaluate(path_override=sentinel)
    assert state.phase is WelcomePhase.DISABLED
    assert state.should_show_expanded_banner() is False


def test_evaluate_records_timestamp():
    state = evaluate(now_unix=1234.5, path_override=Path("/nonexistent"))
    assert state.evaluated_at_unix == 1234.5


# --- mark_seen -------------------------------------------------------------


def test_mark_seen_creates_sentinel(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    sentinel = tmp_path / "subdir" / "x.flag"
    assert not sentinel.exists()
    ok = mark_seen(path_override=sentinel)
    assert ok is True
    assert sentinel.exists()


def test_mark_seen_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    sentinel = tmp_path / "x.flag"
    sentinel.touch()
    ok = mark_seen(path_override=sentinel)
    assert ok is True
    assert sentinel.exists()


def test_mark_seen_master_off(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "false")
    sentinel = tmp_path / "x.flag"
    ok = mark_seen(path_override=sentinel)
    assert ok is False
    assert not sentinel.exists()


def test_mark_seen_readonly_returns_false_no_raise(monkeypatch, tmp_path):
    """When the parent dir can't be created (e.g., file in
    its path), mark_seen returns False without raising."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    blocker = tmp_path / "blocker"
    blocker.touch()  # regular file blocking subdir creation
    sentinel = blocker / "x.flag"  # would need blocker to be a dir
    ok = mark_seen(path_override=sentinel)
    assert ok is False


# --- WelcomeState dataclass ------------------------------------------------


def test_welcome_state_to_dict():
    state = WelcomeState(
        phase=WelcomePhase.FIRST_LAUNCH,
        sentinel_path="/tmp/x",
        sentinel_existed=False,
        evaluated_at_unix=1.0,
        master_enabled=True,
    )
    d = state.to_dict()
    assert d["phase"] == "first_launch"
    assert d["sentinel_existed"] is False
    assert d["schema_version"] == "welcome_state.1"


def test_should_show_expanded_banner_matrix():
    matrix = {
        WelcomePhase.FIRST_LAUNCH: True,
        WelcomePhase.FORCED: True,
        WelcomePhase.RETURNING: False,
        WelcomePhase.DISABLED: False,
    }
    for phase, expected in matrix.items():
        s = WelcomeState(
            phase=phase, sentinel_path="x",
            sentinel_existed=False,
            evaluated_at_unix=0.0,
            master_enabled=phase is not WelcomePhase.DISABLED,
        )
        assert s.should_show_expanded_banner() is expected


# --- Banner rendering ------------------------------------------------------


def test_banner_renders_without_registry():
    text = render_first_launch_banner(None)
    assert "First launch" in text
    assert "/tutorial" in text


def test_banner_renders_with_registry():
    class _R:
        def _handle_cancel(self):
            """Cancel an op.

            @category: lifecycle
            """
    reg = rc.discover_verbs(_R())
    text = render_first_launch_banner(reg)
    assert "First launch" in text
    assert "verbs total" in text


def test_banner_uses_registry_description_when_present():
    class _R:
        def _handle_tutorial(self):
            """My custom tutorial description here."""
    reg = rc.discover_verbs(_R())
    text = render_first_launch_banner(reg)
    assert "My custom tutorial description" in text


def test_banner_never_raises_on_garbage_registry():
    class _Bogus:
        def find(self, x):
            raise RuntimeError("boom")
        def __len__(self):
            raise RuntimeError("boom")
    text = render_first_launch_banner(_Bogus())
    # Defensive — at minimum the header line.
    assert "First launch" in text


# --- Tutorial rendering ----------------------------------------------------


def _build_registry():
    class _R:
        def _handle_cancel(self, op_id):
            """Cancel a pending op.

            @arg_spec: <op_id>
            @example: /cancel op-abc
            @category: lifecycle
            """
        def _handle_status(self):
            """Show status snapshot.

            @category: introspection
            """
        def _handle_expand(self, ref):
            """Expand a stored artifact.

            @arg_spec: <ref>
            @category: navigation
            """
    return rc.discover_verbs(_R())


def test_tutorial_renders_all_categories():
    reg = _build_registry()
    text = render_tutorial(reg)
    assert "Operator Tutorial" in text
    assert "LIFECYCLE" in text
    assert "INTROSPECTION" in text
    assert "NAVIGATION" in text


def test_tutorial_category_filter():
    reg = _build_registry()
    text = render_tutorial(reg, category_filter="lifecycle")
    assert "LIFECYCLE" in text
    assert "INTROSPECTION" not in text


def test_tutorial_unknown_filter_returns_empty_message():
    reg = _build_registry()
    text = render_tutorial(reg, category_filter="bogus")
    assert "no verbs in category" in text


def test_tutorial_none_registry():
    text = render_tutorial(None)
    assert "no verb registry" in text


def test_tutorial_garbage_registry_safe():
    class _Bogus:
        def categories(self):
            raise RuntimeError("boom")
    text = render_tutorial(_Bogus())
    assert "no categories" in text


def test_tutorial_includes_examples():
    reg = _build_registry()
    text = render_tutorial(reg)
    assert "/cancel op-abc" in text  # example from @example tag


def test_tutorial_includes_arg_spec():
    reg = _build_registry()
    text = render_tutorial(reg)
    assert "<op_id>" in text  # from @arg_spec tag


# --- FlagRegistry seeds ----------------------------------------------------


def test_register_flags_count():
    class _Reg:
        def __init__(self):
            self.specs = []

        def register(self, spec):
            self.specs.append(spec)

    r = _Reg()
    n = register_flags(r)
    assert n == 3
    names = [s.name for s in r.specs]
    assert _ENV_MASTER in names
    assert _ENV_FORCE_SHOW in names
    assert _ENV_SENTINEL_PATH in names


def test_register_flags_master_default_true():
    class _Reg:
        def __init__(self):
            self.specs = []

        def register(self, spec):
            self.specs.append(spec)

    r = _Reg()
    register_flags(r)
    master = next(s for s in r.specs if s.name == _ENV_MASTER)
    assert master.default is True


# --- AST pins --------------------------------------------------------------


def _load_source_tree():
    target = Path(
        "backend/core/ouroboros/battle_test/welcome_state.py"
    )
    src = target.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    assert len(register_shipped_invariants()) == 4


def test_ast_pin_phase_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "phase_taxonomy" in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_ast_pin_authority_asymmetry_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "authority_asymmetry" in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_ast_pin_master_default_true_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "master_default_true" in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "composes_canonical" in p.invariant_name)
    assert pin.validate(tree, src) == ()


# --- AST pin synthetic regressions -----------------------------------------


def test_ast_pin_phase_catches_drift():
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "phase_taxonomy" in p.invariant_name)
    bad = '''
class WelcomePhase(str, enum.Enum):
    FIRST_LAUNCH = "first_launch"
    RETURNING = "returning"
    FORCED = "forced"
    UNKNOWN = "unknown"
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_authority_catches_orchestrator():
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "authority_asymmetry" in p.invariant_name)
    bad = '''
from backend.core.ouroboros.governance.orchestrator import x
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_master_default_true_catches_false():
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "master_default_true" in p.invariant_name)
    bad = '''
def master_enabled():
    return _flag("X", default=False)
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_composes_catches_missing_repl_completion():
    pins = register_shipped_invariants()
    pin = next(p for p in pins if "composes_canonical" in p.invariant_name)
    bad = '''
# format_verb_help
import pathlib
'''
    assert pin.validate(ast.parse(bad), bad) != ()
