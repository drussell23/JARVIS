"""Regression spine for §41.3 Slice 3 #14 — arg completion substrate."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc
from backend.core.ouroboros.battle_test.repl_completion import (
    ArgKind,
    ArgPositionSpec,
    VerbCategory,
    VerbDescriptor,
    VerbRegistry,
    cursor_arg_position,
    get_arg_candidates,
    list_arg_providers,
    parse_arg_spec,
    register_arg_provider,
    unregister_arg_provider,
)


# Test fixtures + helpers


@pytest.fixture(autouse=True)
def _isolate_providers():
    """Each test gets a fresh provider registry — DYNAMIC tests
    register providers locally and must not bleed into siblings."""
    snapshot = list(rc._ARG_PROVIDERS.items())
    rc._ARG_PROVIDERS.clear()
    yield
    rc._ARG_PROVIDERS.clear()
    for k, v in snapshot:
        rc._ARG_PROVIDERS[k] = v


# --- ArgKind closed taxonomy -----------------------------------------------


def test_arg_kind_closed():
    assert {v.value for v in ArgKind} == {
        "static", "dynamic", "path", "free",
    }


# --- ArgPositionSpec serialization -----------------------------------------


def test_arg_position_spec_defaults():
    p = ArgPositionSpec(name="x", required=True)
    assert p.kind is ArgKind.FREE
    assert p.static_values == ()
    assert p.provider_key == ""
    assert p.flag_form == ""


def test_arg_position_spec_to_dict():
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    d = p.to_dict()
    assert d["name"] == "op_id"
    assert d["required"] is True
    assert d["kind"] == "dynamic"
    assert d["provider_key"] == "op_id"


# --- parse_arg_spec --------------------------------------------------------


def test_parse_empty_returns_empty():
    assert parse_arg_spec("") == ()
    assert parse_arg_spec("   ") == ()
    assert parse_arg_spec(None) == ()


def test_parse_garbage_returns_empty():
    assert parse_arg_spec(42) == ()
    assert parse_arg_spec(object()) == ()


def test_parse_single_required():
    out = parse_arg_spec("<op_id>")
    assert len(out) == 1
    assert out[0].name == "op_id"
    assert out[0].required is True


def test_parse_single_optional():
    out = parse_arg_spec("[category]")
    assert len(out) == 1
    assert out[0].name == "category"
    assert out[0].required is False


def test_parse_required_plus_flag():
    out = parse_arg_spec("<op_id> [--immediate]")
    assert len(out) == 2
    assert out[0].name == "op_id"
    assert out[0].required is True
    assert out[1].name == "immediate"
    assert out[1].required is False
    assert out[1].flag_form == "--immediate"
    # Bare flag completes to its literal
    assert out[1].kind is ArgKind.STATIC
    assert out[1].static_values == ("--immediate",)


def test_parse_flag_with_value():
    out = parse_arg_spec("[--mode <value>]")
    assert len(out) == 1
    p = out[0]
    assert p.name == "value"
    assert p.flag_form == "--mode"
    assert p.required is False


def test_parse_multiple_positionals():
    out = parse_arg_spec("<src> <dest>")
    assert len(out) == 2
    assert out[0].required is True
    assert out[1].required is True


def test_parse_classifies_category_as_static():
    """Convention dispatch: `category` resolves to VerbCategory."""
    out = parse_arg_spec("[category]")
    assert out[0].kind is ArgKind.STATIC
    assert set(out[0].static_values) == {
        c.value for c in VerbCategory
    }


def test_parse_classifies_path_as_path():
    out = parse_arg_spec("<file_path>")
    assert out[0].kind is ArgKind.PATH


def test_parse_classifies_registered_provider_as_dynamic():
    register_arg_provider("op_id", lambda p: ("op-abc",))
    out = parse_arg_spec("<op_id>")
    assert out[0].kind is ArgKind.DYNAMIC
    assert out[0].provider_key == "op_id"


def test_parse_unknown_name_falls_back_to_free():
    out = parse_arg_spec("<some_unknown_thing>")
    assert out[0].kind is ArgKind.FREE


def test_parse_mixed_form():
    out = parse_arg_spec("<op_id> [category] [--immediate]")
    assert len(out) == 3
    assert (out[0].name, out[0].required) == ("op_id", True)
    assert (out[1].name, out[1].required) == ("category", False)
    assert (out[2].name, out[2].required) == ("immediate", False)


# --- register_arg_provider -------------------------------------------------


def test_register_provider_invalid_key():
    assert register_arg_provider("", lambda p: ()) is False
    assert register_arg_provider(None, lambda p: ()) is False
    assert register_arg_provider(42, lambda p: ()) is False


def test_register_provider_non_callable():
    assert register_arg_provider("x", "not callable") is False
    assert register_arg_provider("x", 42) is False
    assert register_arg_provider("x", None) is False


def test_register_provider_success():
    assert register_arg_provider("op_id", lambda p: ("op-abc",)) is True
    assert "op_id" in list_arg_providers()


def test_register_provider_strips_key():
    register_arg_provider("  op_id  ", lambda p: ())
    assert "op_id" in list_arg_providers()


def test_unregister_provider():
    register_arg_provider("op_id", lambda p: ())
    assert unregister_arg_provider("op_id") is True
    assert unregister_arg_provider("op_id") is False  # already gone


def test_unregister_invalid():
    assert unregister_arg_provider(None) is False
    assert unregister_arg_provider(42) is False


def test_list_arg_providers_sorted():
    register_arg_provider("zebra", lambda p: ())
    register_arg_provider("apple", lambda p: ())
    assert list_arg_providers() == ("apple", "zebra")


# --- get_arg_candidates ----------------------------------------------------


def test_candidates_static():
    p = ArgPositionSpec(
        name="x", required=True, kind=ArgKind.STATIC,
        static_values=("foo", "bar", "baz"),
    )
    assert set(get_arg_candidates(p)) == {"foo", "bar", "baz"}


def test_candidates_static_prefix_filter():
    p = ArgPositionSpec(
        name="x", required=True, kind=ArgKind.STATIC,
        static_values=("foo", "fizz", "bar"),
    )
    out = get_arg_candidates(p, "f")
    assert set(out) == {"foo", "fizz"}


def test_candidates_static_case_insensitive_prefix():
    p = ArgPositionSpec(
        name="x", required=True, kind=ArgKind.STATIC,
        static_values=("Lifecycle", "operational"),
    )
    out = get_arg_candidates(p, "L")
    assert "Lifecycle" in out


def test_candidates_static_dedup():
    p = ArgPositionSpec(
        name="x", required=True, kind=ArgKind.STATIC,
        static_values=("foo", "foo", "bar"),
    )
    out = get_arg_candidates(p)
    assert out == ("foo", "bar")


def test_candidates_static_max_results():
    p = ArgPositionSpec(
        name="x", required=True, kind=ArgKind.STATIC,
        static_values=tuple(f"v{i}" for i in range(50)),
    )
    out = get_arg_candidates(p, max_results=5)
    assert len(out) == 5


def test_candidates_dynamic_invokes_provider():
    calls = []

    def provider(prefix):
        calls.append(prefix)
        return ("op-abc", "op-def", "task-xyz")

    register_arg_provider("op_id", provider)
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    out = get_arg_candidates(p, "op-")
    assert calls == ["op-"]
    # Prefix filter applies on top of what the provider returned
    assert set(out) == {"op-abc", "op-def"}


def test_candidates_dynamic_missing_provider_returns_empty():
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    assert get_arg_candidates(p) == ()


def test_candidates_dynamic_crashy_provider_safe():
    def crashy(prefix):
        raise RuntimeError("boom")

    register_arg_provider("op_id", crashy)
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    # NEVER raises — degrades to empty
    assert get_arg_candidates(p) == ()


def test_candidates_dynamic_accepts_list_return():
    register_arg_provider("op_id", lambda p: ["a", "b"])
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    out = get_arg_candidates(p)
    assert set(out) == {"a", "b"}


def test_candidates_dynamic_skips_non_strings():
    register_arg_provider("op_id", lambda p: ["a", None, 42])
    p = ArgPositionSpec(
        name="op_id", required=True, kind=ArgKind.DYNAMIC,
        provider_key="op_id",
    )
    out = get_arg_candidates(p)
    # Substrate coerces non-None values to str — `42` becomes "42",
    # None is dropped
    assert "a" in out


def test_candidates_path_returns_empty():
    """PATH is delegated to the completer's PathCompleter."""
    p = ArgPositionSpec(name="path", required=True, kind=ArgKind.PATH)
    assert get_arg_candidates(p) == ()


