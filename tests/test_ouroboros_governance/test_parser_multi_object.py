"""Tests for the multi-object JSON parser in providers._extract_json_block.

The model sometimes emits self-correcting responses — writes a JSON object,
realises it made a mistake, then writes a second corrected JSON. The
parser must pick the LAST object containing ``schema_version`` so the
model's intended final answer wins.
"""

from __future__ import annotations

import json

from backend.core.ouroboros.governance.providers import (
    _extract_json_block,
    _find_all_top_level_json,
    _pick_preferred_json_object,
)


# ---------------------------------------------------------------------------
# _find_all_top_level_json
# ---------------------------------------------------------------------------


def test_find_all_single_object() -> None:
    text = '{"schema_version": "2b.1", "x": 1}'
    assert _find_all_top_level_json(text) == [text]


def test_find_all_two_back_to_back_objects() -> None:
    text = '{"a": 1}{"b": 2}'
    assert _find_all_top_level_json(text) == ['{"a": 1}', '{"b": 2}']


def test_find_all_objects_with_natural_language_between() -> None:
    text = (
        '{"schema_version": "2b.1", "attempt": 1}\n\n'
        "Wait, I need to reconsider.\n\n"
        '{"schema_version": "2b.1", "attempt": 2}'
    )
    objects = _find_all_top_level_json(text)
    assert len(objects) == 2
    assert '"attempt": 1' in objects[0]
    assert '"attempt": 2' in objects[1]


def test_find_all_objects_with_markdown_fences_between() -> None:
    text = (
        '```json\n{"schema_version": "2b.1", "attempt": 1}\n```\n'
        "Wait, I need to reconsider.\n"
        '```json\n{"schema_version": "2b.1", "attempt": 2}\n```'
    )
    objects = _find_all_top_level_json(text)
    assert len(objects) == 2
    assert json.loads(objects[0])["attempt"] == 1
    assert json.loads(objects[1])["attempt"] == 2


def test_find_all_handles_nested_braces() -> None:
    text = '{"outer": {"inner": {"deep": 1}}}{"next": 2}'
    objects = _find_all_top_level_json(text)
    assert len(objects) == 2
    assert json.loads(objects[0]) == {"outer": {"inner": {"deep": 1}}}
    assert json.loads(objects[1]) == {"next": 2}


def test_find_all_handles_braces_inside_strings() -> None:
    text = '{"msg": "hello { world }"}{"other": 2}'
    objects = _find_all_top_level_json(text)
    assert len(objects) == 2
    assert json.loads(objects[0])["msg"] == "hello { world }"


def test_find_all_handles_escaped_quotes() -> None:
    text = '{"msg": "he said \\"hi\\""}{"other": 2}'
    objects = _find_all_top_level_json(text)
    assert len(objects) == 2
    assert json.loads(objects[0])["msg"] == 'he said "hi"'


def test_find_all_returns_empty_for_no_json() -> None:
    assert _find_all_top_level_json("just some text") == []


def test_find_all_handles_unbalanced_gracefully() -> None:
    # Unbalanced text — should bail early, not loop forever
    text = '{"a": 1}{"b": ' + '{' * 100
    objects = _find_all_top_level_json(text)
    # First object is well-formed, second is unbalanced → bail.
    assert len(objects) == 1
    assert objects[0] == '{"a": 1}'


# ---------------------------------------------------------------------------
# _pick_preferred_json_object
# ---------------------------------------------------------------------------


def test_pick_empty_list_returns_none() -> None:
    assert _pick_preferred_json_object([]) is None


def test_pick_single_object_returns_it() -> None:
    obj = '{"schema_version": "2b.1"}'
    assert _pick_preferred_json_object([obj]) == obj


def test_pick_prefers_last_schema_version_object() -> None:
    first = '{"schema_version": "2b.1", "attempt": 1}'
    second = '{"schema_version": "2b.1", "attempt": 2}'
    assert _pick_preferred_json_object([first, second]) == second


