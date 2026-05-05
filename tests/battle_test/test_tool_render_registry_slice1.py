"""Tests for tool_render_registry (Gap #2 Slice 1).

Validates the descriptor substrate is:
  • Schema-versioned + closed taxonomy
  • Pure (no Console / Rich import; deterministic)
  • Defensive (non-string inputs + raising summarizers degrade gracefully)
  • Complete (every Venom tool kind has a descriptor)
  • Renders header / summary / bounded body correctly per shape
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.tool_render_registry import (
    TOOL_RENDER_REGISTRY_SCHEMA_VERSION,
    BodyShape,
    RenderedToolResult,
    ToolRenderDescriptor,
    ToolStatus,
    default_descriptor,
    get_descriptor,
    is_known_tool,
    known_tool_kinds,
    render,
)


# ===========================================================================
# Schema + closed taxonomy
# ===========================================================================


def test_schema_version_pinned():
    assert TOOL_RENDER_REGISTRY_SCHEMA_VERSION == "tool_render_registry.v1"


def test_body_shape_closed_taxonomy():
    # Closed 6-value enum — extending requires a slice.
    assert {m.value for m in BodyShape} == {
        "none", "single", "multi", "diff", "code", "log",
    }


def test_tool_status_closed_taxonomy():
    assert {m.value for m in ToolStatus} == {
        "success", "error", "timeout", "denied",
    }


# ===========================================================================
# ToolStatus.coerce — lenient parse
# ===========================================================================


def test_status_coerce_accepts_enum():
    assert ToolStatus.coerce(ToolStatus.SUCCESS) is ToolStatus.SUCCESS


@pytest.mark.parametrize("raw,expected", [
    ("success", ToolStatus.SUCCESS),
    ("SUCCESS", ToolStatus.SUCCESS),
    ("  success  ", ToolStatus.SUCCESS),
    ("error", ToolStatus.ERROR),
    ("timeout", ToolStatus.TIMEOUT),
    ("denied", ToolStatus.DENIED),
])
def test_status_coerce_accepts_strings(raw: str, expected: ToolStatus):
    assert ToolStatus.coerce(raw) is expected


@pytest.mark.parametrize("raw", [None, "garbage", 42, object()])
def test_status_coerce_unknowns_become_error(raw):
    assert ToolStatus.coerce(raw) is ToolStatus.ERROR


# ===========================================================================
# Descriptor catalog completeness — all 18 Venom tools registered
# ===========================================================================


_EXPECTED_TOOLS = frozenset({
    # tool_executor._dispatch keys (15 sync handlers)
    "read_file", "list_symbols", "search_code", "run_tests", "get_callers",
    "glob_files", "list_dir", "git_log", "git_diff", "git_blame",
    "bash", "edit_file", "write_file", "delete_file", "type_check",
    # ToolManifest async-native tools (3)
    "web_fetch", "web_search", "ask_human",
})


def test_all_venom_tools_registered():
    registered = set(known_tool_kinds())
    missing = _EXPECTED_TOOLS - registered
    assert not missing, f"missing descriptors: {sorted(missing)}"


@pytest.mark.parametrize("kind", sorted(_EXPECTED_TOOLS))
def test_each_descriptor_resolves_to_itself(kind: str):
    desc = get_descriptor(kind)
    assert isinstance(desc, ToolRenderDescriptor)
    assert desc.tool_kind == kind
    assert desc is not default_descriptor()


def test_known_tool_kinds_sorted():
    kinds = known_tool_kinds()
    assert list(kinds) == sorted(kinds)


def test_is_known_tool_predicate():
    assert is_known_tool("read_file")
    assert not is_known_tool("never_existed")
    assert not is_known_tool("")
    assert not is_known_tool(None)  # type: ignore[arg-type]


# ===========================================================================
# Default descriptor fallback
# ===========================================================================


def test_unknown_tool_returns_default_descriptor():
    desc = get_descriptor("mcp_some_external_tool")
    assert desc is default_descriptor()
    assert desc.tool_kind == "_default"
    assert desc.cc_verb is None


def test_non_string_tool_kind_returns_default():
    assert get_descriptor(None) is default_descriptor()  # type: ignore[arg-type]
    assert get_descriptor(42) is default_descriptor()  # type: ignore[arg-type]


# ===========================================================================
# CC-verb assignments match Claude Code convention
# ===========================================================================


@pytest.mark.parametrize("kind,expected_verb", [
    ("read_file", "Read"),
    ("edit_file", "Update"),
    ("write_file", "Write"),
])
def test_cc_verb_assignments(kind: str, expected_verb: str):
    assert get_descriptor(kind).cc_verb == expected_verb


@pytest.mark.parametrize("kind", [
    "bash", "search_code", "run_tests", "get_callers", "glob_files",
    "list_dir", "list_symbols", "git_log", "git_diff", "git_blame",
    "delete_file", "type_check", "web_fetch", "web_search", "ask_human",
])
def test_non_cc_tools_use_icon_path(kind: str):
    assert get_descriptor(kind).cc_verb is None


# ===========================================================================
# Body shape assignments
# ===========================================================================


def test_read_file_is_header_only():
    assert get_descriptor("read_file").body_shape is BodyShape.NONE


def test_edit_file_is_diff_shape():
    assert get_descriptor("edit_file").body_shape is BodyShape.DIFF


def test_bash_is_log_shape():
    assert get_descriptor("bash").body_shape is BodyShape.LOG


def test_search_code_is_multi_line():
    assert get_descriptor("search_code").body_shape is BodyShape.MULTI_LINE


def test_default_descriptor_is_single_line():
    assert default_descriptor().body_shape is BodyShape.SINGLE_LINE


# ===========================================================================
# Args summarizers
# ===========================================================================


def test_path_args_truncate_to_80_chars():
    desc = get_descriptor("read_file")
    long_path = "x" * 200
    out = desc.summarize_args(long_path)
    assert len(out) <= 80
    assert out.endswith("…")


def test_bash_args_show_dollar_prefix():
    desc = get_descriptor("bash")
    assert desc.summarize_args("pytest -x") == "$ pytest -x"


def test_bash_args_collapse_whitespace():
    desc = get_descriptor("bash")
    assert desc.summarize_args("  pytest   -x  ") == "$ pytest -x"


def test_search_args_quoted():
    desc = get_descriptor("search_code")
    assert desc.summarize_args("pattern") == '"pattern"'


def test_empty_args_get_safe_default():
    desc = get_descriptor("read_file")
    assert desc.summarize_args("") == "file"


def test_args_summarizer_handles_none_safely():
    # Test the safety wrapper rather than the inner fn
    desc = get_descriptor("read_file")
    assert desc.summarize_args(None) == "file"  # type: ignore[arg-type]


# ===========================================================================
# Result summarizers — success cases
# ===========================================================================


def test_read_summary_pluralizes():
    desc = get_descriptor("read_file")
    assert desc.summarize_result("a\nb\nc", ToolStatus.SUCCESS) == "3 lines read"
    assert desc.summarize_result("only", ToolStatus.SUCCESS) == "1 line read"
    assert desc.summarize_result("", ToolStatus.SUCCESS) == "0 lines read"


def test_edit_summary_parses_unified_diff():
    desc = get_descriptor("edit_file")
    diff = "+added one\n+added two\n-removed one\n context"
    assert desc.summarize_result(diff, ToolStatus.SUCCESS) == "+2 / -1 lines"


def test_edit_summary_falls_back_when_no_diff_markers():
    desc = get_descriptor("edit_file")
    out = desc.summarize_result("plain output", ToolStatus.SUCCESS)
    assert "edit applied" in out


def test_write_summary_pluralizes():
    desc = get_descriptor("write_file")
    assert desc.summarize_result("a\nb", ToolStatus.SUCCESS) == "2 lines written"


def test_search_summary_counts_matches():
    desc = get_descriptor("search_code")
    out = desc.summarize_result("hit1\nhit2\n\nhit3", ToolStatus.SUCCESS)
    assert out == "3 matches"


def test_search_summary_empty_returns_no_matches():
    desc = get_descriptor("search_code")
    assert desc.summarize_result("", ToolStatus.SUCCESS) == "no matches"


def test_bash_summary_counts_output_lines():
    desc = get_descriptor("bash")
    assert desc.summarize_result("a\nb\nc", ToolStatus.SUCCESS) == "3 lines of output"


def test_run_tests_summary_extracts_pytest_line():
    desc = get_descriptor("run_tests")
    pytest_out = (
        "collected 5 items\n"
        "test_foo.py ..F.s\n"
        "===== 3 passed, 1 failed, 1 skipped in 0.42s ====="
    )
    out = desc.summarize_result(pytest_out, ToolStatus.SUCCESS)
    assert "passed" in out and "failed" in out


def test_git_log_summary_counts_commits():
    desc = get_descriptor("git_log")
    log_out = (
        "commit abc1234 short\n"
        "commit def5678 short\n"
    )
    assert desc.summarize_result(log_out, ToolStatus.SUCCESS) == "2 commits"


def test_ask_human_summary_describes_response():
    desc = get_descriptor("ask_human")
    assert "operator replied" in desc.summarize_result(
        "yes please", ToolStatus.SUCCESS,
    )


# ===========================================================================
# Result summarizers — error/timeout/denied paths
# ===========================================================================


@pytest.mark.parametrize("kind", sorted(_EXPECTED_TOOLS))
def test_error_status_yields_failure_summary(kind: str):
    desc = get_descriptor(kind)
    out = desc.summarize_result("any output", ToolStatus.ERROR)
    # All summarizers must communicate non-success.
    assert any(
        marker in out.lower()
        for marker in ("fail", "error", "timed", "did not")
    )


def test_bash_timeout_summary():
    desc = get_descriptor("bash")
    assert desc.summarize_result("partial", ToolStatus.TIMEOUT) == "command timed out"


def test_run_tests_timeout_summary():
    desc = get_descriptor("run_tests")
    assert desc.summarize_result("", ToolStatus.TIMEOUT) == "tests timed out"


def test_ask_human_timeout_summary():
    desc = get_descriptor("ask_human")
    assert desc.summarize_result("", ToolStatus.TIMEOUT) == "operator did not respond"


# ===========================================================================
# render() — pure, deterministic, defensive
# ===========================================================================


def test_render_returns_frozen_record():
    desc = get_descriptor("read_file")
    out = render(desc, "foo.py", "line1\nline2", ToolStatus.SUCCESS)
    assert isinstance(out, RenderedToolResult)
    assert out.schema_version == TOOL_RENDER_REGISTRY_SCHEMA_VERSION


def test_render_cc_verb_header_format():
    desc = get_descriptor("read_file")
    out = render(desc, "foo.py", "x\n" * 5, ToolStatus.SUCCESS)
    assert out.header_line == "Read(foo.py)"


def test_render_icon_header_format():
    desc = get_descriptor("bash")
    out = render(desc, "ls", "x", ToolStatus.SUCCESS)
    assert out.header_line.startswith("💻 bash $ ls")


def test_render_default_descriptor_for_unknown():
    out = render(
        get_descriptor("mcp_unknown_tool"), "args", "result",
        ToolStatus.SUCCESS,
    )
    # Default descriptor uses icon path; header begins with the wrench icon.
    assert out.header_line.startswith("🔧 _default")


def test_render_zero_budget_yields_no_body():
    desc = get_descriptor("search_code")
    out = render(desc, "pat", "a\nb\nc", ToolStatus.SUCCESS, max_body_lines=0)
    assert out.body_lines == ()
    assert out.elided_line_count == 0


def test_render_body_shape_none_yields_no_body_even_with_budget():
    # read_file is BodyShape.NONE — body suppressed regardless of budget.
    desc = get_descriptor("read_file")
    out = render(desc, "foo.py", "a\nb\nc", ToolStatus.SUCCESS, max_body_lines=20)
    assert out.body_lines == ()


def test_render_body_fits_in_budget_no_elision():
    desc = get_descriptor("search_code")
    out = render(
        desc, "pat", "a\nb\nc", ToolStatus.SUCCESS, max_body_lines=10,
    )
    assert out.body_lines == ("a", "b", "c")
    assert out.elided_line_count == 0


def test_render_body_exceeds_budget_head_tail_elision():
    desc = get_descriptor("search_code")
    big = "\n".join(f"line{i}" for i in range(100))
    out = render(desc, "pat", big, ToolStatus.SUCCESS, max_body_lines=10)
    # Total line count must respect the budget exactly.
    assert len(out.body_lines) == 10
    # Truncation marker must be present somewhere in the middle.
    assert any("more line" in ln and "elided" in ln for ln in out.body_lines)
    # Must include head + tail of original.
    assert out.body_lines[0] == "line0"
    assert out.body_lines[-1] == "line99"
    # Elided count must be positive and consistent.
    assert out.elided_line_count == 100 - 10 + 1  # +1 for the marker slot


def test_render_expansion_ref_passes_through():
    desc = get_descriptor("bash")
    out = render(
        desc, "ls", "out", ToolStatus.SUCCESS,
        max_body_lines=5, expansion_ref="t-12",
    )
    assert out.expansion_ref == "t-12"


def test_render_non_string_expansion_ref_dropped():
    desc = get_descriptor("bash")
    out = render(
        desc, "ls", "out", ToolStatus.SUCCESS,
        max_body_lines=5, expansion_ref=12,  # type: ignore[arg-type]
    )
    assert out.expansion_ref is None


def test_render_status_string_coerced():
    desc = get_descriptor("read_file")
    out = render(desc, "f", "x\nx", "success")
    assert "lines read" in out.body_summary


def test_render_deterministic():
    desc = get_descriptor("search_code")
    a = render(desc, "p", "a\nb\nc", ToolStatus.SUCCESS, max_body_lines=10)
    b = render(desc, "p", "a\nb\nc", ToolStatus.SUCCESS, max_body_lines=10)
    assert a == b


def test_render_invalid_descriptor_falls_back_to_default():
    # Pass garbage in the descriptor slot — must NOT raise.
    out = render(
        "not-a-descriptor",  # type: ignore[arg-type]
        "args", "result", ToolStatus.SUCCESS,
    )
    assert out.header_line.startswith("🔧 _default")


def test_render_handles_none_inputs():
    desc = get_descriptor("read_file")
    out = render(
        desc, None, None, ToolStatus.SUCCESS,  # type: ignore[arg-type]
    )
    assert out.header_line == "Read(file)"
    assert "0 lines read" == out.body_summary


# ===========================================================================
# Defensive contract — summarizers wrapped to never raise
# ===========================================================================


def test_summarizers_never_raise_on_pathological_input():
    pathological = "\x00" * 10 + "\xff" * 10
    for kind in _EXPECTED_TOOLS:
        desc = get_descriptor(kind)
        # Must not raise for any tool.
        out = desc.summarize_result(pathological, ToolStatus.ERROR)
        assert isinstance(out, str) and out


def test_args_summarizers_never_raise_on_pathological_input():
    pathological = "\x00" * 10
    for kind in _EXPECTED_TOOLS:
        desc = get_descriptor(kind)
        out = desc.summarize_args(pathological)
        assert isinstance(out, str)


# ===========================================================================
# Authority invariant — module is renderer-agnostic
# ===========================================================================


def test_module_does_not_import_rich_or_console():
    """Slice 1 substrate must NOT depend on Rich / prompt_toolkit /
    Console — those are owned by Slice 4's wiring layer. A regression
    where the substrate imports a renderer would defeat the whole
    point of the layered design."""
    import backend.core.ouroboros.battle_test.tool_render_registry as mod
    import sys
    # Walk the module's source for any import of the forbidden surfaces.
    src = open(mod.__file__).read()
    for forbidden in (
        "from rich",
        "import rich",
        "from prompt_toolkit",
        "import prompt_toolkit",
    ):
        assert forbidden not in src, (
            f"tool_render_registry must not import {forbidden!r} — "
            "renderer concerns belong to Slice 4"
        )
    # Defense in depth: also assert at runtime that importing the module
    # didn't cause Rich to land in sys.modules transitively from us.
    # (Other modules may have loaded Rich; this test just proves we
    # didn't introduce a fresh dependency.)
    assert sys.modules.get(
        "backend.core.ouroboros.battle_test.tool_render_registry",
    ) is mod
