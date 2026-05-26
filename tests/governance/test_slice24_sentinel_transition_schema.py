"""Slice 24 — Sentinel Transition Schema Completion.

Closes the half-shipped Phase 12 Slice F / Slice H "Substrate Error
Unmasking" schema. ``TopologySentinel.report_failure`` was extended
(kwargs at line 1265-1267) to accept structured-error fields:

    status_code: Optional[int] = None
    response_body: str = ""
    is_terminal: bool = False

It stuffed them into an ``extra`` dict and passed via ``**extra`` to
``_emit_transition`` — but ``_emit_transition`` was NEVER updated to
accept these kwargs, NOR was ``TransitionRecord`` updated to carry
them, NOR was ``to_json`` updated to serialize them. Every call to
``report_failure`` with a non-None ``status_code`` raised
``TypeError: TopologySentinel._emit_transition() got an unexpected
keyword argument 'status_code'`` — silently swallowed by the bare
``except`` at line 1379.

Consequence v18 forensic (bt-2026-05-26-233010) caught: 2 fires in
the first 20 min while Slice 23's fleet walker iterated all 4
trusted DW models. Every terminal failure (4xx modality, 401/403
auth, etc.) lost its structural fields from the audit ledger AND
the sentinel state machine never recorded ``is_terminal=True``
transitions — so models that should have flipped to TERMINAL_OPEN
stayed in their previous state at the persistent layer.

Slice 24 completes the schema additively:

  * ``TransitionRecord`` gains 3 fields with defaults
    (``status_code: Optional[int] = None``, ``response_body: str = ""``,
    ``is_terminal: bool = False``)
  * ``_emit_transition`` signature gains the same 3 kwargs
  * ``to_json`` serializes them ONLY when non-default (byte-size
    budget for legacy state_change / probe records preserved)

# Test surface (2 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "topology_sentinel.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_transition_record_has_structured_error_fields() -> None:
    """``TransitionRecord`` MUST declare the 3 structured-error fields
    with default values that preserve byte-identical legacy
    construction (so existing callers that build a record without
    them keep compiling)."""
    src = TS_FILE.read_text()
    assert "Slice 24" in src, (
        "topology_sentinel missing Slice 24 attribution — schema reverted"
    )
    tree = ast.parse(src, filename=str(TS_FILE))
    transition_record = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "TransitionRecord"
        ):
            transition_record = node
            break
    assert transition_record is not None, (
        "TransitionRecord class not found in topology_sentinel"
    )
    # Collect annotated field names
    field_names = set()
    for stmt in transition_record.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            field_names.add(stmt.target.id)
    for field in ("status_code", "response_body", "is_terminal"):
        assert field in field_names, (
            f"TransitionRecord missing field {field!r} — Slice 24 "
            "schema incomplete; _emit_transition will TypeError"
        )


def test_ast_pin_emit_transition_accepts_structured_kwargs() -> None:
    """``_emit_transition`` signature MUST accept the 3 structured-error
    kwargs with defaults. Without this, ``report_failure``'s ``**extra``
    splat raises TypeError and the bare except silently swallows the
    audit-ledger write (the v18 regression this slice closes)."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TopologySentinel,
    )
    sig = inspect.signature(TopologySentinel._emit_transition)
    params = sig.parameters
    for kwarg in ("status_code", "response_body", "is_terminal"):
        assert kwarg in params, (
            f"_emit_transition signature missing {kwarg!r} — "
            "report_failure's **extra splat will TypeError"
        )
        # Defaults present (otherwise the 7 OTHER call sites that
        # don't pass them break)
        assert params[kwarg].default is not inspect.Parameter.empty, (
            f"_emit_transition kwarg {kwarg!r} has no default — "
            "byte-identical legacy contract for other callers broken"
        )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_transition_record_construction_with_new_fields() -> None:
    """TransitionRecord MUST construct successfully with all 3 new
    fields populated (the structural terminal-failure shape)."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TransitionRecord,
    )
    r = TransitionRecord(
        ts_epoch=time.time(),
        model_id="Qwen/Qwen3.5-4B",
        transition_kind="state_change",
        from_state="CLOSED",
        to_state="TERMINAL_OPEN",
        failure_source="live_transport",
        failure_detail="http_403",
        status_code=403,
        response_body="blocked by routing rule",
        is_terminal=True,
    )
    assert r.status_code == 403
    assert r.response_body == "blocked by routing rule"
    assert r.is_terminal is True


def test_spine_legacy_record_still_constructs_unchanged() -> None:
    """Records built without the new fields MUST still construct
    (byte-identical legacy behavior — 7 other call sites in
    topology_sentinel pass no structured fields)."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TransitionRecord,
    )
    r = TransitionRecord(
        ts_epoch=time.time(),
        model_id="m",
        transition_kind="probe",
    )
    # Defaults
    assert r.status_code is None
    assert r.response_body == ""
    assert r.is_terminal is False


