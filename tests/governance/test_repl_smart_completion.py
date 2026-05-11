"""Regression spine for §41 Phase 0 UX Slice 3 — REPL Smart Completion."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    repl_smart_completion as rsc,
)
from backend.core.ouroboros.governance.repl_smart_completion import (
    REPL_SMART_COMPLETION_SCHEMA_VERSION,
    CompletionKind,
    CompletionMatch,
    FastPathClassification,
    FormattedOutput,
    OutputFormat,
    PaletteResult,
    VerbHelp,
    _ENV_FAST_PATH_ENABLED,
    _ENV_FAST_PATH_MAX_LEN,
    _ENV_JSON_INDENT,
    _ENV_MASTER,
    _ENV_OUTPUT_BOUND,
    _ENV_PALETTE_DISTANCE,
    _ENV_PALETTE_MAX_RESULTS,
    _ENV_PRETTY_JSON_ENABLED,
    _extract_example_from_description,
    build_verb_help,
    classify_fast_path,
    fast_path_enabled,
    fast_path_max_len,
    format_completion_panel,
    format_glyph,
    is_json_shaped,
    json_indent,
    kind_glyph,
    master_enabled,
    output_bound,
    palette_distance_threshold,
    palette_max_results,
    pretty_json_enabled,
    pretty_print_json,
    rank_palette,
    register_flags,
    register_shipped_invariants,
)


@dataclass
class _FakeDescriptor:
    slash_form: str = ""
    description: str = ""
    handler_method: str = ""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for env in (
        _ENV_MASTER, _ENV_PRETTY_JSON_ENABLED,
        _ENV_FAST_PATH_ENABLED, _ENV_FAST_PATH_MAX_LEN,
        _ENV_PALETTE_MAX_RESULTS, _ENV_PALETTE_DISTANCE,
        _ENV_JSON_INDENT, _ENV_OUTPUT_BOUND,
    ):
        monkeypatch.delenv(env, raising=False)
    yield


# Defaults


def test_schema():
    assert REPL_SMART_COMPLETION_SCHEMA_VERSION == "repl_smart_completion.1"


def test_master_default_false():
    assert master_enabled() is False


def test_pretty_json_default_true():
    assert pretty_json_enabled() is True


def test_fast_path_default_true():
    assert fast_path_enabled() is True


def test_fast_path_max_len_default():
    assert fast_path_max_len() == 200


def test_palette_max_results_default():
    assert palette_max_results() == 8


def test_palette_distance_default():
    assert palette_distance_threshold() == 3


def test_json_indent_default():
    assert json_indent() == 2


def test_output_bound_default():
    assert output_bound() == 8192


def test_kind_taxonomy_closed():
    assert {k.value for k in CompletionKind} == {
        "verb", "mention", "argument", "none",
    }


def test_format_taxonomy_closed():
    assert {f.value for f in OutputFormat} == {
        "plain", "pretty_json", "truncated", "disabled",
    }


@pytest.mark.parametrize("k", list(CompletionKind))
def test_kind_glyph(k):
    assert kind_glyph(k) != "?"


@pytest.mark.parametrize("f", list(OutputFormat))
def test_format_glyph(f):
    assert format_glyph(f) != "?"


# Example extraction


def test_extract_example_present():
    e = _extract_example_from_description(
        "Override posture. example: /posture HARDEN", "/posture",
    )
    assert e == "/posture HARDEN"


def test_extract_example_case_insensitive():
    e = _extract_example_from_description(
        "Show help. Example: /help verbs", "/help",
    )
    assert e == "/help verbs"


def test_extract_example_absent_returns_slash():
    e = _extract_example_from_description(
        "Just a description", "/x",
    )
    assert e == "/x"


# build_verb_help


def test_build_verb_help_master_off():
    assert build_verb_help("/help") is None


def test_build_verb_help_found(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (
        _FakeDescriptor(
            slash_form="/help",
            description="Show help. example: /help",
            handler_method="_handle_help",
        ),
    )
    help_artifact = build_verb_help(
        "/help", descriptors_override=descs,
    )
    assert isinstance(help_artifact, VerbHelp)
    assert help_artifact.slash_form == "/help"
    assert help_artifact.example_command == "/help"


def test_build_verb_help_normalizes_slash(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (
        _FakeDescriptor(slash_form="/help", description="d"),
    )
    h = build_verb_help("help", descriptors_override=descs)
    assert h is not None
    assert h.slash_form == "/help"


def test_build_verb_help_not_found(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (_FakeDescriptor(slash_form="/help"),)
    assert build_verb_help(
        "/posture", descriptors_override=descs,
    ) is None


def test_verb_help_render():
    h = VerbHelp(
        slash_form="/posture",
        description="Override posture",
        handler_method="_handle_posture",
        example_command="/posture HARDEN",
    )
    out = h.render()
    assert "/posture" in out
    assert "Override posture" in out
    assert "example: /posture HARDEN" in out


# rank_palette


def test_palette_master_off():
    result = rank_palette("/h")
    assert result.matches == ()
    assert _ENV_MASTER in result.diagnostic


def test_palette_empty_input_browse_mode(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (
        _FakeDescriptor(slash_form="/help", description="d1"),
        _FakeDescriptor(slash_form="/posture", description="d2"),
        _FakeDescriptor(slash_form="/status", description="d3"),
    )
    result = rank_palette(
        "", descriptors_override=descs,
    )
    assert len(result.matches) == 3
    slashes = {m.slash_form for m in result.matches}
    assert slashes == {"/help", "/posture", "/status"}


def test_palette_prefix_match_zero_score(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (
        _FakeDescriptor(slash_form="/help", description="d1"),
        _FakeDescriptor(slash_form="/helo", description="d2"),
        _FakeDescriptor(slash_form="/posture", description="d3"),
    )
    result = rank_palette("/he", descriptors_override=descs)
    # /help and /helo both prefix-match → score 0
    prefix = [m for m in result.matches if m.score == 0]
    assert len(prefix) == 2
    assert {m.slash_form for m in prefix} == {"/help", "/helo"}


def test_palette_fuzzy_match_threshold(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PALETTE_DISTANCE, "1")
    descs = (
        _FakeDescriptor(slash_form="/help"),
        _FakeDescriptor(slash_form="/posture"),
    )
    # /helo has distance 1 from /help, distance way more from /posture
    result = rank_palette("/helo", descriptors_override=descs)
    forms = [m.slash_form for m in result.matches]
    assert "/help" in forms
    assert "/posture" not in forms


def test_palette_non_slash_input(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    descs = (_FakeDescriptor(slash_form="/help"),)
    result = rank_palette("hello world", descriptors_override=descs)
    assert result.matches == ()


def test_palette_max_results_cap(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PALETTE_MAX_RESULTS, "2")
    descs = tuple(
        _FakeDescriptor(slash_form=f"/verb{i}")
        for i in range(10)
    )
    result = rank_palette(
        "/verb", descriptors_override=descs,
    )
    assert len(result.matches) == 2


def test_palette_no_registry(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = rank_palette("/help", descriptors_override=())
    assert result.matches == ()
    assert "no verb registry" in result.diagnostic


# JSON pretty-print


def test_is_json_shaped_object():
    assert is_json_shaped('{"a": 1}') is True


def test_is_json_shaped_array():
    assert is_json_shaped("[1, 2, 3]") is True


def test_is_json_shaped_with_leading_whitespace():
    assert is_json_shaped("  \n  {\"a\": 1}") is True


def test_is_json_shaped_plain_text():
    assert is_json_shaped("hello world") is False


def test_is_json_shaped_empty():
    assert is_json_shaped("") is False


def test_pretty_print_master_off():
    out = pretty_print_json('{"a": 1}')
    assert out.format is OutputFormat.DISABLED


def test_pretty_print_disabled_subflag(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PRETTY_JSON_ENABLED, "false")
    out = pretty_print_json('{"a": 1}')
    assert out.format is OutputFormat.PLAIN


def test_pretty_print_plain_text(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = pretty_print_json("just text")
    assert out.format is OutputFormat.PLAIN
    assert out.body == "just text"


def test_pretty_print_valid_json(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = pretty_print_json('{"b": 2, "a": 1}')
    assert out.format is OutputFormat.PRETTY_JSON
    # sort_keys=True → "a" before "b"
    assert out.body.index('"a"') < out.body.index('"b"')


def test_pretty_print_invalid_json_falls_back(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = pretty_print_json('{"bad": json}')
    assert out.format is OutputFormat.PLAIN
    assert "parse failed" in out.diagnostic.lower()


def test_pretty_print_truncated(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_OUTPUT_BOUND, "100")
    huge = json.dumps({f"key_{i}": "value" * 5 for i in range(20)})
    out = pretty_print_json(huge)
    assert out.format is OutputFormat.TRUNCATED
    assert "truncated" in out.body.lower()


def test_pretty_print_array(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = pretty_print_json("[3, 1, 2]")
    assert out.format is OutputFormat.PRETTY_JSON


def test_pretty_print_indent_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = pretty_print_json('{"a": 1}', indent_override=4)
    assert "    " in out.body  # 4-space indent


# classify_fast_path


def test_classify_master_off():
    out = classify_fast_path("simple question")
    assert out.is_fast_path is False


def test_classify_simple_question(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = classify_fast_path("what is the meaning of life?")
    assert out.is_fast_path is True


def test_classify_slash_command_not_fast(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = classify_fast_path("/help")
    assert out.is_fast_path is False
    assert out.has_slash is True


def test_classify_mention_not_fast(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = classify_fast_path("hey @file.py please review")
    assert out.is_fast_path is False
    assert out.has_mention is True


def test_classify_code_fence_not_fast(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = classify_fast_path("```python\nx = 1\n```")
    assert out.is_fast_path is False
    assert out.has_code_fence is True


def test_classify_long_input_not_fast(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FAST_PATH_MAX_LEN, "20")
    out = classify_fast_path("x" * 50)
    assert out.is_fast_path is False


def test_classify_empty_not_fast(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    out = classify_fast_path("")
    assert out.is_fast_path is False


def test_classify_subflag_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_FAST_PATH_ENABLED, "false")
    out = classify_fast_path("simple")
    assert out.is_fast_path is False


# Renderer


def test_format_panel_master_off():
    out = format_completion_panel()
    assert "disabled" in out


def test_format_panel_with_verb_help(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    h = VerbHelp(
        slash_form="/help",
        description="show help",
        handler_method="_handle_help",
        example_command="/help",
    )
    out = format_completion_panel(verb_help=h)
    assert "/help" in out


def test_format_panel_with_palette(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    p = PaletteResult(
        input_text="/h",
        matches=(
            CompletionMatch(
                slash_form="/help",
                description="show help",
                score=0,
                kind=CompletionKind.VERB,
            ),
        ),
        diagnostic="x",
        elapsed_ms=1.0,
    )
    out = format_completion_panel(palette=p)
    assert "Palette" in out
    assert "/help" in out


def test_format_panel_with_pretty_json(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    f = FormattedOutput(
        format=OutputFormat.PRETTY_JSON,
        body='{"a": 1}',
        original_bytes=8,
        formatted_bytes=8,
        diagnostic="ok",
    )
    out = format_completion_panel(output=f)
    assert "JSON" in out


def test_format_panel_with_classification(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = FastPathClassification(
        is_fast_path=True,
        input_length=10,
        has_slash=False, has_mention=False, has_code_fence=False,
        diagnostic="fast-path",
    )
    out = format_completion_panel(classification=c)
    assert "fast_path=True" in out


# to_dict


def test_verb_help_to_dict():
    h = VerbHelp(
        slash_form="/x", description="d",
        handler_method="m", example_command="/x",
    )
    d = h.to_dict()
    assert d["schema_version"] == REPL_SMART_COMPLETION_SCHEMA_VERSION


def test_palette_result_to_dict():
    p = PaletteResult(
        input_text="/h", matches=(), diagnostic="x", elapsed_ms=0.0,
    )
    d = p.to_dict()
    assert d["schema_version"] == REPL_SMART_COMPLETION_SCHEMA_VERSION


def test_formatted_output_to_dict():
    f = FormattedOutput(
        format=OutputFormat.PLAIN, body="x",
        original_bytes=1, formatted_bytes=1, diagnostic="",
    )
    d = f.to_dict()
    assert d["format"] == "plain"


def test_fast_path_to_dict():
    c = FastPathClassification(
        is_fast_path=True, input_length=5,
        has_slash=False, has_mention=False, has_code_fence=False,
        diagnostic="",
    )
    d = c.to_dict()
    assert d["is_fast_path"] is True


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "repl_smart_completion.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "kind_taxonomy_closed",
        "format_taxonomy_closed",
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


def test_pin_kind_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "kind_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class CompletionKind(str, enum.Enum):\n"
        "    VERB = 'verb'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_format_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "format_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class OutputFormat(str, enum.Enum):\n"
        "    PLAIN = 'plain'\n"
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
    assert count == 8


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False
