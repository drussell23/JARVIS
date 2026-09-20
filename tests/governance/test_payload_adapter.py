"""Meet the provider's cage; do not argue with it.

APPLY does not want a diff -- ``ChangeRequest.proposed_content`` is "the new
content to write to the file", and the provider is caged to emit full
content. Those already agree, so nothing is adapted between them.

The one real mismatch kept the micro-fix dead. It asks for
``{start_line, end_line, replacement}``; the provider answered with a
``2b.1`` envelope, observed live 2026-09-19 on
``op-01a0bcce-27c9`` -- and aimed it at the SOURCE module while repairing a
TEST file. Both halves of that are pinned below.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.interactive_repair import (
    InteractiveRepairLoop,
)
from backend.core.ouroboros.governance.payload_adapter import (
    candidate_contents,
    line_patch,
    micro_fix_contract,
    strip_fences,
)

ORIGINAL = "def f():\n    return 1\n"
PROPOSED = "def f():\n    return 2\n"


def _envelope(content=PROPOSED, path="m.py"):
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [{
            "candidate_id": "0", "file_path": path,
            "rationale": "fix", "full_content": content,
        }],
    })


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extracts_from_the_envelope_the_provider_actually_sends():
    assert candidate_contents(_envelope(), file_path="m.py") == PROPOSED


def test_extracts_from_a_bare_candidate():
    raw = json.dumps({"file_path": "m.py", "full_content": PROPOSED})
    assert candidate_contents(raw, file_path="m.py") == PROPOSED


def test_extracts_the_matching_entry_from_a_multi_file_shape():
    raw = json.dumps({"candidates": [{"files": [
        {"file_path": "other.py", "full_content": "x = 1\n"},
        {"file_path": "m.py", "full_content": PROPOSED},
    ]}]})
    assert candidate_contents(raw, file_path="m.py") == PROPOSED


def test_wrong_file_is_refused_not_adopted():
    """The live failure: repairing a TEST, the model proposed a change to the
    SOURCE module. Adopting it would patch a file the op never targeted."""
    raw = _envelope(path="backend/other.py")
    assert candidate_contents(raw, file_path="tests/test_m.py") is None
    assert micro_fix_contract(
        raw, original=ORIGINAL, file_path="tests/test_m.py",
    ) is None


def test_suffix_paths_still_match():
    """Providers echo repo-relative or bare names interchangeably."""
    raw = _envelope(path="tests/test_m.py")
    assert candidate_contents(raw, file_path="test_m.py") == PROPOSED


def test_fences_are_stripped():
    assert candidate_contents("```json\n" + _envelope() + "\n```",
                              file_path="m.py") == PROPOSED


def test_strip_fences_is_safe_on_plain_text():
    assert strip_fences("nothing to strip") == "nothing to strip"


def test_non_json_yields_nothing():
    for junk in ("", "not json", "{unclosed", None):
        assert candidate_contents(junk) is None


# ---------------------------------------------------------------------------
# Line-range derivation
# ---------------------------------------------------------------------------


def test_single_line_change_is_exact():
    patch = line_patch(ORIGINAL, PROPOSED)
    assert (patch.start_line, patch.end_line) == (2, 2)
    assert patch.replacement == "    return 2\n"


def test_replacement_reconstructs_the_proposal():
    """The contract's whole job: applying the range must reproduce the file."""
    before = "a\nb\nc\nd\ne\n"
    after = "a\nB\nC\nd\ne\n"
    patch = line_patch(before, after)
    lines = before.splitlines(keepends=True)
    lines[patch.start_line - 1:patch.end_line] = patch.replacement.splitlines(keepends=True)
    assert "".join(lines) == after


def test_multiple_edits_collapse_to_an_enclosing_span():
    """One span, not hunks: the untouched middle rides along verbatim, so
    the range is larger than strictly necessary and never wrong."""
    before = "a\nb\nc\nd\ne\n"
    after = "a\nX\nc\nY\ne\n"
    patch = line_patch(before, after)
    assert patch.start_line == 2 and patch.end_line == 4
    lines = before.splitlines(keepends=True)
    lines[patch.start_line - 1:patch.end_line] = patch.replacement.splitlines(keepends=True)
    assert "".join(lines) == after


def test_insertion_reconstructs_too():
    before = "a\nb\n"
    after = "a\nNEW\nb\n"
    patch = line_patch(before, after)
    lines = before.splitlines(keepends=True)
    lines[patch.start_line - 1:patch.end_line] = patch.replacement.splitlines(keepends=True)
    assert "".join(lines) == after


def test_deletion_reconstructs_too():
    before = "a\nb\nc\n"
    after = "a\nc\n"
    patch = line_patch(before, after)
    lines = before.splitlines(keepends=True)
    lines[patch.start_line - 1:patch.end_line] = patch.replacement.splitlines(keepends=True)
    assert "".join(lines) == after


def test_identical_text_is_not_a_patch():
    """A no-op reported as a repair is how 532 invocations looked alike."""
    assert line_patch(ORIGINAL, ORIGINAL) is None


def test_line_numbers_are_one_based():
    patch = line_patch("only\n", "changed\n")
    assert patch.start_line == 1


# ---------------------------------------------------------------------------
# Wired into the loop
# ---------------------------------------------------------------------------


def test_strict_contract_is_preferred():
    """A provider that answers correctly never pays for a comparison."""
    raw = json.dumps({"start_line": 2, "end_line": 2, "replacement": "    return 2"})
    fix = InteractiveRepairLoop._parse_micro_fix(raw, "m.py", ORIGINAL)
    assert fix.line_range == (2, 2)
    assert fix.reasoning == ""


def test_caged_envelope_is_translated():
    fix = InteractiveRepairLoop._parse_micro_fix(_envelope(), "m.py", ORIGINAL)
    assert fix is not None
    assert fix.line_range == (2, 2)
    assert "translated" in fix.reasoning


def test_without_original_there_is_nothing_to_compare():
    """Backward compatible: the old two-argument call cannot translate, and
    must not pretend to."""
    assert InteractiveRepairLoop._parse_micro_fix(_envelope(), "m.py") is None


def test_unusable_response_still_returns_none():
    assert InteractiveRepairLoop._parse_micro_fix("garbage", "m.py", ORIGINAL) is None


def test_translation_never_raises_on_junk():
    for junk in ("", "{}", "[]", '{"candidates": []}', '{"candidates": [1,2]}'):
        InteractiveRepairLoop._parse_micro_fix(junk, "m.py", ORIGINAL)