def test_pick_skips_non_schema_tail_object() -> None:
    """If the tail object lacks schema_version, pick the last one that has it."""
    schema = '{"schema_version": "2b.1", "attempt": 1}'
    trailing = '{"meta": "some other thing"}'
    assert _pick_preferred_json_object([schema, trailing]) == schema


def test_pick_falls_back_to_last_object_when_no_schema_version() -> None:
    a = '{"a": 1}'
    b = '{"b": 2}'
    assert _pick_preferred_json_object([a, b]) == b


# ---------------------------------------------------------------------------
# _extract_json_block — end-to-end self-correction scenarios
# ---------------------------------------------------------------------------


def test_extract_picks_corrected_version_on_self_correction() -> None:
    """The exact pattern from bt-2026-04-10-182037 debug.log."""
    raw = (
        '{\n'
        '  "schema_version": "2b.1",\n'
        '  "candidates": [\n'
        '    {"candidate_id": "c1", "full_content": "typo here"}\n'
        '  ]\n'
        '}\n'
        '```\n\n'
        "Wait, I need to reconsider. `python_requires` is not valid in "
        "requirements.txt. Let me provide a corrected version.\n\n"
        '```json\n'
        '{\n'
        '  "schema_version": "2b.1",\n'
        '  "candidates": [\n'
        '    {"candidate_id": "c1", "full_content": "corrected content"}\n'
        '  ]\n'
        '}\n'
        '```'
    )
    extracted = _extract_json_block(raw)
    obj = json.loads(extracted)
    assert obj["candidates"][0]["full_content"] == "corrected content"


def test_extract_single_object_unchanged() -> None:
    raw = '{"schema_version": "2b.1", "candidates": []}'
    extracted = _extract_json_block(raw)
    assert json.loads(extracted) == {"schema_version": "2b.1", "candidates": []}


def test_extract_handles_markdown_fenced_single_object() -> None:
    raw = '```json\n{"schema_version": "2b.1", "x": 1}\n```'
    extracted = _extract_json_block(raw)
    assert json.loads(extracted)["x"] == 1


def test_extract_handles_two_fenced_blocks_picks_last_schema() -> None:
    raw = (
        "Here's my first attempt:\n\n"
        '```json\n{"schema_version": "2b.1", "attempt": 1}\n```\n\n'
        "Actually wait, let me fix that:\n\n"
        '```json\n{"schema_version": "2b.1", "attempt": 2}\n```'
    )
    extracted = _extract_json_block(raw)
    obj = json.loads(extracted)
    assert obj["attempt"] == 2


def test_extract_handles_think_block_prefix() -> None:
    raw = (
        "<think>Let me think about this...</think>\n"
        '{"schema_version": "2b.1", "x": 1}'
    )
    extracted = _extract_json_block(raw)
    assert json.loads(extracted)["x"] == 1


def test_extract_handles_think_block_plus_self_correction() -> None:
    raw = (
        "<think>thinking hard</think>\n"
        '{"schema_version": "2b.1", "attempt": 1}\n'
        "Wait, nope:\n"
        '{"schema_version": "2b.1", "attempt": 2}'
    )
    extracted = _extract_json_block(raw)
    assert json.loads(extracted)["attempt"] == 2


def test_extract_trailing_non_schema_object_still_picks_schema() -> None:
    """If the model adds a trailing meta object without schema_version,
    we should still pick the schema object."""
    raw = (
        '{"schema_version": "2b.1", "x": 1}\n'
        '{"meta": "irrelevant"}'
    )
    extracted = _extract_json_block(raw)
    obj = json.loads(extracted)
    assert obj["x"] == 1


def test_extract_preserves_deep_nesting() -> None:
    raw = (
        '{"schema_version": "2b.1", "candidates": ['
        '{"full_content": "line1\\nline2\\n{\\"not\\": \\"real\\"}\\nline4"}'
        ']}'
    )
    extracted = _extract_json_block(raw)
    obj = json.loads(extracted)
    assert "{\"not\": \"real\"}" in obj["candidates"][0]["full_content"]
