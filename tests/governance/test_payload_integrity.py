"""Structural payload-integrity validation — truncation vs syntax fault.

Raising the Aegis upstream read ceiling to 600s means a generation is allowed to
run long, but a socket severed at the wall (or any mid-stream drop) delivers a
PARTIAL body. A partial JSON body must be rejected as TRUNCATION — not silently
brace-closed by the deterministic repair into a corrupt-but-parseable blueprint
that flows into the governed loop.

These tests pin: severed payloads raise PayloadTruncationError (distinct from a
JSONDecodeError), the deterministic repair is still reused for genuine syntax
faults, string-internal braces don't false-balance, and the DreamEngine cascade
boundary routes truncation to the failure lifecycle with distinct telemetry.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.payload_integrity import (
    PayloadTruncationError,
    is_truncated,
    structural_scan,
    validate_json_payload,
)


# ---------------------------------------------------------------------------
# Structural scan — string-aware container/quote balance
# ---------------------------------------------------------------------------


def test_scan_balanced():
    assert structural_scan('{"a": 1}') == (0, False, True)


def test_scan_open_container():
    depth, in_string, saw = structural_scan('{"a": [1, 2')
    assert depth == 2 and in_string is False and saw is True


def test_scan_open_string():
    depth, in_string, saw = structural_scan('{"a": "unterminated')
    assert in_string is True and saw is True


def test_scan_ignores_braces_inside_strings():
    # Braces / brackets inside a string literal must not affect depth.
    assert structural_scan('{"a": "}}}]]]"}') == (0, False, True)


def test_scan_respects_escaped_quote():
    # An escaped quote does NOT close the string.
    depth, in_string, _ = structural_scan('{"a": "he said \\"hi\\""}')
    assert depth == 0 and in_string is False


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------


def test_clean_payload_not_truncated():
    assert is_truncated('{"a": 1}') is False


def test_severed_mid_string_is_truncated():
    assert is_truncated('{"changes":[{"file":"a.py","content":"def foo(') is True


def test_unbalanced_containers_truncated():
    assert is_truncated('{"changes": [{"x": 1}') is True


def test_empty_or_garbage_not_flagged_as_truncation():
    # No container ever opened → not the sever class (it's just empty/garbage,
    # which the parse step rejects as a normal error).
    assert is_truncated("") is False
    assert is_truncated("   ") is False


# ---------------------------------------------------------------------------
# validate_json_payload — the boundary contract
# ---------------------------------------------------------------------------


def test_valid_payload_parses():
    assert validate_json_payload('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_severed_payload_raises_truncation_not_parse_error():
    sev = '{"changes":[{"file":"a.py","content":"def foo('
    with pytest.raises(PayloadTruncationError) as ei:
        validate_json_payload(sev)
    err = ei.value
    assert err.in_string is True and err.depth > 0
    assert err.received_bytes == len(sev)
    # Crucially, it is NOT a bare JSONDecodeError — the caller can distinguish it.
    assert isinstance(err, ValueError)
    assert not isinstance(err, json.JSONDecodeError)


def test_truncation_is_not_silently_brace_closed():
    # The exact danger: _repair_json closes open containers. Prove the validator
    # rejects the sever BEFORE that can produce corrupt-but-parseable data.
    sev = '{"file": "x.py", "content": "def f(): retur'
    with pytest.raises(PayloadTruncationError):
        validate_json_payload(sev)


def test_complete_but_malformed_is_repaired_not_truncation():
    # Trailing comma — complete structure, genuine syntax defect → deterministic
    # repair recovers it; NOT a truncation.
    assert validate_json_payload('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_complete_malformed_unrepairable_raises_decode_error():
    # Balanced braces but genuinely broken content that repair can't fix →
    # a JSONDecodeError (syntax fault), still NOT a truncation.
    with pytest.raises(json.JSONDecodeError):
        validate_json_payload('{"a": @@@ !!! nonsense here}', allow_repair=True)


def test_non_object_root_rejected():
    with pytest.raises(ValueError):
        validate_json_payload('[1, 2, 3]')


def test_markdown_fenced_payload():
    assert validate_json_payload('```json\n{"a": 5}\n```') == {"a": 5}


def test_lone_opening_fence_with_severed_body_is_truncation():
    # A ```json opener with a cut body (no closing fence) → the structural scan
    # still sees the severed object.
    assert is_truncated('```json\n{"a": [1, 2') is True


# ---------------------------------------------------------------------------
# 600s-sever SIMULATION at the DreamEngine Tier-0/1/2 cascade boundary
# ---------------------------------------------------------------------------


def test_dream_cascade_routes_truncation_to_failure(caplog):
    """Simulate an upstream sever at the 600s wall: the inference returns a
    partial body. The cascade boundary must route it to the failure lifecycle
    (return None) with DISTINCT truncation telemetry — not a corrupt blueprint,
    not a silent generic None."""
    import logging
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine

    severed = (
        '{"title": "Optimize the hot loop", "target_files": ["a.py"], '
        '"changes": [{"file": "a.py", "content": "def hot():\\n    for i in ra'
    )  # ← socket cut here, mid-string, mid-list
    caplog.set_level(logging.WARNING)
    result = DreamEngine._parse_json_response(severed)
    assert result is None                                   # routed to failure
    assert any("TRUNCATED" in m for m in caplog.messages)   # distinct telemetry


def test_dream_cascade_parses_complete_payload():
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    good = '{"title": "x", "target_files": ["a.py"], "changes": []}'
    assert DreamEngine._parse_json_response(good) == {
        "title": "x", "target_files": ["a.py"], "changes": [],
    }


def test_dream_cascade_repairs_trailing_comma():
    # A complete-but-sloppy DW body still flows (deterministic repair), so we
    # don't over-reject legitimate-if-messy generations as failures.
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    sloppy = '{"title": "x", "target_files": ["a.py"], "changes": [],}'
    assert DreamEngine._parse_json_response(sloppy) is not None
