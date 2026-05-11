"""Regression spine for §41.3 #8 — JSON pretty-print in tool_render_view.

Substrate tests for the JSON detection + pretty-printing +
token-coloring substrate. Compositional: hooks into the existing
`tool_render_view.compose()` between descriptor lookup and the
per-line wrapping loop. NO new BodyShape value (closed taxonomy
preserved). NO parallel rendering pipeline. The same `compose`
+ `render` + `BoundedBodyStore` flow handles both legacy and
JSON-detected paths.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import tool_render_view as trv
from backend.core.ouroboros.battle_test.tool_render_view import (
    JSON_PRETTY_ENABLED_ENV_VAR,
    JSON_PRETTY_MIN_SIZE_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    _JSON_TOKEN_PALETTE_KEYS,
    _detect_json_body,
    _json_palette_value,
    _wrap_json_line,
    compose,
    is_json_pretty_enabled,
    json_pretty_min_size,
    pretty_print_json,
)


# --- Env knobs ------------------------------------------------------------


def test_is_json_pretty_default_true(monkeypatch):
    monkeypatch.delenv(JSON_PRETTY_ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    assert is_json_pretty_enabled() is True


def test_is_json_pretty_explicit_off(monkeypatch):
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "false")
    assert is_json_pretty_enabled() is False


def test_is_json_pretty_off_aliases(monkeypatch):
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, off)
        assert is_json_pretty_enabled() is False, off


def test_is_json_pretty_implicit_off_when_registry_master_off(
    monkeypatch,
):
    """When the registry master is off, the rendering path
    isn't engaged at all → JSON pretty implicitly off."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    assert is_json_pretty_enabled() is False


def test_min_size_default(monkeypatch):
    monkeypatch.delenv(JSON_PRETTY_MIN_SIZE_ENV_VAR, raising=False)
    assert json_pretty_min_size() == 60


def test_min_size_clamps_high(monkeypatch):
    monkeypatch.setenv(JSON_PRETTY_MIN_SIZE_ENV_VAR, "999999999")
    assert json_pretty_min_size() == 100_000


def test_min_size_clamps_low(monkeypatch):
    monkeypatch.setenv(JSON_PRETTY_MIN_SIZE_ENV_VAR, "0")
    assert json_pretty_min_size() == 10


def test_min_size_garbage(monkeypatch):
    monkeypatch.setenv(JSON_PRETTY_MIN_SIZE_ENV_VAR, "not a num")
    assert json_pretty_min_size() == 60


# --- _detect_json_body — tier 2 (content auto-detect) --------------------


def test_detect_obj_above_threshold():
    body = json.dumps({"name": "value", "count": 42, "items": [1, 2, 3]})
    parsed = _detect_json_body(body, min_size=10)
    assert isinstance(parsed, dict)
    assert parsed["name"] == "value"


def test_detect_array_above_threshold():
    body = json.dumps([{"a": 1}, {"a": 2}, {"a": 3}])
    parsed = _detect_json_body(body, min_size=10)
    assert isinstance(parsed, list)
    assert len(parsed) == 3


def test_detect_below_threshold_returns_none():
    """Tiny JSON doesn't benefit from pretty-printing — detect
    refuses so legacy rendering surfaces the raw text."""
    parsed = _detect_json_body('{"ok":true}', min_size=60)
    assert parsed is None


def test_detect_not_json_returns_none():
    assert _detect_json_body("hello world", min_size=10) is None
    assert _detect_json_body("function foo() {}", min_size=10) is None


def test_detect_malformed_json_returns_none():
    """Broken JSON shouldn't crash — degrades to legacy render."""
    bad = "{name: value}"  # not actually JSON
    assert _detect_json_body(bad, min_size=5) is None


def test_detect_empty_returns_none():
    assert _detect_json_body("", min_size=10) is None
    assert _detect_json_body(None, min_size=10) is None
    assert _detect_json_body("   ", min_size=10) is None


