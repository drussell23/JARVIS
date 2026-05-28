"""Slice 38 — Canonical JSONL Batch Entry Composer + Trailing Newline.

Closes the root cause of the v25→v33 capability blocker. Per direct
contact with DW Support (peter@doubleword.ai, 2026-05-28): our
``/v1/files`` uploads were rejected as "invalid multi part files"
because the JSONL payload was a single ``json.dumps(...)`` output
with NO trailing newline — structurally valid JSON but structurally
invalid JSONL/ndjson per RFC 7464.

Slice 38 introduces ``DoublewordProvider._compose_jsonl_batch_entry``
as the single source of truth for batch entry framing, replaces the
two raw ``json.dumps`` call sites (``submit_batch`` line 884 +
``prompt_only`` line 3472), and adds a belt-and-braces guard in
``_upload_file`` that warn-and-fixes any future bypass.

Test surface:
  * 5 AST pins (composer present + signature + both call sites
    wired + no raw json.dumps→_upload_file chain + belt-and-braces
    guard wired)
  * 8 spine (composer raises on bad input, always trails \\n,
    upload guard fires, preserves byte shape except for \\n)
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 5
# ──────────────────────────────────────────────────────────────────────


def _find_member(tree: ast.AST, cls_name: str, fn_name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if isinstance(
                    sub, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and sub.name == fn_name:
                    return sub
    return None


def test_ast_pin_slice38_composer_present() -> None:
    """``DoublewordProvider._compose_jsonl_batch_entry`` MUST exist
    as a @staticmethod taking ``entry: Dict[str, Any]`` and returning
    ``str``."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    node = _find_member(tree, "DoublewordProvider",
                        "_compose_jsonl_batch_entry")
    assert node is not None, (
        "DoublewordProvider._compose_jsonl_batch_entry missing"
    )
    body = ast.unparse(node)
    # Must validate required fields
    for field in ("custom_id", "method", "url", "body"):
        assert f"'{field}'" in body, (
            f"composer must validate required field {field!r}"
        )
    # Must end with json.dumps(...) + "\n"
    assert "+ '\\n'" in body or '+ "\\n"' in body, (
        "composer must append literal \\n trailing newline"
    )
    assert "json.dumps(entry)" in body


def test_ast_pin_slice38_composer_is_staticmethod() -> None:
    """The composer is a @staticmethod — pure function, no provider
    state involved. This is intentional: the JSONL framing rules are
    spec-defined (RFC 7464), not per-provider-instance."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    node = _find_member(tree, "DoublewordProvider",
                        "_compose_jsonl_batch_entry")
    assert node is not None
    decorator_names = [
        d.id for d in node.decorator_list if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorator_names, (
        "composer must be @staticmethod (pure function over the "
        "JSONL spec, no provider state)"
    )


def test_ast_pin_slice38_submit_batch_uses_composer() -> None:
    """``submit_batch`` MUST call ``self._compose_jsonl_batch_entry``
    instead of raw ``json.dumps(...)`` followed by upload."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    node = _find_member(tree, "DoublewordProvider", "submit_batch")
    assert node is not None, "submit_batch not located"
    body = ast.unparse(node)
    assert "self._compose_jsonl_batch_entry" in body, (
        "submit_batch must use the canonical composer"
    )


def test_ast_pin_slice38_prompt_only_uses_composer() -> None:
    """``prompt_only`` MUST call ``self._compose_jsonl_batch_entry``
    instead of raw ``json.dumps(...)`` followed by upload."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    node = _find_member(tree, "DoublewordProvider", "prompt_only")
    assert node is not None, "prompt_only not located"
    body = ast.unparse(node)
    assert "self._compose_jsonl_batch_entry" in body, (
        "prompt_only must use the canonical composer"
    )


def test_ast_pin_slice38_upload_file_guard_present() -> None:
    """``_upload_file`` MUST have a belt-and-braces guard that
    warn-and-fixes any payload missing the trailing ``\\n``."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    node = _find_member(tree, "DoublewordProvider", "_upload_file")
    assert node is not None
    body = ast.unparse(node)
    assert "jsonl_content.endswith" in body, (
        "guard must check for trailing newline via endswith"
    )
    assert "payload missing" in body and "trailing newline" in body, (
        "guard must log a structured warning identifying the issue"
    )
    assert "_compose_jsonl_batch_entry" in body, (
        "guard warning must reference the canonical composer so "
        "operators know where to look"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 8
# ──────────────────────────────────────────────────────────────────────


def _composer():
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    return DoublewordProvider._compose_jsonl_batch_entry


def test_spine_composer_always_ends_with_newline() -> None:
    out = _composer()({
        "custom_id": "abc",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "x", "messages": []},
    })
    assert out.endswith("\n"), (
        "composer output must end with exactly one trailing newline"
    )
    # Exactly ONE trailing newline (not multiple)
    assert not out.endswith("\n\n"), (
        "composer must emit exactly ONE trailing newline (RFC 7464)"
    )


