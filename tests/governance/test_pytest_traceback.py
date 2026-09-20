"""The traceback engine locates a failure, or refuses to.

The micro-fix was dying on ``UnknownError line=0`` with a five-branch pytest
parser already in place, so pytest literacy was never the missing piece.
Running the real subprocess found four faults, and these tests pin each one:

* ``pytest.ini`` forces ``--color=yes``, so ANSI escapes sit in front of every
  line anchor the cascade matched on. A bare-temp-dir reproduction hides this
  exactly, which is why the fault survived a parser written specifically to
  cure it.
* the short-summary branch answered ``line_number=1`` when it had no line,
  and the hard guard only rejects ``<= 0``, so the placeholder passed it.
* frames were taken from the end of the stack without asking who owned them,
  so failures resolved into ``pathlib.py`` and ``_pytest/python.py``.
* an unscoped run made the first ambient failure in the repo the thing under
  repair.

The engine's contract is narrow on purpose: a location comes back only when
it is a line this repository controls and that line exists. Everything else
is ``None``, and ``None`` means drop the attempt.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.pytest_traceback import (
    Frame,
    is_vendored,
    parse_error,
    parse_failure,
    parse_frames,
    resolve_owned_frame,
    strip_ansi,
)


# ---------------------------------------------------------------------------
# ANSI — the fault that made a correct parser look illiterate
# ---------------------------------------------------------------------------


def test_strip_ansi_removes_pytest_colour():
    raw = "\x1b[1m\x1b[31mtests/x.py\x1b[0m:12: in test_y"
    assert strip_ansi(raw) == "tests/x.py:12: in test_y"


def test_frames_parse_through_colour():
    """The exact shape ``--color=yes`` produces. Without stripping, the
    line anchors never fire and every failure reads as unattributable."""
    raw = (
        "\x1b[1m\x1b[31mtests/x.py\x1b[0m:12: in test_y\n"
        "    assert f() == 1\n"
        "\x1b[1m\x1b[31mE   ValueError: boom\x1b[0m\n"
    )
    frames = parse_frames(raw)
    assert [(f.file_path, f.line_number) for f in frames] == [("tests/x.py", 12)]
    assert parse_error(raw) == ("ValueError", "boom")


def test_strip_ansi_never_raises_on_junk():
    assert strip_ansi(None) == ""
    assert strip_ansi("") == ""


# ---------------------------------------------------------------------------
# Frame parsing — both dialects, order preserved
# ---------------------------------------------------------------------------


def test_parses_both_dialects_in_printed_order():
    out = (
        "tests/a.py:1: in test_a\n"
        '  File "/usr/lib/python3.11/pathlib.py", line 1058, in read_text\n'
        "tests/b.py:2: in helper\n"
    )
    assert [(f.file_path, f.line_number) for f in parse_frames(out)] == [
        ("tests/a.py", 1),
        ("/usr/lib/python3.11/pathlib.py", 1058),
        ("tests/b.py", 2),
    ]


def test_error_text_is_not_mistaken_for_a_frame():
    """``E   AssertionError: tests/x.py:3: nope`` is a message, not a frame."""
    out = "E   AssertionError: tests/x.py:3: nope\n"
    assert parse_frames(out) == ()


def test_collection_syntax_error_frame_is_found():
    """pytest prefixes this frame with ``E``; requiring a bare line start
    dropped the single most repairable failure there is."""
    out = (
        'E     File "/repo/tests/x.py", line 1\n'
        "E       def test_x(:\n"
        "E   SyntaxError: invalid syntax\n"
    )
    assert [(f.file_path, f.line_number) for f in parse_frames(out)] == [
        ("/repo/tests/x.py", 1),
    ]
    assert parse_error(out) == ("SyntaxError", "invalid syntax")


def test_bare_assertion_is_typed():
    """pytest names no class for a plain assert, so the prompt would
    otherwise be told a test failed without being told what it asserted."""
    assert parse_error("E       assert 1 == 2\n") == ("AssertionError", "assert 1 == 2")


def test_last_error_wins():
    """``-x`` stops on the final error; a summary may repeat earlier ones."""
    out = "E   ValueError: first\nE   KeyError: second\n"
    assert parse_error(out)[0] == "KeyError"


# ---------------------------------------------------------------------------
# Ownership — the walk backwards
# ---------------------------------------------------------------------------


def test_vendored_detection_is_segment_wise():
    assert is_vendored("/x/.venv/lib/python3.11/site-packages/_pytest/python.py")
    assert is_vendored("/usr/lib/python3.11/pathlib.py")
    assert is_vendored("/x/node_modules/y/z.py")
    # A repo may legitimately contain a directory whose NAME merely
    # contains a vendored token; substring matching would misjudge it.
    assert not is_vendored("backend/venv_tools/helper.py")
    assert not is_vendored("tests/test_x.py")


def test_walk_stops_at_innermost_repo_frame(tmp_path):
    """A stdlib frame is where the exception surfaced, not our line."""
    frames = (
        Frame("tests/x.py", 4, "test_x"),
        Frame("/usr/lib/python3.11/pathlib.py", 1044, "open"),
        Frame("/usr/lib/python3.11/pathlib.py", 1058, "read_text"),
    )
    frame, why = resolve_owned_frame(frames, repo_root=tmp_path)
    assert frame is not None
    assert (frame.file_path, frame.line_number) == ("tests/x.py", 4)
    assert why == "repo"


def test_candidate_frame_outranks_a_deeper_repo_frame(tmp_path):
    """A repo-owned conftest or helper is still not the file the op was
    sanctioned to edit."""
    frames = (
        Frame("tests/test_mine.py", 10, "test_x"),
        Frame("tests/conftest.py", 99, "fixture"),
    )
    frame, why = resolve_owned_frame(
        frames, repo_root=tmp_path, preferred_paths=("tests/test_mine.py",),
    )
    assert frame is not None
    assert frame.file_path == "tests/test_mine.py"
    assert why == "candidate"


def test_no_repo_frame_is_a_refusal_not_a_guess(tmp_path):
    frames = (
        Frame("/usr/lib/python3.11/pathlib.py", 1058, "read_text"),
        Frame("/x/site-packages/_pytest/python.py", 507, "_call"),
    )
    frame, why = resolve_owned_frame(frames, repo_root=tmp_path)
    assert frame is None
    assert why == "no_repo_owned_frame"


def test_no_frames_at_all_is_a_refusal(tmp_path):
    assert resolve_owned_frame((), repo_root=tmp_path) == (None, "no_frames")


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locates_a_verified_line(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text(
        "def test_x():\n    assert 1 == 2\n",
    )
    out = (
        "\x1b[1mtests/x.py\x1b[0m:2: in test_x\n"
        "E   AssertionError: assert 1 == 2\n"
    )
    parsed = await parse_failure(
        out, repo_root=tmp_path, preferred_paths=("tests/x.py",),
    )
    assert parsed is not None
    assert (parsed.file_path, parsed.line_number) == ("tests/x.py", 2)
    assert parsed.error_type == "AssertionError"
    assert parsed.resolution == "candidate"


@pytest.mark.asyncio
async def test_line_past_end_of_file_is_dropped(tmp_path):
    """The loop rewrites the file between iterations, so a traceback can
    outlive the text it describes. A patch aimed past the end lands on
    nothing, or on whatever moved into that position."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("def test_x():\n    pass\n")
    out = "tests/x.py:9999: in test_x\nE   AssertionError: stale\n"
    assert await parse_failure(out, repo_root=tmp_path) is None