def test_detect_garbage_input_safe():
    assert _detect_json_body(42, min_size=10) is None  # int — not JSON-shaped
    assert _detect_json_body([1, 2, 3], min_size=10) is None  # list, not str


def test_detect_strips_whitespace():
    body = "   \n  " + json.dumps({"key": "value"}) + "\n\n"
    # Even though raw text is longer than min_size, strip + content check
    parsed = _detect_json_body(body, min_size=10)
    assert parsed == {"key": "value"}


# --- _detect_json_body — tier 1 (descriptor hint) ------------------------


def test_detect_hint_wins_over_size_threshold():
    """When body_lexer_hint='json', even tiny JSON is detected."""
    parsed = _detect_json_body(
        '{"ok":true}', body_lexer_hint="json", min_size=999,
    )
    assert parsed == {"ok": True}


def test_detect_hint_validates_parses():
    """Hint says JSON, but body is invalid → still returns None."""
    parsed = _detect_json_body(
        "not actually json", body_lexer_hint="json", min_size=999,
    )
    assert parsed is None


def test_detect_hint_case_insensitive():
    parsed = _detect_json_body(
        '{"k":1}', body_lexer_hint="JSON", min_size=999,
    )
    assert parsed == {"k": 1}


def test_detect_hint_other_lexer_falls_to_auto():
    """When the descriptor declares a non-JSON lexer, tier 2
    runs normally with the size threshold."""
    parsed = _detect_json_body(
        '{"a":1}', body_lexer_hint="python", min_size=999,
    )
    # Below threshold → None even though it parses
    assert parsed is None


# --- pretty_print_json ---------------------------------------------------


def test_pretty_print_dict():
    out = pretty_print_json({"key": "value", "n": 42})
    assert "{\n" in out
    assert '"key"' in out
    # Default indent = 2
    assert "  " in out


def test_pretty_print_array():
    out = pretty_print_json([1, 2, 3])
    assert "[\n" in out
    assert "1," in out


def test_pretty_print_indent_param():
    out = pretty_print_json({"a": 1}, indent=4)
    assert "    " in out


def test_pretty_print_garbage_falls_back():
    """Non-serializable objects fall back to str()."""
    class _Bogus:
        def __repr__(self):
            return "BOGUS"

    out = pretty_print_json(_Bogus())
    # default=str makes it serializable, but the structure isn't
    # a primitive; output should at least be non-empty
    assert isinstance(out, str)


def test_pretty_print_negative_indent_clamps():
    """Negative indent clamps to 0. `json.dumps(indent=0)` still
    inserts newlines but no leading spaces — verify shape, not
    one-liner-ness."""
    out = pretty_print_json({"a": 1, "b": 2}, indent=-5)
    # No leading-space indent on inner lines
    assert "  " not in out


def test_pretty_print_nested():
    nested = {"outer": {"inner": {"deep": [1, 2, 3]}}}
    out = pretty_print_json(nested)
    assert "outer" in out
    assert "inner" in out
    assert "deep" in out


# --- Palette table -------------------------------------------------------


def test_palette_table_has_5_token_types():
    """Data on module: keys / strings / numbers / keywords /
    punctuation. AST-pinned at 5 entries to prevent silent
    drop / addition without an explicit pin update."""
    keys = {k for k, _ in _JSON_TOKEN_PALETTE_KEYS}
    assert keys == {
        "code_key", "code_str", "code_num", "code_kw", "code_punct",
    }


def test_json_palette_fallback_to_default():
    """When the operator-passed palette doesn't carry json keys,
    helper falls back to the table defaults."""
    assert _json_palette_value(None, "code_key") == "cyan"
    assert _json_palette_value(None, "code_str") == "green"
    assert _json_palette_value({}, "code_num") == "magenta"


def test_json_palette_operator_override():
    """Operator can theme JSON via the same palette mapping
    they pass to compose()."""
    palette = {"code_key": "yellow"}
    assert _json_palette_value(palette, "code_key") == "yellow"