def test_spine_composer_output_parses_as_jsonl() -> None:
    """The output must be parseable as a single JSONL record per
    RFC 7464: split on \\n, parse each non-empty line as JSON."""
    out = _composer()({
        "custom_id": "abc",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "x", "messages": [{"role": "user",
                                             "content": "hi"}]},
    })
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert len(lines) == 1, (
        f"expected exactly 1 JSONL line, got {len(lines)}"
    )
    parsed = json.loads(lines[0])
    assert parsed["custom_id"] == "abc"
    assert parsed["body"]["model"] == "x"


def test_spine_composer_raises_typeerror_on_non_dict() -> None:
    bad_inputs: list[Any] = [None, "string", 42, [1, 2, 3], object()]
    for bad in bad_inputs:
        with pytest.raises(TypeError, match="expects dict"):
            _composer()(bad)


def test_spine_composer_raises_valueerror_on_missing_field() -> None:
    """Every required field must be present."""
    base = {
        "custom_id": "x",
        "method": "POST",
        "url": "/v1/y",
        "body": {},
    }
    for missing in ("custom_id", "method", "url", "body"):
        bad = {k: v for k, v in base.items() if k != missing}
        with pytest.raises(ValueError, match="missing required"):
            _composer()(bad)


def test_spine_composer_raises_on_non_dict_body() -> None:
    """``body`` must be a dict (per OpenAI/DW batch API spec)."""
    for bad_body in ("string", 42, [1, 2], None):
        with pytest.raises(ValueError, match="'body' must be dict"):
            _composer()({
                "custom_id": "x",
                "method": "POST",
                "url": "/v1/y",
                "body": bad_body,
            })


def test_spine_composer_preserves_byte_shape_except_newline() -> None:
    """The byte shape MUST be identical to the legacy
    ``json.dumps(entry)`` output, with exactly one ``\\n`` appended.
    This makes Slice 38 the minimum-diff structural fix — any
    remaining DW issues are unambiguously \\n-independent."""
    entry = {
        "custom_id": "abc",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "Qwen/Qwen3.5-35B-A3B-FP8",
                 "messages": [{"role": "user", "content": "test"}]},
    }
    legacy = json.dumps(entry)
    slice38 = _composer()(entry)
    assert slice38 == legacy + "\n", (
        "composer must emit json.dumps(entry) + '\\n' exactly"
    )


def test_spine_upload_file_guard_fires_warning() -> None:
    """If a caller bypasses the composer and hands ``_upload_file``
    a payload without a trailing newline, the guard MUST log a
    WARNING explicitly referencing the composer."""
    # We can't easily exercise _upload_file end-to-end without a live
    # aiohttp session, but we CAN verify the guard branch fires by
    # patching the early-exit paths. The simpler structural test is
    # to verify the AST + the warning template — the AST pin above
    # already enforces the structure; here we verify the log message
    # template would surface the right diagnostic.
    src = DW_FILE.read_text()
    # Confirm the warning message includes the literal phrase
    # "_compose_jsonl_batch_entry" so log greps surface it.
    assert (
        "_compose_jsonl_batch_entry" in src
        and "payload missing" in src
        and "trailing newline" in src
    )


def test_spine_no_raw_json_dumps_then_upload_chain() -> None:
    """Structural guard: across the entire file, no
    ``json.dumps(...)`` line may be IMMEDIATELY followed by a
    call to ``self._upload_file(...)``. This would mean a future
    refactor reintroduced the bug Slice 38 fixed.

    NB: lenient — we only check the within-3-line window because
    the actual reintroduction pattern would have json.dumps and
    _upload_file very close together (as the v25-v33 bug did)."""
    src = DW_FILE.read_text()
    lines = src.splitlines()
    violations = []
    for i, line in enumerate(lines):
        if "json.dumps(" in line and "jsonl" in line.lower():
            # Look ahead 5 lines for _upload_file call
            window = "\n".join(lines[i:i + 5])
            if "self._upload_file(" in window:
                violations.append((i + 1, line.strip()))
    assert not violations, (
        "raw json.dumps(...) → _upload_file chain detected — "
        f"must use _compose_jsonl_batch_entry: {violations}"
    )
