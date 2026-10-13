"""Numbered source in, copied numbers out.

The held-out baseline (run-20261010-071537) failed 28/30 tasks on hunks whose
context lines occur nowhere in the file: the model was shown ~60 KB of
unnumbered source and rebuilt the region from memory. Snapshots now carry
their positions (``source_lines``); these pin that the model can copy them
back into a diff or a whole file without breaking the patch.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import source_lines as sl
from backend.core.ouroboros.governance import providers as pv

BAR = sl.SEPARATOR
SRC = "".join(f"line {i}\n" for i in range(1, 41)).replace("line 20\n", "\n")


def _numbered_diff(start: int, ctx_before: int = 3) -> str:
    """A hunk replacing line *start* whose context and removal lines carry
    the numbers the snapshot showed, and whose header is wrong."""
    rows = [f" {sl.number_lines([f'line {n}'], n)[0]}" for n in range(start - ctx_before, start)]
    rows.append(f"-{sl.number_lines([f'line {start}'], start)[0]}")
    rows.append("+line X")
    rows.append(f" {sl.number_lines([f'line {start + 1}'], start + 1)[0]}")
    return "@@ -1,5 +1,5 @@\n" + "\n".join(rows) + "\n"


def test_number_and_strip_are_inverse_and_keep_the_line_exactly() -> None:
    lines = ["def f():", "    return 1", "", "x = '1│not a prefix'"]
    shown = sl.number_lines(lines, 98)
    assert shown[0] == f" 98{BAR}def f():" and shown[-1].startswith(f"101{BAR}")
    back, first, n = sl.strip_echoed(shown, min_share=sl.hunk_share())
    assert back == lines and first == 98 and n == 4


def test_a_numbered_hunk_applies_and_its_numbers_place_it() -> None:
    patched = pv._apply_unified_diff(SRC, _numbered_diff(30))
    assert "line X\n" in patched and "line 30\n" not in patched
    assert patched.count("\n") == SRC.count("\n")
    pv.validate_diff_context(SRC, _numbered_diff(30))           # the pre-check agrees


def test_the_copied_number_replaces_a_wrong_header() -> None:
    [(start, ops)] = pv._parse_unified_hunks(_numbered_diff(30))
    assert start == 26, "first context line 27 is the hunk's real start (0-indexed 26)"
    assert all(BAR not in t for _k, t in ops)


def test_a_copied_line_without_its_diff_marker_is_still_context() -> None:
    diff = ("@@ -1,3 +1,3 @@\n"
            f"{sl.number_lines(['line 5'], 5)[0]}\n"
            f"-{sl.number_lines(['line 6'], 6)[0]}\n"
            "+line six\n"
            f" {sl.number_lines(['line 7'], 7)[0]}\n")
    assert "line six\nline 7\n" in pv._apply_unified_diff(SRC, diff)


def test_a_numbered_blank_context_line_is_blank() -> None:
    diff = ("@@ -18,4 +18,4 @@\n"
            f" {sl.number_lines(['line 19'], 19)[0]}\n"
            f" {sl.number_lines([''], 20)[0]}\n"
            f"-{sl.number_lines(['line 21'], 21)[0]}\n"
            "+line 21b\n")
    assert "line 19\n\nline 21b\n" in pv._apply_unified_diff(SRC, diff)


def test_a_lone_source_line_shaped_like_a_number_is_not_touched() -> None:
    """Below the share, nothing is stripped: the line is the file's own text."""
    src = "a = 1\n12" + BAR + "box\nb = 2\n"
    diff = "@@ -1,3 +1,3 @@\n a = 1\n 12" + BAR + "box\n-b = 2\n+b = 3\n"
    assert pv._apply_unified_diff(src, diff) == "a = 1\n12" + BAR + "box\nb = 3\n"


def test_a_whole_file_copied_from_a_numbered_snapshot_loses_the_numbers() -> None:
    body = "import os\n\n\ndef f():\n    return os.sep\n"
    shown = sl.number(body)
    assert sl.strip_echoed_content(shown) == body
    assert sl.strip_echoed_content(body) == body
    # a file that merely contains a few such lines is left alone
    mixed = body + "\n".join(f"{n}{BAR}x" for n in range(2)) + "\n"
    assert sl.strip_echoed_content(mixed) == mixed


def test_a_truncated_snapshot_keeps_real_positions_after_the_marker(tmp_path: Path) -> None:
    f = tmp_path / "big.py"
    f.write_text("".join(f"v{i} = {i}\n" for i in range(1, 2001)))
    out = pv._read_with_truncation(f, max_chars=4000, head_chars=1000, tail_chars=1000, numbered=True)
    head, tail = out.split("[TRUNCATED:")
    assert head.startswith(f"   1{BAR}v1 = 1\n")
    last = tail.rstrip("\n").splitlines()[-1]
    assert last == f"2000{BAR}v2000 = 2000"
    plain = pv._read_with_truncation(f, max_chars=4000, head_chars=1000, tail_chars=1000)
    assert BAR not in plain, "numbering is opt-in at this seam"


def test_the_diff_schema_explains_the_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_NUMBERED_SOURCE_ENABLED", "true")
    assert sl.legend() in pv.diff_schema_instruction("abc")
    monkeypatch.setenv("JARVIS_NUMBERED_SOURCE_ENABLED", "false")
    assert sl.legend() not in pv.diff_schema_instruction("abc")


def test_every_snippet_the_model_is_shown_uses_the_one_format(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance import diff_healer, op_context_cache
    src = "".join(f"x{i} = {i}\n" for i in range(1, 60))
    assert f"30{BAR}x30 = 30" in diff_healer._region(src, "file line 30 is 'y'", 5)
    (tmp_path / "m.py").write_text(src)
    assert f"30{BAR}x30 = 30" in op_context_cache._pinned_region(tmp_path, "m.py", 30)
    region = pv._extract_target_region(src, "change line 30")
    assert f"30{BAR}x30 = 30" in region


def test_read_file_numbers_in_the_one_format(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.tool_executor import ToolExecutor
    (tmp_path / "m.py").write_text("a = 1\nb = 2\n")
    out = ToolExecutor(repo_root=tmp_path)._read_file({"path": "m.py"})
    assert out.splitlines()[1:] == [f"1{BAR}a = 1", f"2{BAR}b = 2"]


def test_a_micro_fix_reply_copied_from_the_window_is_code_only() -> None:
    from backend.core.ouroboros.governance.interactive_repair import InteractiveRepairLoop, MicroFix
    fix = MicroFix(target_file="m.py", line_range=(2, 2),
                   replacement=f"2{BAR}b = 3\n", reasoning="")
    assert InteractiveRepairLoop._apply_fix("a = 1\nb = 2\nc = 3", fix) == "a = 1\nb = 3\nc = 3"


def test_the_healer_does_not_read_a_copied_number_as_a_new_intent() -> None:
    from backend.core.ouroboros.governance import diff_healer
    plain = "@@ -2,2 +2,2 @@\n a\n-b\n+c\n"
    numbered = f"@@ -2,2 +2,2 @@\n 2{BAR}a\n-3{BAR}b\n+c\n"
    assert diff_healer.intent_preserved(plain, numbered)
