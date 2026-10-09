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

The spinner stays — it is the right affordance locally. The cockpit first got
a PERSISTENT line; since 2026-10-09 it gets the running call on the LIVE
channel instead (`tool_inflight` → heartbeat verb), because the completed
header is persistent too and a tool must not read twice in scrollback.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import tool_call_label

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


# --------------------------------------------------------------------------
# 1. the argument is the point
# --------------------------------------------------------------------------

def test_the_file_being_read_is_named() -> None:
    """THE request. `Read()` says an op is busy; `Read(path)` says what it
    is busy with."""
    out = tool_call_label("read_file", "backend/core/ouroboros/cli/thin_client.py")
    assert out == "Read(backend/core/ouroboros/cli/thin_client.py)"


def test_a_search_shows_its_pattern() -> None:
    out = tool_call_label("search_code", 'pattern="socket path" glob=*.py')
    assert "socket path" in out and out.startswith("Search(")


def test_a_command_shows_what_ran() -> None:
    assert "pytest tests/cli -q" in tool_call_label("bash", "pytest tests/cli -q")


@pytest.mark.parametrize("token,verb", [
    ("read_file", "Read"), ("search_code", "Search"), ("edit_file", "Update"),
    ("write_file", "Write"), ("run_tests", "Test"), ("bash", "Bash"),
    ("get_callers", "Callers"), ("ask_human", "Ask"),
])
def test_each_tool_is_SPOKEN_not_tokenised(token: str, verb: str) -> None:
    """`read_file` is a routing identifier; `Read` is what an operator reads."""
    assert tool_call_label(token, "x").startswith(f"{verb}(")


def test_an_unknown_tool_renders_under_its_own_name() -> None:
    """SUPERSEDED same-day: this asserted the RAW token survived, which was
    the weaker behaviour it was describing. An MCP tool now renders as
    `server·tool`, so the invariant that matters — a new tool APPEARS rather
    than vanishing — is expressed against identity instead of against the
    routing string.

    A new tool must appear, not vanish: MCP tools arrive at runtime and
    cannot be enumerated ahead of time."""
    out = tool_call_label("mcp_github_search", "q=x")
    assert "github" in out and "search" in out
    assert "mcp_" not in out, "the raw routing prefix leaked to the operator"


# --------------------------------------------------------------------------
# 2. long paths keep the part that identifies them
# --------------------------------------------------------------------------

def test_a_deep_path_keeps_BOTH_ends() -> None:
    """SUPERSEDED same-day: this required the line to START with `…`, which
    pinned a clip that threw the repo away. Keeping only the tail loses which
    project the file is in; keeping only the head loses the file. Whole
    segments are elided from the MIDDLE now, so both ends survive.

    The filename still identifies the file — that part was right."""
    deep = "backend/core/ouroboros/governance/" + ("x" * 60) + "/thin_client.py"
    out = tool_call_label("read_file", deep)
    assert "thin_client.py" in out
    assert "backend" in out, "the repo root was thrown away"
    assert "…" in out


def test_a_line_stays_a_line() -> None:
    assert len(tool_call_label("read_file", "y" * 400)) < 80


def test_multiline_args_are_flattened() -> None:
    """A newline inside chrome breaks the ⏺/⎿ pairing visually."""
    assert "\n" not in tool_call_label("bash", "line one\nline two")


def test_a_tool_with_no_arguments_still_renders() -> None:
    assert tool_call_label("list_dir", "") == "List"


@pytest.mark.parametrize("junk", [None, 42, object()])
def test_it_never_raises(junk: Any) -> None:
    assert isinstance(tool_call_label(junk, junk), str)


# --------------------------------------------------------------------------
# 3. it reaches the cockpit, and the spinner survives
# --------------------------------------------------------------------------

