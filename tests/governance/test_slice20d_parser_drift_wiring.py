"""Slice 20D — Parser-level schema_id_hallucination drift hook.

Closes the v15 forensic gap left after Slice 20B+20C+Phase 3:

* Slice 20B wired JSON_PARSE_ERROR_AFTER_HEAL drift at the heal layer.
* Slice 20C wired ZERO_CANDIDATE_RETURN drift at the dispatch return path.
* The remaining SCHEMA_ID_HALLUCINATION enum value had substrate but no
  caller — parser at ``providers.py:4121`` detected the v15 ``2b.1-diff``
  hallucination case, logged a warning, attempted reconstruction, but
  NEVER fed the signal to the drift tracker. Result: the rotation
  engine was blind to schema traps at parser level.

Slice 20D wires that hook:

* When ``actual_version == _SCHEMA_VERSION_DIFF`` (2b.1-diff) AFTER the
  prompt explicitly directed 2b.1 full_content, the parser records a
  ``SCHEMA_ID_HALLUCINATION`` drift event keyed by ``(op_id, model_id)``.
* Model_id is resolved via ``topology_sentinel.get_dw_model_override()``
  (async-safe ContextVar reader stamped by the dispatcher at
  ``candidate_generator.py:2583``).
* Recovery STILL runs (diff→full_content reconstruction); drift is
  op-scoped and only affects subsequent retries for the same op_id.

# Test surface (1 AST pin + 3 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROV_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PIN — 1
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_parser_records_schema_id_hallucination_at_diff_site() -> None:
    """The Slice 20D hook MUST live inside the ``2b.1-diff`` branch
    of ``_parse_generation_response`` — specifically after the warning
    log and before (or alongside) the reconstruction attempt.

    Without this hook, the drift tracker has SCHEMA_ID_HALLUCINATION
    in its DriftType enum (Slice 20C) but no caller — the rotation
    engine runs blind to parser-level schema traps.
    """
    src = PROV_FILE.read_text()
    assert "Slice 20D" in src, (
        "providers.py missing Slice 20D attribution — hook reverted"
    )
    assert "SCHEMA_ID_HALLUCINATION" in src, (
        "providers.py missing SCHEMA_ID_HALLUCINATION drift_type "
        "reference — hook dead code"
    )
    assert "get_dw_model_override" in src, (
        "providers.py missing topology_sentinel.get_dw_model_override "
        "import — model_id resolution broken; drift records would "
        "carry wrong provenance"
    )
    # Verify the hook is INSIDE the diff-detection branch by walking
    # the AST and checking the function's source contains both the
    # diff comparison AND the drift record name.
    tree = ast.parse(src, filename=str(PROV_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_parse_generation_response"
        ):
            body_src = ast.unparse(node)
            if (
                "_SCHEMA_VERSION_DIFF" in body_src
                and "SCHEMA_ID_HALLUCINATION" in body_src
                and "Slice 20D" in body_src
            ):
                found = True
                break
    assert found, (
        "_parse_generation_response body missing the Slice 20D drift "
        "record in the _SCHEMA_VERSION_DIFF branch — hook misplaced "
        "or detection structure changed"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 3
# ──────────────────────────────────────────────────────────────────────


def test_spine_20d_diff_schema_records_drift_with_dispatcher_model_id(
    monkeypatch,
) -> None:
    """End-to-end: build a ``2b.1-diff`` payload, simulate the
    dispatcher having stamped a model_id via ContextVar, parse the
    payload, and verify drift was recorded with the dispatcher's
    model_id (NOT just provider_name)."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    from backend.core.ouroboros.governance.providers import (
        _parse_generation_response,
    )
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        get_default_tracker, reset_default_tracker, DriftType,
    )
    from backend.core.ouroboros.governance.topology_sentinel import (
        set_dw_model_override, reset_dw_model_override,
    )
    reset_default_tracker()

    # Stamp the dispatcher's model_id override
    _token = set_dw_model_override("Qwen/Qwen3.5-397B-A17B-FP8")
    try:
        # Build a 2b.1-diff response. The reconstruction will fail
        # (no source file) but the drift record fires BEFORE
        # reconstruction. We expect: (a) drift recorded, (b) the
        # parser raises (because reconstruction with empty source
        # is rejected by the diff_source_unreadable gate).
        raw = (
            '{"schema_version": "2b.1-diff", "candidates": [{'
            '"candidate_id": "c1", "file_path": "fake.py", '
            '"unified_diff": "--- a\\n+++ b\\n@@ -1 +1 @@\\n-x\\n+y\\n", '
            '"rationale": "test"}]}'
        )
        ctx = mock.MagicMock()
        ctx.op_id = "op-20d-1"
        ctx.is_read_only = False  # MagicMock attrs default truthy
        try:
            _parse_generation_response(
                raw=raw,
                provider_name="doubleword",
                duration_s=0.1,
                ctx=ctx,
                source_hash="",
                source_path="nonexistent.py",
            )
        except RuntimeError:
            # Reconstruction failure is expected (no source file)
            pass
    finally:
        reset_dw_model_override(_token)

    # The drift MUST have been recorded — even though reconstruction
    # failed downstream. Drift records BEFORE reconstruction.
    tracker = get_default_tracker()
    assert tracker.has_drifted(
        "op-20d-1", "Qwen/Qwen3.5-397B-A17B-FP8",
    ), (
        "Slice 20D drift NOT recorded with dispatcher's model_id; "
        "parser hook is not reading topology_sentinel ContextVar"
    )
    # Confirm the event taxonomy is SCHEMA_ID_HALLUCINATION (not
    # some other drift type)
    events = tracker.events_for("op-20d-1")
    assert any(
        e.drift_type == DriftType.SCHEMA_ID_HALLUCINATION
        for e in events
    ), (
        f"Slice 20D recorded wrong drift_type: got "
        f"{[e.drift_type for e in events]!r}, expected SCHEMA_ID_HALLUCINATION"
    )