def test_json_palette_unknown_key_returns_white():
    assert _json_palette_value(None, "unknown_key") == "white"


# --- _wrap_json_line ------------------------------------------------------


def test_wrap_key_value_pair():
    line = '  "name": "value",'
    out = _wrap_json_line(line, None)
    assert "[cyan]" in out  # key token
    assert "[green]" in out  # string value
    assert "[dim]" in out  # punctuation


def test_wrap_number_value():
    line = '  "count": 42,'
    out = _wrap_json_line(line, None)
    assert "[magenta]" in out  # number


def test_wrap_boolean_keywords():
    line = '  "ok": true,'
    out = _wrap_json_line(line, None)
    assert "[bright_black bold]" in out


def test_wrap_null_keyword():
    line = '  "v": null'
    out = _wrap_json_line(line, None)
    assert "[bright_black bold]" in out
    assert "null" in out


def test_wrap_punctuation_only():
    line = "  },"
    out = _wrap_json_line(line, None)
    # `}` and `,` are both punctuation
    assert "[dim]" in out


def test_wrap_handles_array_brackets():
    line = '  "items": ['
    out = _wrap_json_line(line, None)
    assert "[" in out  # the bracket char survives


def test_wrap_never_raises_on_garbage():
    """Non-JSON text passed in should not crash the wrapper;
    it'll just emit escaped plain text."""
    try:
        out = _wrap_json_line("some random ☆ text", None)
        assert isinstance(out, str)
    except Exception:
        pytest.fail("_wrap_json_line raised")


def test_wrap_preserves_indentation():
    line = "      \"deep_key\": 1"
    out = _wrap_json_line(line, None)
    # Leading whitespace preserved as plain text
    assert out.startswith("      ") or "      " in out


def test_wrap_palette_override():
    palette = {"code_key": "red"}
    line = '"foo":'
    out = _wrap_json_line(line, palette)
    assert "[red]" in out
    assert "[cyan]" not in out  # default key color


# --- compose() end-to-end ------------------------------------------------


def _compose_result(result_str, **kwargs):
    """Helper: call compose() with safe defaults + explicit
    DensityPolicy so the result is deterministic. Uses the
    canonical 3-value DensityLevel (verbose for the most-lines
    headroom) so the bounding cap doesn't drop our test body."""
    from backend.core.ouroboros.battle_test.tool_render_policy import (
        DensityLevel,
        DensityPolicy,
    )
    policy = DensityPolicy(
        level=DensityLevel.VERBOSE,
        max_body_lines=20,
        max_summary_chars=200,
        provenance="test:explicit",
    )
    return compose(
        tool_name="web_fetch",
        args_str="https://api.example.com/data",
        result_str=result_str,
        explicit_density=policy,
        **kwargs,
    )


