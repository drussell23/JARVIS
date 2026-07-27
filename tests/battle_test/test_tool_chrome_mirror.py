"""Which file is it reading? The cockpit could not say.

Venom's tool loop has always emitted a full per-call event — `op_id`,
`tool_name`, `args_summary`, `result_preview`, `duration_ms` — through
`ToolNarrationChannel` to CommProtocol. SerpentFlow renders the START of each
call via `_start_status`, a Rich spinner.

A spinner is local-only. It never passes through `_op_line`, the chokepoint
that reaches an attached cockpit, so the tool NAME and its ARGUMENTS rendered
on the daemon's own terminal and nowhere else. An operator saw an op working
with no idea what it was touching:

    ⏺ Explore(find every place we parse a socket path)
      ⎿ 3 files · 2.1s          ← the summary, but never the steps

The spinner stays — it is the right affordance locally. The cockpit gets a
PERSISTENT line, because a remote surface has no spinner to erase and a
vanishing status is worse than none.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import _tool_chrome_line

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


# --------------------------------------------------------------------------
# 1. the argument is the point
# --------------------------------------------------------------------------

def test_the_file_being_read_is_named() -> None:
    """THE request. `⏺ Read()` says an op is busy; `⏺ Read(path)` says what
    it is busy with."""
    out = _tool_chrome_line("read_file", "backend/core/ouroboros/cli/thin_client.py")
    assert out == "⏺ Read(backend/core/ouroboros/cli/thin_client.py)"


def test_a_search_shows_its_pattern() -> None:
    out = _tool_chrome_line("search_code", 'pattern="socket path" glob=*.py')
    assert "socket path" in out and out.startswith("⏺ Search(")


def test_a_command_shows_what_ran() -> None:
    assert "pytest tests/cli -q" in _tool_chrome_line("bash", "pytest tests/cli -q")


@pytest.mark.parametrize("token,verb", [
    ("read_file", "Read"), ("search_code", "Search"), ("edit_file", "Update"),
    ("write_file", "Write"), ("run_tests", "Test"), ("bash", "Bash"),
    ("get_callers", "Callers"), ("ask_human", "Ask"),
])
def test_each_tool_is_SPOKEN_not_tokenised(token: str, verb: str) -> None:
    """`read_file` is a routing identifier; `Read` is what an operator reads."""
    assert _tool_chrome_line(token, "x").startswith(f"⏺ {verb}(")


def test_an_unknown_tool_renders_under_its_own_name() -> None:
    """A new tool must appear, not vanish — MCP tools arrive at runtime and
    cannot be enumerated ahead of time."""
    assert "mcp_github_search" in _tool_chrome_line("mcp_github_search", "q=x")


# --------------------------------------------------------------------------
# 2. long paths keep the part that identifies them
# --------------------------------------------------------------------------

def test_a_deep_path_is_clipped_from_the_LEFT() -> None:
    """The filename identifies the file. Clipping the tail would give every
    entry an identical `backend/core/…` prefix — the same defect as truncating
    a UUIDv7 from the right."""
    deep = "backend/core/ouroboros/governance/" + ("x" * 60) + "/thin_client.py"
    out = _tool_chrome_line("read_file", deep)
    assert "thin_client.py" in out
    assert out.startswith("⏺ Read(…")


def test_a_line_stays_a_line() -> None:
    assert len(_tool_chrome_line("read_file", "y" * 400)) < 80


def test_multiline_args_are_flattened() -> None:
    """A newline inside chrome breaks the ⏺/⎿ pairing visually."""
    assert "\n" not in _tool_chrome_line("bash", "line one\nline two")


def test_a_tool_with_no_arguments_still_renders() -> None:
    assert _tool_chrome_line("list_dir", "") == "⏺ List"


@pytest.mark.parametrize("junk", [None, 42, object()])
def test_it_never_raises(junk: Any) -> None:
    assert isinstance(_tool_chrome_line(junk, junk), str)


# --------------------------------------------------------------------------
# 3. it reaches the cockpit, and the spinner survives
# --------------------------------------------------------------------------

def _start_event_body() -> str:
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                "_start_status(" in (ast.unparse(node) or ""):
            body = ast.unparse(node)
            if "_tool_chrome_line" in body:
                return body
    return ""


def test_the_start_event_goes_through_the_MIRRORED_path() -> None:
    """`_op_line` mirrors to attached cockpits; `_start_status` does not.
    Asserted on the AST rather than a text window — a character slice measures
    how much prose sits between two calls, which is not the invariant."""
    body = _start_event_body()
    assert body, "the tool-start renderer no longer emits chrome"
    assert "_op_line" in body
    assert "_tool_chrome_line" in body


def test_the_local_spinner_is_KEPT() -> None:
    """It is the right affordance locally, where an in-place animation costs
    nothing. Removing it would trade one surface's quality for another's."""
    assert "_start_status" in _start_event_body()


def test_op_line_is_the_mirroring_chokepoint() -> None:
    """The property this whole change depends on."""
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_op_line":
            assert "_mirror_markup" in ast.unparse(node)
            return
    pytest.fail("_op_line is gone")
