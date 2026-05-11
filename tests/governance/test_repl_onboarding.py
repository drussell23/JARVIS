"""Regression spine for §41 Phase 0 UX Slice 2 — REPL Onboarding."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import repl_onboarding as ro
from backend.core.ouroboros.governance.repl_onboarding import (
    REPL_ONBOARDING_SCHEMA_VERSION,
    ErrorSuggestion,
    HintKind,
    OnboardingStage,
    OnboardingState,
    TutorialStep,
    WelcomeBanner,
    _ENV_AUTO_WELCOME,
    _ENV_BANNER_TEMPLATE,
    _ENV_LEDGER_PATH,
    _ENV_MARKER_PATH,
    _ENV_MASTER,
    _ENV_PERSIST,
    _ENV_TUTORIAL_YAML,
    _ENV_TYPO_MAX_DISTANCE,
    advance_tutorial,
    auto_welcome_enabled,
    build_welcome_banner,
    current_tutorial_step,
    format_onboarding_panel,
    hint_glyph,
    is_first_launch,
    ledger_path,
    load_tutorial_steps,
    mark_onboarded,
    marker_path,
    master_enabled,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    reset_onboarded,
    stage_glyph,
    start_tutorial,
    suggest_for_typo,
    typo_max_distance,
)


# Fake REPL exposing /verb methods for discovery


@dataclass
class _FakeVerbDescriptor:
    slash_name: str = ""
    example: str = ""


@dataclass
class _FakeRegistry:
    descriptors: tuple = ()


class _FakeRepl:
    """A fake repl-like instance with /-prefixed methods so
    repl_completion.discover_verbs picks them up."""

    def _handle_help(self):
        """Show help. example: /help"""
        return ""

    def _handle_posture(self, arg):
        """Override posture. example: /posture HARDEN"""
        return ""

    def _handle_status(self):
        """Show status. example: /status"""
        return ""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_AUTO_WELCOME,
        _ENV_MARKER_PATH, _ENV_TUTORIAL_YAML,
        _ENV_TYPO_MAX_DISTANCE, _ENV_BANNER_TEMPLATE,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_MARKER_PATH, str(tmp_path / "marker"),
    )
    monkeypatch.setenv(
        _ENV_TUTORIAL_YAML, str(tmp_path / "tutorial.yaml"),
    )
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "state.jsonl"),
    )
    yield


def test_schema():
    assert REPL_ONBOARDING_SCHEMA_VERSION == "repl_onboarding.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_auto_welcome_default_true():
    assert auto_welcome_enabled() is True


def test_typo_distance_default():
    assert typo_max_distance() == 2


def test_typo_distance_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_TYPO_MAX_DISTANCE, "100")
    assert typo_max_distance() == 10


def test_marker_path_env(monkeypatch, tmp_path):
    target = tmp_path / "custom"
    monkeypatch.setenv(_ENV_MARKER_PATH, str(target))
    assert marker_path() == target


def test_stage_taxonomy_closed():
    assert {s.value for s in OnboardingStage} == {
        "new_user", "in_tutorial", "graduated", "disabled",
    }


def test_hint_taxonomy_closed():
    assert {h.value for h in HintKind} == {
        "verb_typo", "missing_arg", "out_of_scope", "none",
    }


@pytest.mark.parametrize("s", list(OnboardingStage))
def test_stage_glyph(s):
    assert stage_glyph(s) != "?"


@pytest.mark.parametrize("h", list(HintKind))
def test_hint_glyph(h):
    assert hint_glyph(h) != "?"


# First launch detection


def test_first_launch_master_off():
    """Master off → always returns False (no first-launch
    nag when substrate is gated)."""
    assert is_first_launch() is False


def test_first_launch_master_on_no_marker(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert is_first_launch() is True


def test_first_launch_master_on_with_marker(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert is_first_launch() is False


def test_mark_onboarded_master_off_no_write():
    assert mark_onboarded() is False
    assert not marker_path().exists()


def test_mark_onboarded_master_on_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert mark_onboarded(op_id="op-1") is True
    assert marker_path().exists()


def test_mark_onboarded_idempotent(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_onboarded()
    first_content = marker_path().read_text(encoding="utf-8")
    mark_onboarded()
    second_content = marker_path().read_text(encoding="utf-8")
    assert first_content == second_content


def test_reset_onboarded_removes_marker(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_onboarded()
    assert marker_path().exists()
    assert reset_onboarded() is True
    assert not marker_path().exists()


# Welcome banner


def test_welcome_banner_first_launch(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    banner = build_welcome_banner()
    assert isinstance(banner, WelcomeBanner)
    assert banner.is_first_launch is True
    assert "first launch" in banner.title.lower()


def test_welcome_banner_after_onboarded(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_onboarded()
    banner = build_welcome_banner()
    assert banner.is_first_launch is False


def test_welcome_banner_with_verbs(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    banner = build_welcome_banner(repl_instance=_FakeRepl())
    assert banner.verb_count >= 1


def test_welcome_banner_with_template(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    template = tmp_path / "banner.txt"
    template.write_text(
        "Custom banner line 1\nCustom banner line 2",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_BANNER_TEMPLATE, str(template))
    banner = build_welcome_banner()
    assert any("Custom banner" in line for line in banner.body_lines)


def test_welcome_banner_render():
    banner = WelcomeBanner(
        title="t", body_lines=("line1", "line2"),
        is_first_launch=True, cwd="/x",
        flag_count=5, verb_count=3,
        rendered_at_unix=1.0,
    )
    out = banner.render()
    assert "t" in out
    assert "line1" in out
    assert "line2" in out


# Tutorial — yaml loader


def test_load_tutorial_missing_yaml_returns_empty():
    assert load_tutorial_steps() == ()


def test_load_tutorial_json_format(monkeypatch, tmp_path):
    target = tmp_path / "tutorial.json"
    target.write_text(
        json.dumps({
            "steps": [
                {
                    "id": "first",
                    "prompt": "Type /help",
                    "action": "/help",
                    "marker": "Available verbs",
                },
                {
                    "id": "second",
                    "prompt": "Try /posture",
                    "action": "/posture",
                    "marker": "Current posture",
                },
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_TUTORIAL_YAML, str(target))
    steps = load_tutorial_steps()
    assert len(steps) == 2
    assert steps[0].step_id == "first"
    assert steps[1].step_id == "second"


def test_load_tutorial_malformed_yaml(monkeypatch, tmp_path):
    target = tmp_path / "tutorial.yaml"
    target.write_text("not a valid yaml: {{{", encoding="utf-8")
    monkeypatch.setenv(_ENV_TUTORIAL_YAML, str(target))
    assert load_tutorial_steps() == ()


def test_load_tutorial_skips_invalid_entries(monkeypatch, tmp_path):
    target = tmp_path / "tutorial.json"
    target.write_text(
        json.dumps({
            "steps": [
                {"id": "ok", "prompt": "p"},
                {"id": "", "prompt": "no id"},
                {"prompt": "no id"},
                "not a dict",
                {"id": "ok2", "prompt": "p2"},
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_TUTORIAL_YAML, str(target))
    steps = load_tutorial_steps()
    ids = {s.step_id for s in steps}
    assert ids == {"ok", "ok2"}


# Tutorial state machine


def test_current_step_master_off():
    assert current_tutorial_step() is None


def test_current_step_no_tutorial(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert current_tutorial_step() is None


def test_current_step_with_steps(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="/a", completion_marker="",
        ),
        TutorialStep(
            step_id="b", prompt_text="p",
            action_required="/b", completion_marker="",
        ),
    )
    step = current_tutorial_step(steps=steps)
    assert step is not None
    assert step.step_id == "a"


def test_current_step_skips_completed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="", completion_marker="",
        ),
        TutorialStep(
            step_id="b", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    step = current_tutorial_step(
        completed_ids=("a",), steps=steps,
    )
    assert step.step_id == "b"


def test_start_tutorial_master_off():
    state = start_tutorial()
    assert state.stage is OnboardingStage.DISABLED


def test_start_tutorial_no_steps_graduates(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    state = start_tutorial(steps=())
    assert state.stage is OnboardingStage.GRADUATED


def test_start_tutorial_with_steps(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    state = start_tutorial(steps=steps)
    assert state.stage is OnboardingStage.IN_TUTORIAL
    assert state.current_step_id == "a"


def test_advance_tutorial_progresses(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="", completion_marker="",
        ),
        TutorialStep(
            step_id="b", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    state = start_tutorial(steps=steps)
    new_state = advance_tutorial(state, steps=steps)
    assert new_state.stage is OnboardingStage.IN_TUTORIAL
    assert new_state.current_step_id == "b"
    assert "a" in new_state.completed_step_ids


def test_advance_tutorial_graduates_at_end(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    steps = (
        TutorialStep(
            step_id="only", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    state = start_tutorial(steps=steps)
    new_state = advance_tutorial(state, steps=steps)
    assert new_state.stage is OnboardingStage.GRADUATED


# Did-you-mean


def test_suggest_master_off():
    out = suggest_for_typo("/help")
    assert out.hint_kind is HintKind.NONE
    assert _ENV_MASTER in out.diagnostic


def test_suggest_empty_input(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = suggest_for_typo("")
    assert out.hint_kind is HintKind.NONE


def test_suggest_plain_text_is_out_of_scope(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = suggest_for_typo("just a question for the AI")
    assert out.hint_kind is HintKind.OUT_OF_SCOPE
    assert "/help" in out.suggestions


def test_suggest_unknown_verb_no_close_match(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = suggest_for_typo(
        "/zzzwxxq",
        verbs_override=("/help", "/posture", "/status"),
    )
    assert out.hint_kind is HintKind.OUT_OF_SCOPE


def test_suggest_unknown_verb_close_match(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = suggest_for_typo(
        "/helo",  # typo of /help
        verbs_override=("/help", "/posture", "/status"),
    )
    assert out.hint_kind is HintKind.VERB_TYPO
    assert "/help" in out.suggestions


def test_suggest_exact_match_no_args_required(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = suggest_for_typo(
        "/help",
        verbs_override=("/help",),
    )
    assert out.hint_kind is HintKind.NONE
    assert "recognized" in out.diagnostic


def test_suggest_renders_with_example():
    s = ErrorSuggestion(
        input_text="/posure",
        hint_kind=HintKind.VERB_TYPO,
        suggestions=("/posture",),
        example_command="/posture HARDEN",
        diagnostic="unknown verb /posure",
    )
    out = s.render()
    assert "Did you mean: /posture?" in out
    assert "Example: /posture HARDEN" in out


def test_suggest_none_renders_diagnostic_only():
    s = ErrorSuggestion(
        input_text="ok",
        hint_kind=HintKind.NONE,
        suggestions=(),
        example_command="",
        diagnostic="all good",
    )
    out = s.render()
    assert out == "all good"


# Renderer


def test_format_panel_master_off():
    out = format_onboarding_panel()
    assert "disabled" in out


def test_format_panel_with_banner(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    banner = build_welcome_banner()
    out = format_onboarding_panel(banner=banner)
    assert banner.title in out


def test_format_panel_with_state(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    state = OnboardingState(
        stage=OnboardingStage.IN_TUTORIAL,
        current_step_id="step-1",
        completed_step_ids=(),
        started_at_unix=1.0,
    )
    out = format_onboarding_panel(state=state)
    assert "in_tutorial" in out


# to_dict


def test_banner_to_dict():
    b = WelcomeBanner(
        title="t", body_lines=("l",),
        is_first_launch=True, cwd="/x",
        flag_count=1, verb_count=1, rendered_at_unix=1.0,
    )
    d = b.to_dict()
    assert d["schema_version"] == REPL_ONBOARDING_SCHEMA_VERSION


def test_step_to_dict():
    s = TutorialStep(
        step_id="a", prompt_text="p",
        action_required="x", completion_marker="m",
    )
    d = s.to_dict()
    assert d["schema_version"] == REPL_ONBOARDING_SCHEMA_VERSION


def test_state_to_dict():
    s = OnboardingState(
        stage=OnboardingStage.GRADUATED,
        current_step_id="",
        completed_step_ids=("a", "b"),
        started_at_unix=1.0,
    )
    d = s.to_dict()
    assert d["kind"] == "onboarding_state"
    assert d["stage"] == "graduated"


# Persistence


def test_persist_advance_tutorial_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="", completion_marker="",
        ),
        TutorialStep(
            step_id="b", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    state = start_tutorial(steps=steps)
    advance_tutorial(state, steps=steps)
    assert ledger_path().exists()


def test_persist_disabled_no_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    steps = (
        TutorialStep(
            step_id="a", prompt_text="p",
            action_required="", completion_marker="",
        ),
    )
    state = start_tutorial(steps=steps)
    advance_tutorial(state, steps=steps)
    assert not ledger_path().exists()


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/repl_onboarding.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "stage_taxonomy_closed",
        "hint_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_stage_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "stage_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class OnboardingStage(str, enum.Enum):\n"
        "    NEW_USER = 'new_user'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_hint_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "hint_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class HintKind(str, enum.Enum):\n"
        "    VERB_TYPO = 'verb_typo'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.battle_test.serpent_flow "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 7


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False