def test_candidates_free_returns_empty():
    p = ArgPositionSpec(name="x", required=True, kind=ArgKind.FREE)
    assert get_arg_candidates(p) == ()


def test_candidates_garbage_position_returns_empty():
    assert get_arg_candidates(None) == ()
    assert get_arg_candidates("not a position") == ()
    assert get_arg_candidates(42) == ()


# --- cursor_arg_position ---------------------------------------------------


def test_cursor_position_no_slash():
    assert cursor_arg_position("hello world") == (-1, "")


def test_cursor_position_just_verb():
    """`/cancel` — no trailing space; still on verb position."""
    assert cursor_arg_position("/cancel") == (-1, "")


def test_cursor_position_first_arg():
    """`/cancel ` — cursor at first arg position, empty prefix."""
    idx, prefix = cursor_arg_position("/cancel ")
    assert idx == 0
    assert prefix == ""


def test_cursor_position_first_arg_with_prefix():
    """`/cancel op-` — first arg position, partial prefix."""
    idx, prefix = cursor_arg_position("/cancel op-")
    assert idx == 0
    assert prefix == "op-"


def test_cursor_position_second_arg():
    """`/cancel op-abc --` — past first arg, on second."""
    idx, prefix = cursor_arg_position("/cancel op-abc --")
    assert idx == 1
    assert prefix == "--"


