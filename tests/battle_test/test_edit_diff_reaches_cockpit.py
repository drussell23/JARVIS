"""The cockpit shows the change, not a count of it.

A successful `edit_file` rendered:

    ⏺ Update(backend/…/thin_client.py)
      ⎿ edit applied (12 lines affected)

A NUMBER where the change belongs. `show_diff` has rendered numbered green/red
hunks through `_op_line` — the mirrored path — all along; the tool loop simply
never called it. Seventh instance this session of a good renderer the live path
does not reach.

It needs no diff text passed in: after a successful edit the file ON DISK is
the change, so `show_diff` falls back to `_get_git_diff` and reads it. Handing
it the tool's `result_preview` instead would be a second, weaker source for
something git knows exactly.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import tempfile
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import (
    SerpentFlow,
    _extract_path_arg,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


@pytest.fixture()
def git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path,
                   check=False)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path,
                   check=False)
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=False)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=False)
    return tmp_path


def _flow(repo: pathlib.Path) -> Any:
    flow = SerpentFlow.__new__(SerpentFlow)
    flow._repo_path = str(repo)
    return flow


# --------------------------------------------------------------------------
# 1. a change is readable, including one git does not track yet
# --------------------------------------------------------------------------

def test_a_modified_file_yields_its_diff(git_repo: pathlib.Path) -> None:
    (git_repo / "mod.py").write_text("def f():\n    return 2\n")
    diff = _flow(git_repo)._get_git_diff("mod.py")
    assert "-    return 1" in diff and "+    return 2" in diff


def test_a_BRAND_NEW_file_renders_as_additions(git_repo: pathlib.Path) -> None:
    """THE gap: an untracked file produces nothing from every plain `git
    diff` form — and that is exactly what `write_file` creates. Rendering it
    as all-additions is what it is."""
    (git_repo / "brand_new.py").write_text("print('hello')\n")
    diff = _flow(git_repo)._get_git_diff("brand_new.py")
    assert diff, "a new file produced no diff at all"
    assert "+print('hello')" in diff


def test_a_staged_change_is_found(git_repo: pathlib.Path) -> None:
    (git_repo / "mod.py").write_text("def f():\n    return 9\n")
    subprocess.run(["git", "add", "mod.py"], cwd=git_repo, check=False)
    assert "return 9" in _flow(git_repo)._get_git_diff("mod.py")


def test_an_unchanged_file_yields_nothing(git_repo: pathlib.Path) -> None:
    """Empty is correct here — `show_diff` then falls back to its compact
    one-liner, so a missing diff costs a detail, never the line."""
    assert _flow(git_repo)._get_git_diff("mod.py") == ""


def test_an_unchanged_TRACKED_file_is_not_shown_as_new(
    git_repo: pathlib.Path,
) -> None:
    """The bug this test caught: `--no-index` against /dev/null renders ANY
    existing file as all-additions, so an untouched tracked file appeared to
    have just been written. The fallback is gated on the file genuinely being
    untracked."""
    flow = _flow(git_repo)
    assert flow._is_untracked("mod.py") is False
    assert flow._get_git_diff("mod.py") == ""


def test_an_untracked_file_is_recognised(git_repo: pathlib.Path) -> None:
    (git_repo / "fresh.py").write_text("x = 1\n")
    assert _flow(git_repo)._is_untracked("fresh.py") is True


def test_the_untracked_check_is_an_index_lookup() -> None:
    """`ls-files --error-unmatch` reads the index only — no working-tree
    scan, so it stays cheap inside a tool loop that edits repeatedly."""
    import ast

    for node in ast.walk(ast.parse(_SRC.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name == "_is_untracked":
            body = ast.unparse(node)
            assert "ls-files" in body and "--error-unmatch" in body
            return
    pytest.fail("_is_untracked is gone")


def test_a_missing_file_never_raises(git_repo: pathlib.Path) -> None:
    assert isinstance(_flow(git_repo)._get_git_diff("nope.py"), str)


def test_outside_a_git_repo_never_raises(tmp_path: pathlib.Path) -> None:
    (tmp_path / "loose.py").write_text("x = 1\n")
    assert isinstance(_flow(tmp_path)._get_git_diff("loose.py"), str)


def test_the_lookup_is_time_bounded() -> None:
    """Three invocations at 5s each is up to 15 seconds inside a tool loop
    that may edit repeatedly."""
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_get_git_diff":
            body = ast.unparse(node)
            assert "timeout=2" in body, "the per-call timeout was loosened"
            return
    pytest.fail("_get_git_diff is gone")


# --------------------------------------------------------------------------
# 2. the path is found in free-text arguments
# --------------------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    ("backend/core/x.py", "backend/core/x.py"),
    ("path=backend/core/x.py old=foo new=bar", "backend/core/x.py"),
    ('"a/b/c.md"', "a/b/c.md"),
    ("file.py", "file.py"),
])
def test_the_path_is_extracted(args: str, expected: str) -> None:
    """`args_summary` is free text — sometimes a bare path, sometimes
    `path=… old=… new=…`. Finding the path-shaped token is more robust than
    assuming a position in a format the tool layer does not promise."""
    assert _extract_path_arg(args) == expected


@pytest.mark.parametrize("junk", ["", None, 42, "   "])
def test_path_extraction_never_raises(junk: Any) -> None:
    assert isinstance(_extract_path_arg(junk), str)


# --------------------------------------------------------------------------
# 3. the live edit path calls the real renderer
# --------------------------------------------------------------------------

def _tool_result_body() -> str:
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name == "op_tool_call":
            return ast.unparse(node)
    return ""


def test_a_successful_edit_renders_the_DIFF_not_a_count() -> None:
    body = _tool_result_body()
    assert body, "op_tool_call is gone"
    assert "self.show_diff(" in body, "the tool path still bypasses show_diff"
    assert "edit applied (" not in body, "the count line is still being emitted"


def test_write_file_goes_through_the_same_renderer() -> None:
    """A new file is a change too — and the one an operator most wants to
    see, since nothing existed to compare against before."""
    body = _tool_result_body()
    assert '"edit_file", "write_file"' in body or \
        "'edit_file', 'write_file'" in body


def test_no_diff_text_is_threaded_from_the_tool() -> None:
    """The file on disk IS the change. `result_preview` would be a second,
    weaker source for something git knows exactly."""
    body = _tool_result_body()
    assert "show_diff(path or 'file', op_id=op_id)" in body.replace('"', "'")


def test_show_diff_still_reaches_the_cockpit() -> None:
    """The property this whole change rests on: `_op_line` mirrors, a plain
    console print does not."""
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "show_diff":
            assert "_op_line" in ast.unparse(node)
            return
    pytest.fail("show_diff is gone")


def test_a_diffless_change_still_prints_its_header() -> None:
    """Degradation, not disappearance: `show_diff` falls back to a compact
    one-liner when no diff can be read."""
    src = _SRC.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "show_diff":
            body = ast.unparse(node)
            assert "if not diff_text" in body
            return
    pytest.fail("show_diff is gone")