def _method_body(name: str) -> str:
    for node in ast.walk(ast.parse(_SRC.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == name:
            return ast.unparse(node)
    return ""


def _start_event_body() -> str:
    return _method_body("op_tool_start")


def test_the_start_event_goes_to_the_LIVE_channel() -> None:
    """SUPERSEDED 2026-10-09: this asserted the start went through `_op_line`,
    a PERSISTENT transcript line. The completed header in `op_tool_call` is
    persistent too, so every tool read twice in scrollback. A start is live
    state, so it is recorded for the heartbeat, which rewrites itself."""
    body = _start_event_body()
    assert "get_inflight_tools().started" in body
    assert "tool_call_label" in body


def test_the_start_event_writes_NOTHING_to_the_transcript() -> None:
    """The duplicate, pinned shut. Any `_op_line` call left in the start
    handler that carries the call label would bring it back."""
    tree = ast.parse(_start_event_body())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_op_line":
            assert "tool_call_label" not in ast.unparse(node)


def test_completion_retires_the_live_label_and_op_close_clears_it() -> None:
    assert "get_inflight_tools().finished" in _method_body("op_tool_call")
    assert "get_inflight_tools().clear" in _method_body("_close_op_block")


def test_the_heartbeat_names_the_running_tool(monkeypatch) -> None:
    """End to end over the real composer: a running call outranks the phase
    verb, and the phase verb comes back the moment it finishes."""
    from backend.core.ouroboros.battle_test import attach_heartbeat, status_line
    from backend.core.ouroboros.battle_test.tool_inflight import get_inflight_tools

    class _Builder:
        def snapshot(self):
            return status_line.StatusSnapshot(phase="GENERATE",
                                              primary_op_id="op-live")

    monkeypatch.setattr(status_line, "get_status_line_builder", lambda: _Builder())
    tools = get_inflight_tools()
    label = tool_call_label("read_file", "backend/a.py")
    tools.started("op-live", label)
    try:
        payload = attach_heartbeat.build_heartbeat_payload()
        assert payload["verb"] == "Read(backend/a.py)" and payload["active"]
        tools.finished("op-live", label)
        assert attach_heartbeat.build_heartbeat_payload()["verb"] != label
    finally:
        tools.clear("op-live")


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


# --------------------------------------------------------------------------
# 4. verbs are DERIVED, not enumerated  (2026-07-27)
# --------------------------------------------------------------------------

from backend.core.ouroboros.battle_test.serpent_flow import (  # noqa: E402
    derive_verb,
    elide_path,
)


def test_an_mcp_tool_renders_as_server_and_tool() -> None:
    """MCP tools arrive at RUNTIME — an operator can connect a server this
    afternoon — so a fixed table cannot cover them, and a lookup miss must
    not render a raw routing token."""
    assert derive_verb("mcp_github_search_issues") == "github·search_issues"


def test_a_known_server_list_resolves_an_ambiguous_split() -> None:
    """Server names may contain underscores, so `mcp_my_server_run_query` is
    ambiguous without the connection list. Longest match wins — the same rule
    the client's own dispatcher uses."""
    assert derive_verb("mcp_my_server_run_query", ["my_server"]) == \
        "my_server·run_query"


def test_an_unknown_server_still_renders_honestly() -> None:
    """A slightly mis-split label beats a raw `mcp_github_search_issues`."""
    out = derive_verb("mcp_unknown_thing")
    assert "·" in out and "mcp_" not in out


def test_a_brand_new_builtin_is_title_cased_not_tokenised() -> None:
    """No entry needed: derivation covers it."""
    assert derive_verb("some_brand_new_tool") == "SomeBrandNewTool"


def test_the_override_table_is_only_for_wrong_derivations() -> None:
    """`edit_file` would derive to "EditFile"; operators read "Update". The
    table is an override list, not a registry."""
    assert derive_verb("edit_file") == "Update"
    assert derive_verb("read_file") == "Read"


@pytest.mark.parametrize("junk", ["", None, 42])
def test_verb_derivation_never_returns_empty(junk: Any) -> None:
    assert derive_verb(junk)


# --------------------------------------------------------------------------
# 5. paths keep BOTH ends; commands keep their head
# --------------------------------------------------------------------------

def test_a_long_path_elides_WHOLE_SEGMENTS() -> None:
    """A mid-word cut yields `…xxxxx/deep_module.py` — the same defect as
    truncating a UUIDv7 from the wrong end, on the other axis."""
    out = elide_path("backend/core/ouroboros/governance/chat_repl_dispatcher.py")
    assert out.startswith("backend/")
    assert out.endswith("chat_repl_dispatcher.py")
    assert "…" in out
    for segment in out.split("/"):
        assert segment == "…" or "…" not in segment, "a segment was cut in half"


def test_elision_grows_back_toward_the_FILENAME() -> None:
    """The segments nearest the file carry the most meaning: `governance/`
    locates it, `core/` barely narrows anything. Free context should buy the
    useful end."""
    out = elide_path("backend/core/ouroboros/governance/chat_repl_dispatcher.py")
    assert "governance" in out


def test_a_path_that_fits_is_left_alone() -> None:
    short = "backend/core/ouroboros/battle_test/serpent_flow.py"
    assert elide_path(short) == short


def test_a_single_enormous_filename_keeps_its_tail() -> None:
    """Nothing to elide between — the extension and suffix still identify
    it."""
    out = elide_path("one_enormous_single_segment_filename_with_no_dirs.py" * 2)
    assert out.endswith(".py") and out.startswith("…")


def test_a_command_is_clipped_from_the_RIGHT() -> None:
    """A shell command is identified by its head. Eliding `pytest` to keep
    `--no-header` would be exactly backwards."""
    out = tool_call_label(
        "bash", "pytest tests/cli/test_thin_client.py -q --tb=short -x --no-header",
    )
    assert "pytest" in out
    assert out.rstrip(")").endswith("…")


def test_a_command_containing_a_slash_is_not_mistaken_for_a_path() -> None:
    """`pytest tests/cli -q` has a slash but is not a path — the
    discriminator is whether the FIRST token is itself the path."""
    out = tool_call_label("bash", "pytest " + "tests/cli/x " * 20)
    assert out.startswith("Bash(pytest")


def test_a_bare_long_path_IS_treated_as_one() -> None:
    out = tool_call_label("read_file", "backend/core/ouroboros/" + "d/" * 20 + "f.py")
    assert out.endswith("f.py)")
