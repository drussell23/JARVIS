"""Stateful Tool Masking — the schema-compiler (prompt) mask + in-loop levers.

The Iron Gate rejects a GENERATE candidate emitted with too few exploration tool
calls POST-hoc, costing a whole GENERATE_RETRY. Stateful Tool Masking makes that
premature-patch state unrepresentable IN-LOOP: while below the exploration floor,
the write-tool advertisement is stripped from the prompt (Progressive Schema
Injection) and a premature final candidate is rejected + re-prompted for
exploration (the load-bearing lever, exercised live).

These tests pin the pure "schema compiler" (masked_prompt): the initial compile
strictly OMITS mutation tools, and flipping the exploration state flag (crossing
the floor) RESTORES them.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import tool_masking as tm


# A prompt shaped like providers._build_tool_section output: a read-tools block,
# the write-tools block (Iron-Gate header), then a trailing section.
_PROMPT = (
    "**Read/navigate tools:**\n"
    "- `read_file(path)` — read a file.\n"
    "- `search_code(query)` — ripgrep the repo.\n\n"
    "**Write tools (Iron-Gate-governed, env: JARVIS_TOOL_EDIT_ALLOWED=true):**\n"
    "- `edit_file(path, old_text, new_text)` — surgical find-and-replace.\n"
    "- `write_file(path, content)` — create or overwrite.\n"
    "- `delete_file(path)` — remove a file.\n\n"
    "**External MCP tools:**\n"
    "- `mcp_foo(x)` — external.\n\n"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("JARVIS_TOOL_MASKING_ENABLED", "JARVIS_TOOL_MASK_REJECT_CAP"):
        monkeypatch.delenv(v, raising=False)
    yield


# ===========================================================================
# A. masked_prompt — the schema compiler (mandate 4 core assertion)
# ===========================================================================


def test_initial_compile_omits_mutation_tools():
    """Below the exploration floor → the write-tools block is stripped; the
    read/navigate + MCP sections survive intact."""
    out = tm.masked_prompt(_PROMPT, explore_count=0, floor=2)
    assert "edit_file" not in out
    assert "write_file" not in out
    assert "delete_file" not in out
    assert "**Write tools" not in out
    # ...read + downstream sections preserved.
    assert "read_file(path)" in out
    assert "search_code(query)" in out
    assert "External MCP tools" in out and "mcp_foo" in out


def test_flag_flip_restores_mutation_tools():
    """At/above the floor (state flag flipped) → the prompt is unchanged: mutation
    tools are restored verbatim."""
    out = tm.masked_prompt(_PROMPT, explore_count=2, floor=2)
    assert out == _PROMPT
    assert "edit_file" in out and "write_file" in out and "**Write tools" in out


def test_partial_exploration_still_masked():
    # 1 of 2 required calls → still below floor → still masked.
    out = tm.masked_prompt(_PROMPT, explore_count=1, floor=2)
    assert "write_file" not in out


def test_masking_disabled_is_passthrough(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_MASKING_ENABLED", "false")
    out = tm.masked_prompt(_PROMPT, explore_count=0, floor=2)
    assert out == _PROMPT                      # unchanged — legacy behavior


def test_masked_prompt_never_raises_on_garbage():
    assert tm.masked_prompt(_PROMPT, explore_count="x", floor="y") == _PROMPT  # type: ignore[arg-type]


# ===========================================================================
# B. strip_write_tools_prose — the marker-based, fail-open surgery
# ===========================================================================


def test_strip_removes_only_the_write_block():
    out = tm.strip_write_tools_prose(_PROMPT)
    assert "**Write tools" not in out and "edit_file" not in out
    assert "read_file(path)" in out and "mcp_foo" in out


def test_strip_failopen_when_marker_absent():
    p = "no write tools here\n\njust read tools\n\n"
    assert tm.strip_write_tools_prose(p) == p     # unchanged


def test_strip_failopen_on_empty():
    assert tm.strip_write_tools_prose("") == ""
    assert tm.strip_write_tools_prose(None) is None  # type: ignore[arg-type]


def test_strip_failopen_when_block_runs_to_eof():
    # header present but no terminating blank line → don't risk corrupting.
    p = "x\n\n**Write tools (Iron-Gate-governed):**\n- edit_file"
    assert tm.strip_write_tools_prose(p) == p


# ===========================================================================
# C. knobs + notice
# ===========================================================================


def test_enabled_default_on():
    assert tm.tool_masking_enabled() is True


def test_reject_cap_default_and_override(monkeypatch):
    assert tm.tool_mask_reject_cap() == 2
    monkeypatch.setenv("JARVIS_TOOL_MASK_REJECT_CAP", "5")
    assert tm.tool_mask_reject_cap() == 5
    monkeypatch.setenv("JARVIS_TOOL_MASK_REJECT_CAP", "garbage")
    assert tm.tool_mask_reject_cap() == 2


def test_force_notice_steers_to_exploration():
    note = tm.exploration_force_notice(0, 2)
    assert "read_file" in note and "search_code" in note
    assert "REJECTED" in note and "0/2" in note
