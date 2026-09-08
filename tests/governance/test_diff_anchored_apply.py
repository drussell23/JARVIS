"""Anchored unified-diff application: the hunk's context is a LOCATOR, not a
verbatim requirement.

The served 30B's diff for the tracer goal (soak 2026-09-08 00:11Z) carried one
phantom blank context line in its second hunk; the equal-length verbatim and
whitespace-stripped matchers refused an otherwise exact edit, the error named
two lines that HAD matched, the retry produced the identical diff and the
forward-progress guard tripped. Non-blank lines locate the edit; blank lines
never do; the file's own context text is what gets written.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.providers import (
    StaleDiffError,
    _align_hunk,
    _apply_unified_diff,
    _diff_fuzzy_window,
    _parse_unified_hunks,
    validate_diff_context,
)

FILE = (
    "def _tracer_timeout_s() -> float:\n"
    "    try:\n"
    "        return max(5.0, float(os.environ.get(\"T\", \"90\")))\n"
    "    except (TypeError, ValueError):\n"
    "        return 90.0\n"
    "\n"
    "\n"
    "async def trace_direct_completion(provider, *, model=None) -> str:\n"
    "    if not tracer_enabled():\n"
    "        return \"skipped\"\n"
    "    if provider is None or not hasattr(provider, \"complete_sync\"):\n"
    "        return \"skipped\"\n"
    "    try:\n"
    "        res = await asyncio.wait_for(provider.complete_sync())\n"
    "    except Exception:\n"
    "        return \"failed\"\n"
)

# hunk 2 of the live diff: a blank context line the file does NOT have
# (between `return "skipped"` and `try:`), stated 1 line off.
PHANTOM_BLANK = (
    "@@ -10,7 +10,12 @@\n"
    "     if not tracer_enabled():\n"
    "         return \"skipped\"\n"
    "     if provider is None or not hasattr(provider, \"complete_sync\"):\n"
    "         return \"skipped\"\n"
    " \n"
    "+    # Auth back-off guard\n"
    "+    if _auth_backoff_active():\n"
    "+        return \"auth_backoff\"\n"
    "+\n"
    "     try:\n"
    "         res = await asyncio.wait_for(provider.complete_sync())\n"
)


def test_a_phantom_blank_context_line_no_longer_refuses_the_edit():
    out = _apply_unified_diff(FILE, PHANTOM_BLANK)
    lines = out.splitlines()
    i = lines.index("        return \"skipped\"", 10)
    assert lines[i + 1] == "    # Auth back-off guard"
    assert lines[i + 3] == "        return \"auth_backoff\""
    assert lines[i + 4] == "" and lines[i + 5] == "    try:"
    assert out.count("try:") == 2 and "return 90.0" in out, "the rest of the file is untouched"


def test_the_pre_check_and_the_apply_agree():
    validate_diff_context(FILE, PHANTOM_BLANK)  # must not raise


def test_an_omitted_blank_line_keeps_the_files_own_spacing():
    diff = (
        "@@ -4,4 +4,5 @@\n"
        "     except (TypeError, ValueError):\n"
        "         return 90.0\n"
        "+# marker\n"
        " async def trace_direct_completion(provider, *, model=None) -> str:\n"
    )
    out = _apply_unified_diff(FILE, diff)
    assert "        return 90.0\n# marker\n\n\nasync def trace_direct_completion" in out


def test_a_removal_is_anchored_too():
    diff = (
        "@@ -9,4 +9,3 @@\n"
        "     if not tracer_enabled():\n"
        "         return \"skipped\"\n"
        "-    if provider is None or not hasattr(provider, \"complete_sync\"):\n"
        "-        return \"skipped\"\n"
        " \n"
        "     try:\n"
    )
    out = _apply_unified_diff(FILE, diff)
    assert "complete_sync\")" not in out and out.count("return \"skipped\"") == 1


def test_a_context_line_the_file_lacks_is_named_in_the_error():
    diff = (
        "@@ -9,3 +9,4 @@\n"
        "     if not tracer_enabled():\n"
        "         return \"skipped\"\n"
        "     if provider is None or provider.is_dead():\n"
        "+    x = 1\n"
    )
    with pytest.raises(ValueError, match="does not match source") as ei:
        _apply_unified_diff(FILE, diff)
    msg = str(ei.value)
    assert "after 2 matching line(s)" in msg and "provider.is_dead()" in msg
    with pytest.raises(StaleDiffError) as se:
        validate_diff_context(FILE, diff)
    assert se.value.hunk_line == 9 and "provider.is_dead()" in str(se.value)


def test_the_window_is_one_accessor_for_both(monkeypatch):
    far = "@@ -40,2 +40,3 @@\n     try:\n+    y = 2\n         res = await asyncio.wait_for(provider.complete_sync())\n"
    with pytest.raises(ValueError, match="not found within"):
        _apply_unified_diff(FILE, far)
    monkeypatch.setenv("OUROBOROS_DIFF_FUZZY_WINDOW", "40")
    assert _diff_fuzzy_window() == 40
    assert "    y = 2\n" in _apply_unified_diff(FILE, far)
    validate_diff_context(FILE, far)


def test_a_bare_empty_line_is_a_blank_context_line():
    hunks = _parse_unified_hunks("@@ -1,3 +1,3 @@\n a\n\n b\n")
    assert hunks == [(0, [(" ", "a\n"), (" ", "\n"), (" ", "b\n")])]


def test_align_never_places_a_hunk_without_locators():
    assert _align_hunk(FILE.splitlines(keepends=True), 3, [("+", "x\n"), (" ", "\n")], 15) is None