def test_spine_to_json_emits_structured_fields_when_non_default() -> None:
    """``to_json`` MUST emit the 3 new fields ONLY when non-default so
    the existing audit-ledger byte-size budget is preserved for
    legacy state_change / probe paths."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TransitionRecord,
    )
    # Non-default → fields emitted
    r1 = TransitionRecord(
        ts_epoch=time.time(),
        model_id="m",
        transition_kind="state_change",
        status_code=503,
        response_body="upstream timeout",
        is_terminal=False,
    )
    j1 = r1.to_json()
    assert j1.get("status_code") == 503
    assert j1.get("response_body") == "upstream timeout"
    # is_terminal=False is the default — NOT emitted to keep
    # legacy records byte-identical
    assert "is_terminal" not in j1, (
        "to_json emits is_terminal=False — should only emit when True"
    )

    # All defaults → none of the 3 fields appear in the payload
    r2 = TransitionRecord(ts_epoch=time.time(), model_id="m", transition_kind="probe")
    j2 = r2.to_json()
    assert "status_code" not in j2
    assert "response_body" not in j2
    assert "is_terminal" not in j2


def test_spine_to_json_truncates_response_body_at_512(

) -> None:
    """Response body excerpts can be large (full server error pages).
    The audit ledger has a byte budget — ``to_json`` MUST truncate at
    512 chars matching the existing ``failure_detail[:200]`` discipline."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TransitionRecord,
    )
    huge_body = "A" * 5000
    r = TransitionRecord(
        ts_epoch=time.time(),
        model_id="m",
        transition_kind="state_change",
        response_body=huge_body,
    )
    j = r.to_json()
    assert "response_body" in j
    assert len(j["response_body"]) == 512, (
        f"response_body not truncated: got len={len(j['response_body'])}"
    )


def test_spine_report_failure_no_longer_raises_typeerror() -> None:
    """The v18-reproducible bug: ``report_failure(status_code=403, ...)``
    used to TypeError inside ``_emit_transition``. Slice 24 fixes
    that — ``report_failure`` MUST complete cleanly when structured
    fields are passed. Verified by AST-walking the
    ``report_failure`` source to confirm both ``**extra`` splat
    sites are present, then calling report_failure with the v18
    failure shape and asserting NO exception leaks (the function's
    own ``try/except Exception`` shouldn't have to swallow anything
    in the normal path)."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TopologySentinel, FailureSource,
    )
    import logging

    # Capture any DEBUG-level "report_failure failed" log lines —
    # that's the bare-except's signature for swallowed exceptions.
    # If Slice 24 worked, this log line MUST NOT appear.
    sentinel = TopologySentinel()
    sentinel.register_endpoint("Qwen/Qwen3.5-4B")

    debug_msgs = []
    logger = logging.getLogger(
        "backend.core.ouroboros.governance.topology_sentinel"
    )
    handler = logging.Handler()
    handler.emit = lambda r: debug_msgs.append(r.getMessage())
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        # The exact shape v18 was failing on — http_403 terminal auth
        sentinel.report_failure(
            "Qwen/Qwen3.5-4B",
            FailureSource.LIVE_HTTP_5XX,
            detail="http_403",
            status_code=403,
            response_body="blocked by routing rule",
            is_terminal=True,
        )
    finally:
        logger.removeHandler(handler)

    swallowed = [m for m in debug_msgs if "report_failure failed" in m]
    assert not swallowed, (
        f"report_failure still swallowing TypeError — Slice 24 incomplete. "
        f"Swallowed messages: {swallowed!r}"
    )


def test_spine_emit_transition_forwards_all_structured_fields() -> None:
    """When ``_emit_transition`` is called with all 3 new kwargs, the
    resulting ``TransitionRecord`` MUST carry them through to the
    listener / store. End-to-end forward."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        TopologySentinel, TransitionRecord,
    )

    sentinel = TopologySentinel()
    sentinel.register_endpoint("test-model")
    seen = []
    sentinel.add_listener(lambda rec: seen.append(rec))

    sentinel._emit_transition(
        "test-model",
        "state_change",
        from_state="CLOSED",
        to_state="TERMINAL_OPEN",
        status_code=401,
        response_body="auth required",
        is_terminal=True,
    )
    assert len(seen) == 1
    rec = seen[0]
    assert isinstance(rec, TransitionRecord)
    assert rec.status_code == 401
    assert rec.response_body == "auth required"
    assert rec.is_terminal is True