def test_spine_20d_falls_back_to_provider_name_when_no_override(monkeypatch) -> None:
    """When the dispatcher hasn't stamped an override (legacy single-
    model path), the parser MUST fall back to provider_name so the
    drift record still carries meaningful provenance."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    from backend.core.ouroboros.governance.providers import (
        _parse_generation_response,
    )
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        get_default_tracker, reset_default_tracker,
    )
    from backend.core.ouroboros.governance.topology_sentinel import (
        get_dw_model_override,
    )
    reset_default_tracker()
    # Verify NO override active
    assert get_dw_model_override() is None

    raw = (
        '{"schema_version": "2b.1-diff", "candidates": [{'
        '"candidate_id": "c1", "file_path": "fake.py", '
        '"unified_diff": "--- a\\n+++ b\\n@@ -1 +1 @@\\n-x\\n+y\\n", '
        '"rationale": "test"}]}'
    )
    ctx = mock.MagicMock()
    ctx.op_id = "op-20d-2"
    ctx.is_read_only = False
    try:
        _parse_generation_response(
            raw=raw,
            provider_name="doubleword",
            duration_s=0.1,
            ctx=ctx,
            source_hash="",
            source_path="nonexistent.py",
        )
    except RuntimeError:
        pass

    tracker = get_default_tracker()
    # Drift recorded under "doubleword" (provider_name fallback)
    assert tracker.has_drifted("op-20d-2", "doubleword"), (
        "Slice 20D did not fall back to provider_name when override absent"
    )


def test_spine_20d_drift_record_failure_does_not_break_recovery(
    monkeypatch,
) -> None:
    """If the drift tracker module raises (e.g. circular-import
    collapse, OS error in the tracker init), the parser MUST continue
    to attempt reconstruction. Drift is an enhancement, NEVER a gate.

    Verified by patching get_default_tracker to raise and confirming
    the parser still reaches the reconstruction path (it will fail
    on missing source file, raising RuntimeError; the point is the
    drift-record exception is swallowed, not propagated)."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    from backend.core.ouroboros.governance.providers import (
        _parse_generation_response,
    )

    # Patch get_default_tracker on its OWN module so the lazy import
    # inside the parser picks up the broken version.
    import backend.core.ouroboros.governance.schema_drift_tracker as sdt_mod

    def _broken_tracker():
        raise RuntimeError("simulated tracker init failure")

    monkeypatch.setattr(sdt_mod, "get_default_tracker", _broken_tracker)

    raw = (
        '{"schema_version": "2b.1-diff", "candidates": [{'
        '"candidate_id": "c1", "file_path": "fake.py", '
        '"unified_diff": "--- a\\n+++ b\\n@@ -1 +1 @@\\n-x\\n+y\\n", '
        '"rationale": "test"}]}'
    )
    ctx = mock.MagicMock()
    ctx.op_id = "op-20d-3"
    ctx.is_read_only = False

    # The parser MUST NOT propagate the drift-record exception.
    # It will raise its own RuntimeError for the missing-source-file
    # path (diff_source_unreadable) — which is the EXPECTED downstream
    # error. The Slice 20D try/except must swallow the broken tracker
    # so this downstream error reaches us cleanly.
    raised_msg = None
    try:
        _parse_generation_response(
            raw=raw,
            provider_name="doubleword",
            duration_s=0.1,
            ctx=ctx,
            source_hash="",
            source_path="nonexistent_for_20d.py",
        )
    except RuntimeError as exc:
        raised_msg = str(exc)

    # If drift-record exception had propagated, the message would be
    # "simulated tracker init failure". The expected message is the
    # parser's own diff_source_unreadable raise.
    assert raised_msg is not None
    assert "simulated tracker init failure" not in raised_msg, (
        "Slice 20D drift-record exception leaked into the parse path — "
        "MUST be swallowed so recovery is never gated by tracker health"
    )
    assert "diff_source_unreadable" in raised_msg, (
        f"Parser did not reach reconstruction path; got: {raised_msg!r}"
    )