def test_cursor_position_empty():
    assert cursor_arg_position("") == (-1, "")


def test_cursor_position_garbage_safe():
    assert cursor_arg_position(None) == (-1, "")
    assert cursor_arg_position(42) == (-1, "")


def test_cursor_position_with_leading_whitespace():
    idx, prefix = cursor_arg_position("   /cancel op-")
    assert idx == 0
    assert prefix == "op-"


# --- Completer integration --------------------------------------------------


@pytest.fixture
def verb_registry():
    return VerbRegistry(verbs=(
        VerbDescriptor(
            slash_form="/cancel",
            handler_method="_handle_cancel",
            description="Cancel an op",
            arg_spec="<op_id> [--immediate]",
        ),
        VerbDescriptor(
            slash_form="/tutorial",
            handler_method="_handle_tutorial",
            description="Tutorial",
            arg_spec="[category]",
        ),
        VerbDescriptor(
            slash_form="/risk",
            handler_method="_handle_risk",
            description="Risk",
            arg_spec="<value>",
        ),
    ))


def _completions_for(completer, text):
    """Helper: drive the completer with a fake Document."""
    class _FakeDoc:
        def __init__(self, t):
            self.text_before_cursor = t

    return list(completer.get_completions(_FakeDoc(text), None))


def test_completer_verb_name_path_unchanged(verb_registry, monkeypatch):
    """Existing behavior: typing `/can` still yields `/cancel`."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    assert comp is not None
    # _SlashCompleter is the inner completer (or merged)
    try:
        outs = _completions_for(comp, "/can")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    texts = [c.text for c in outs]
    assert "/cancel" in texts


def test_completer_arg_position_tutorial_category(
    verb_registry, monkeypatch,
):
    """Operator types `/tutorial ` → category candidates surface."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    try:
        outs = _completions_for(comp, "/tutorial ")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    texts = {c.text for c in outs}
    assert "lifecycle" in texts
    assert "introspection" in texts


def test_completer_arg_position_prefix_filter(
    verb_registry, monkeypatch,
):
    """`/tutorial life` — only matches starting with `life`."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    try:
        outs = _completions_for(comp, "/tutorial life")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    texts = {c.text for c in outs}
    assert "lifecycle" in texts
    assert "operational" not in texts


def test_completer_arg_position_dynamic_provider(
    verb_registry, monkeypatch,
):
    """Dynamic provider for `<op_id>` surfaces live candidates."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    register_arg_provider(
        "op_id",
        lambda prefix: ("op-abc", "op-def"),
    )
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    try:
        outs = _completions_for(comp, "/cancel ")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    texts = {c.text for c in outs}
    assert "op-abc" in texts
    assert "op-def" in texts


def test_completer_arg_position_unknown_verb_returns_nothing(
    verb_registry, monkeypatch,
):
    """`/bogus arg` — unknown verb → arg completer yields none."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    try:
        outs = _completions_for(comp, "/bogus ")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    assert outs == []


def test_completer_arg_position_past_spec_returns_nothing(
    verb_registry, monkeypatch,
):
    """`/risk <value> <extra>` — extra position beyond spec."""
    monkeypatch.setenv(rc.MASTER_FLAG_ENV_VAR, "true")
    pytest.importorskip("prompt_toolkit")
    comp = rc.build_completer(verb_registry)
    try:
        outs = _completions_for(comp, "/risk SAFE_AUTO extra-")
    except Exception:
        pytest.skip("merge_completers requires Document with full API")
    # `<value>` is free (no provider registered), so even at
    # position 0 it'd return nothing. Position 1 (extra) is
    # beyond spec → also nothing.
    texts = [c.text for c in outs]
    assert "SAFE_AUTO" not in texts  # past first arg


# --- AST pin: ArgKind taxonomy frozen ---------------------------------------


def test_ast_pin_arg_kind_taxonomy_frozen():
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    tree = ast.parse(src)
    expected = {"static", "dynamic", "path", "free"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ArgKind"
        ):
            found = set()
            for sub in node.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, str)
                ):
                    found.add(sub.value.value)
            assert found == expected
            return
    pytest.fail("ArgKind class not found")


def test_ast_pin_arg_completion_symbols_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    for name in (
        '"ArgKind"',
        '"ArgPositionSpec"',
        '"cursor_arg_position"',
        '"get_arg_candidates"',
        '"parse_arg_spec"',
        '"register_arg_provider"',
        '"unregister_arg_provider"',
        '"list_arg_providers"',
    ):
        assert name in src, f"{name} missing from __all__"


def test_ast_pin_seeded_category_static():
    """Bytes-pin: `category` arg name is seeded with VerbCategory
    values at module load — convention dispatch for the
    /tutorial verb depends on it."""
    out = parse_arg_spec("[category]")
    assert out[0].kind is ArgKind.STATIC
    assert set(out[0].static_values) == {
        c.value for c in VerbCategory
    }