def test_compose_pretty_prints_json_body(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    # Body must exceed JSON_PRETTY_MIN_SIZE (default 60) for tier-2
    # auto-detection to fire.
    body = json.dumps({
        "users": [
            {"id": 1, "name": "Alice", "email": "alice@example.com"},
            {"id": 2, "name": "Bob", "email": "bob@example.com"},
        ],
        "total": 2,
    })
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    # Pretty-printed body is multi-line and contains key markup
    assert len(result.body_lines_markup) > 1
    assert "[cyan]" in joined or "[green]" in joined


def test_compose_skips_pretty_when_disabled(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "false")
    body = json.dumps(
        {"users": [{"id": 1, "name": "Alice"}, {"id": 2}]}
    )
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    # Without pretty: legacy text wrapping (dim only, no token colors)
    assert "[cyan]" not in joined
    assert "[magenta]" not in joined


def test_compose_skips_pretty_for_non_json(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    body = "Just a plain text response\nwith multiple lines\nnot json"
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    assert "[cyan]" not in joined
    assert "[magenta]" not in joined


def test_compose_skips_pretty_for_tiny_json(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_MIN_SIZE_ENV_VAR, "60")
    body = '{"ok":true}'  # below threshold
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    # No JSON token colors for sub-threshold body
    assert "[cyan]" not in joined or "true" not in joined


def test_compose_pretty_respects_body_cap(monkeypatch):
    """When pretty-printed JSON exceeds max_body_lines, the
    render() bounding kicks in → body_lines stays capped."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    big_obj = {f"key_{i}": f"value_{i}" for i in range(50)}
    body = json.dumps(big_obj)
    result = _compose_result(body)
    # max_body_lines=20 per the fixture
    assert len(result.body_lines_markup) <= 22  # 20 + elision markers


def test_compose_handles_array_root(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    body = json.dumps([
        {"id": 1, "name": "Alice", "active": True},
        {"id": 2, "name": "Bob", "active": False},
    ])
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    # Array root → indented children
    assert "[" in joined  # bracket survives
    assert "Alice" in joined


def test_compose_master_off_skips_json_entirely(monkeypatch):
    """When registry master is off, JSON pretty is implicitly off."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    monkeypatch.setenv(JSON_PRETTY_ENABLED_ENV_VAR, "true")
    body = json.dumps({"users": [{"id": 1, "name": "Alice"}]})
    result = _compose_result(body)
    joined = "\n".join(result.body_lines_markup)
    assert "[cyan]" not in joined


# --- AST pins ------------------------------------------------------------


def test_ast_pin_json_symbols_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/tool_render_view.py"
    ).read_text()
    for name in (
        "JSON_PRETTY_ENABLED_ENV_VAR",
        "JSON_PRETTY_MIN_SIZE_ENV_VAR",
        "is_json_pretty_enabled",
        "json_pretty_min_size",
        "_detect_json_body",
        "_wrap_json_line",
        "pretty_print_json",
    ):
        # Module-level def or assignment
        assert name in src, f"{name} missing from module"


def test_ast_pin_compose_routes_json_detection():
    """Bytes-pin: compose() invokes _detect_json_body BEFORE the
    render() call, so the bounding cap applies to the pretty
    version. NEVER raises into render()."""
    src = Path(
        "backend/core/ouroboros/battle_test/tool_render_view.py"
    ).read_text()
    idx_detect = src.find("_detect_json_body(")
    idx_render = src.find("rendered = render(")
    assert idx_detect > 0, "_detect_json_body call missing"
    assert idx_render > 0, "render() call missing"
    assert idx_detect < idx_render, (
        "_detect_json_body must run BEFORE render() so the "
        "bounding cap applies to the pretty version"
    )


def test_ast_pin_json_wrapper_overrides_per_shape():
    """Bytes-pin: when JSON was detected, the per-line wrapper
    must be `_wrap_json_line`, NOT the shape-dispatched
    `_BODY_WRAPPERS.get(...)`. Operator-binding 2026-05-11:
    no parallel render-pipeline; same compose() flow."""
    src = Path(
        "backend/core/ouroboros/battle_test/tool_render_view.py"
    ).read_text()
    idx = src.find("if _json_detected:")
    assert idx > 0, "_json_detected branch missing in compose()"
    branch = src[idx:idx + 400]
    assert "_wrap_json_line" in branch
    assert "wrapper = _wrap_json_line" in branch


def test_ast_pin_palette_keys_table_is_data():
    """Bytes-pin: _JSON_TOKEN_PALETTE_KEYS lives at module scope
    as a tuple-of-tuples. Adding a token type = edit table, not
    code."""
    src = Path(
        "backend/core/ouroboros/battle_test/tool_render_view.py"
    ).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_JSON_TOKEN_PALETTE_KEYS"
            and isinstance(node.value, ast.Tuple)
        ):
            assert len(node.value.elts) >= 5
            return
    pytest.fail("_JSON_TOKEN_PALETTE_KEYS table not found")