@pytest.mark.asyncio
async def test_stdlib_only_failure_is_dropped(tmp_path):
    out = (
        '  File "/usr/lib/python3.11/pathlib.py", line 1058, in read_text\n'
        "E   FileNotFoundError: nope\n"
    )
    assert await parse_failure(out, repo_root=tmp_path) is None


@pytest.mark.asyncio
async def test_unparseable_output_is_dropped(tmp_path):
    assert await parse_failure(
        "random noise, no frames here", repo_root=tmp_path,
    ) is None


@pytest.mark.asyncio
async def test_unparseable_file_still_yields_its_line(tmp_path):
    """A SyntaxError candidate cannot be AST-verified and is exactly what
    the micro-fix should be fixing, so the location is kept."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("def test_x(:\n    pass\n")
    out = (
        'E     File "tests/x.py", line 1\n'
        "E   SyntaxError: invalid syntax\n"
    )
    parsed = await parse_failure(
        out, repo_root=tmp_path, preferred_paths=("tests/x.py",),
    )
    assert parsed is not None
    assert parsed.line_number == 1
    assert parsed.error_type == "SyntaxError"


@pytest.mark.asyncio
async def test_missing_file_is_dropped(tmp_path):
    out = "tests/gone.py:1: in test_x\nE   AssertionError: x\n"
    assert await parse_failure(out, repo_root=tmp_path) is None
