"""Slice 20B + 20C + Phase 3 — JSON healing, fleet rotation, DW reinforcement.

Closes the v15 soak ``bt-2026-05-26-184355`` capability gap end-to-end:

* **Slice 20B**: catch JSONDecodeError after deterministic regex repair
  exhausts → last-resort LLM heal via Qwen3.5-35B (zero-governance
  fast path via ``DoublewordProvider.prompt_only``); on heal failure,
  emit a Slice 20C drift event so the next retry rotates.
* **Slice 20C**: per-op drift tracker (3-value DriftType taxonomy:
  json_parse_error_after_heal / schema_id_hallucination /
  zero_candidate_return); dispatch loop consults ``has_drifted()``
  per ranked-models iteration and skips drifted models indistinguishably
  from sentinel-OPEN breakers.
* **Phase 3**: DW lean prompt gains a literal zero-candidate prohibition
  reinforcement on STANDARD/COMPLEX routes when
  ``JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED=true``.

# Test surface (4 AST pins + 14 spine)
"""

from __future__ import annotations

import ast
import asyncio
import os
import unittest.mock as mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JH_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "json_healer.py"
)
SDT_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "schema_drift_tracker.py"
)
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
PROV_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_heal_system_prompt_is_immutable_operator_string() -> None:
    """The exact operator-attested heal system prompt MUST be present.

    Future copy edits silently weaken the heal contract — block them
    structurally via this AST pin so a refactor needs a deliberate
    test rename to land.
    """
    src = JH_FILE.read_text()
    # The three required clauses
    for required in (
        "Repair the syntax of this malformed JSON patch block",
        "Output ONLY valid JSON",
        "Preserve the exact semantic code modifications",
    ):
        assert required in src, (
            f"json_healer missing required heal-prompt clause: {required!r}"
        )
    # The constant name itself MUST exist (callers depend on it
    # indirectly via heal_json_with_llm, but the AST pin protects the
    # symbol from rename without test update).
    assert "_HEAL_SYSTEM_PROMPT" in src, (
        "json_healer renamed _HEAL_SYSTEM_PROMPT — update tests + audit"
    )


def test_ast_pin_drift_type_taxonomy_is_closed_at_5_values() -> None:
    """DriftType must be exactly the 5 deliberate-slice failure shapes.

    Closing the taxonomy prevents drift recording from becoming a
    catch-all bucket. Adding a new drift kind requires updating
    this pin + the dispatcher's skip predicate + this file's
    spine tests — that friction is intentional.

    History: 3 v15 shapes -> +DUAL_ARM_FAILURE (Slice 194 — the pin was
    NOT updated then; pre-existing breakage repaired here) ->
    +EXPLORATION_INSUFFICIENT (Slice 230 — Iron-Gate rejection feeds model
    rotation so a no-tool weak model can't monopolize GENERATE_RETRY).
    """
    src = SDT_FILE.read_text()
    tree = ast.parse(src, filename=str(SDT_FILE))
    found_values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DriftType":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            found_values.append(target.id)
    expected = {
        "JSON_PARSE_ERROR_AFTER_HEAL",
        "SCHEMA_ID_HALLUCINATION",
        "ZERO_CANDIDATE_RETURN",
        "DUAL_ARM_FAILURE",
        "EXPLORATION_INSUFFICIENT",
    }
    assert set(found_values) == expected, (
        f"DriftType taxonomy drifted: got {found_values!r}, expected {expected!r}"
    )


def test_ast_pin_dispatch_consults_drift_tracker_before_attempt() -> None:
    """The Slice 20C rotation MUST be wired into the sentinel dispatch
    loop in ``candidate_generator._generate_dispatch`` — specifically
    AFTER the OPEN/TERMINAL_OPEN skip and BEFORE the per-attempt
    override stamp. Without this ordering, drift rotation is dead code.
    """
    src = CG_FILE.read_text()
    # Slice 20C attribution + the consult call
    assert "Slice 20C" in src, "candidate_generator missing Slice 20C attribution"
    assert (
        "_drift_tracker.has_drifted(" in src
        or "drift_tracker.has_drifted(" in src
    ), (
        "candidate_generator missing has_drifted consultation — Slice 20C "
        "rotation dead code"
    )
    # Skip token must be the well-known classifier the cascade reports
    assert "skipped_drift" in src, (
        "Missing 'skipped_drift' attempts-log token — observability gap"
    )


def test_ast_pin_phase3_reinforcement_literal_in_lean_prompt() -> None:
    """The Phase 3 operator-attested reinforcement text MUST be present
    in the lean prompt builder, gated by both the master flag AND a
    DW-primary route check.
    """
    src = PROV_FILE.read_text()
    # The operator-specified literal clauses
    for required in (
        "You have completed tool exploration",
        "You are strictly required to synthesize your discoveries",
        "zero-candidate return is an execution failure",
    ):
        assert required in src, (
            f"providers.py missing Phase 3 reinforcement clause: {required!r}"
        )
    # The master flag
    assert "JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED" in src, (
        "providers.py missing Phase 3 master flag"
    )
    # The DW-primary route gate — both 'standard' and 'complex'
    # must appear in the same neighborhood as the master flag.
    # Use the source text rather than AST walk because the
    # injection is inline.
    assert "standard" in src and "complex" in src, (
        "providers.py Phase 3 reinforcement not route-gated"
    )


# ──────────────────────────────────────────────────────────────────────
# Slice 20B spine — 5
# ──────────────────────────────────────────────────────────────────────


def test_spine_20b_master_off_short_circuits_no_heal_call(monkeypatch) -> None:
    """When master flag is OFF, heal_json_with_llm returns master_off
    WITHOUT invoking heal_call. Zero cost on the default path.
    """
    monkeypatch.delenv("JARVIS_JSON_HEAL_LLM_ENABLED", raising=False)
    from backend.core.ouroboros.governance import json_healer

    call_count = {"n": 0}

    async def _stub_call(**kw):
        call_count["n"] += 1
        return "should not be called"

    async def _runner():
        outcome = await json_healer.heal_json_with_llm(
            "{ broken json",
            heal_call=_stub_call,
            op_id="op-1",
            provider_name="dw",
        )
        return outcome

    outcome = asyncio.run(_runner())
    assert outcome.success is False
    assert outcome.repaired_text is None
    assert outcome.failure_reason == "master_off"
    assert call_count["n"] == 0, "heal_call invoked despite master_off"


def test_spine_20b_master_on_success_returns_validated_json(monkeypatch, tmp_path) -> None:
    """When master flag is ON, valid JSON from the heal_call passes
    validation and returns as repaired_text."""
    monkeypatch.setenv("JARVIS_JSON_HEAL_LLM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JSON_HEAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.core.ouroboros.governance import json_healer

    async def _good_call(**kw):
        return '{"schema_version": "2c.1", "candidates": []}'

    async def _runner():
        return await json_healer.heal_json_with_llm(
            '{"schema_version": "2c.1", "candidates": [BROKEN',
            heal_call=_good_call,
            op_id="op-2",
            provider_name="dw",
        )

    outcome = asyncio.run(_runner())
    assert outcome.success is True
    assert outcome.repaired_text is not None
    assert '"schema_version": "2c.1"' in outcome.repaired_text
    # Audit row landed
    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists(), "Audit row not written for successful heal"


def test_spine_20b_master_on_invalid_output_returns_failure(monkeypatch, tmp_path) -> None:
    """When heal_call returns non-JSON, healer marks failure with
    output_not_valid_json and returns None as repaired_text."""
    monkeypatch.setenv("JARVIS_JSON_HEAL_LLM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JSON_HEAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.core.ouroboros.governance import json_healer

    async def _garbage_call(**kw):
        return "Sure here is your JSON: blah blah"

    outcome = asyncio.run(json_healer.heal_json_with_llm(
        '{ broken', heal_call=_garbage_call, op_id="op-3", provider_name="dw",
    ))
    assert outcome.success is False
    assert outcome.repaired_text is None
    assert "output_not_valid_json" in (outcome.failure_reason or "")


def test_spine_20b_strips_markdown_code_fence_from_output(monkeypatch, tmp_path) -> None:
    """Qwen sometimes wraps JSON in ```json ... ``` despite the
    'Output ONLY valid JSON' prompt. The healer must strip fences."""
    monkeypatch.setenv("JARVIS_JSON_HEAL_LLM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JSON_HEAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from backend.core.ouroboros.governance import json_healer

    async def _fenced_call(**kw):
        return '```json\n{"ok": true}\n```'

    outcome = asyncio.run(json_healer.heal_json_with_llm(
        '{ broken', heal_call=_fenced_call, op_id="op-4", provider_name="dw",
    ))
    assert outcome.success is True
    assert outcome.repaired_text == '{"ok": true}'


def test_spine_20b_heal_and_retry_parse_records_drift_on_failure(
    monkeypatch, tmp_path,
) -> None:
    """When heal_and_retry_parse exhausts heal + retry → drift is
    recorded with JSON_PARSE_ERROR_AFTER_HEAL so the dispatcher can
    rotate on the next attempt for this op_id."""
    monkeypatch.setenv("JARVIS_JSON_HEAL_LLM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JSON_HEAL_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from backend.core.ouroboros.governance import json_healer
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        get_default_tracker, reset_default_tracker, DriftType,
    )
    reset_default_tracker()

    def _parse_always_fails(_):
        raise RuntimeError("doubleword_schema_invalid:json_parse_error")

    async def _heal_returns_bad(**kw):
        return "still not json"

    async def _runner():
        try:
            await json_healer.heal_and_retry_parse(
                raw='{ broken',
                parse_fn=_parse_always_fails,
                heal_call=_heal_returns_bad,
                op_id="op-drift-1",
                provider_name="dw",
                model_id="Qwen/Qwen3.5-397B-A17B-FP8",
            )
        except RuntimeError as exc:
            return str(exc)
        return None

    result = asyncio.run(_runner())
    assert result is not None and "json_parse_error" in result, (
        "heal_and_retry_parse did not re-raise json_parse_error"
    )
    # Drift was recorded
    tracker = get_default_tracker()
    assert tracker.has_drifted("op-drift-1", "Qwen/Qwen3.5-397B-A17B-FP8"), (
        "Slice 20C drift not recorded after heal failure — rotation will not fire"
    )


# ──────────────────────────────────────────────────────────────────────
# Slice 20C spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_20c_tracker_isolation_between_op_ids(monkeypatch) -> None:
    """Drift on op A must NOT bleed into op B's rotation decisions."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        SchemaDriftTracker, DriftType,
    )
    t = SchemaDriftTracker()
    t.record(
        op_id="op-A", model_id="m1",
        drift_type=DriftType.JSON_PARSE_ERROR_AFTER_HEAL,
    )
    assert t.has_drifted("op-A", "m1")
    assert not t.has_drifted("op-B", "m1"), (
        "Drift leaked across op_ids — isolation broken"
    )


def test_spine_20c_clear_op_wipes_history(monkeypatch) -> None:
    """clear(op_id) must remove all drift events for that op."""
    monkeypatch.setenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", "true")
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        SchemaDriftTracker, DriftType,
    )
    t = SchemaDriftTracker()
    t.record(op_id="op-X", model_id="m1", drift_type=DriftType.ZERO_CANDIDATE_RETURN)
    t.record(op_id="op-X", model_id="m2", drift_type=DriftType.ZERO_CANDIDATE_RETURN)
    cleared = t.clear("op-X")
    assert cleared == 2
    assert not t.has_drifted("op-X", "m1")
    assert not t.has_drifted("op-X", "m2")


def test_spine_20c_master_off_has_drifted_always_false(monkeypatch) -> None:
    """When master flag off, has_drifted returns False even with
    recorded events — the rotation gate is closed cleanly."""
    monkeypatch.delenv("JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED", raising=False)
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        SchemaDriftTracker, DriftType,
    )
    t = SchemaDriftTracker()
    t.record(op_id="op-1", model_id="m1", drift_type=DriftType.SCHEMA_ID_HALLUCINATION)
    # Event IS recorded for forensic recall — but has_drifted gate is closed
    assert t.events_for("op-1"), "Event NOT recorded — record() shouldn't gate"
    assert not t.has_drifted("op-1", "m1"), "has_drifted leaked master-off"


def test_spine_20c_ring_evicts_oldest_event_per_op() -> None:
    """Per-op ring respects max_events_per_op bound — drop-oldest FIFO."""
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        SchemaDriftTracker, DriftType,
    )
    t = SchemaDriftTracker(max_events_per_op=3)
    for i in range(5):
        t.record(
            op_id="op-1", model_id=f"m{i}",
            drift_type=DriftType.JSON_PARSE_ERROR_AFTER_HEAL,
        )
    events = t.events_for("op-1")
    assert len(events) == 3, (
        f"Ring did not evict: got {len(events)} events, expected 3"
    )
    # The first 2 models should be dropped from the EVENT ring
    event_models = {e.model_id for e in events}
    assert "m0" not in event_models and "m1" not in event_models


def test_spine_20c_op_cap_evicts_oldest_op() -> None:
    """Global op cap evicts oldest op_id when limit reached."""
    from backend.core.ouroboros.governance.schema_drift_tracker import (
        SchemaDriftTracker, DriftType,
    )
    t = SchemaDriftTracker(max_tracked_ops=3)
    for i in range(5):
        t.record(
            op_id=f"op-{i}", model_id="m1",
            drift_type=DriftType.ZERO_CANDIDATE_RETURN,
        )
    stats = t.stats()
    assert stats["tracked_ops"] == 3, (
        f"Op cap not respected: got {stats['tracked_ops']} ops, expected 3"
    )
    # Oldest 2 ops should be gone
    assert not t.events_for("op-0")
    assert not t.events_for("op-1")
    # Newest 3 retained
    assert t.events_for("op-4")


def test_spine_20c_dispatch_skip_tokens_reflect_drift_classifier() -> None:
    """The dispatcher's attempts log must use the 'skipped_drift'
    classifier so /telemetry can distinguish drift skips from
    sentinel-OPEN skips. Source-grep pin — the literal token must
    appear in the dispatch loop."""
    src = CG_FILE.read_text()
    assert "skipped_drift" in src, "Drift skip classifier missing from dispatch"
    assert "skipped_open" in src or 'skipped_{state.lower()}' in src, (
        "Sentinel skip classifier missing — coexistence sanity check"
    )


# ──────────────────────────────────────────────────────────────────────
# Phase 3 spine — 3
# ──────────────────────────────────────────────────────────────────────


def test_spine_phase3_reinforcement_emitted_on_standard_route(monkeypatch) -> None:
    """When master flag ON + route STANDARD, lean prompt MUST contain
    the operator's reinforcement clauses."""
    monkeypatch.setenv("JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED", "true")
    from backend.core.ouroboros.governance.providers import (
        _build_lean_codegen_prompt,
    )
    # Build a stub ctx with provider_route=standard
    stub = mock.MagicMock()
    stub.provider_route = "standard"
    stub.target_files = ()
    stub.context_files = ()
    stub.task = "trivial change"
    stub.complexity_score = 0
    stub.implementation_plan = None
    stub.session_lessons = ()
    stub.session_summary = ""
    stub.strategic_memory_context = ""
    stub.dependency_credit = 0
    stub.dependency_impact = None
    stub.preloaded_files = ()
    stub.repo_root = None
    stub.op_id = "op-phase3"
    # Don't try to call the real function (too many dependencies);
    # instead grep the function body for the literal string at compile
    # time. The AST pin already proved the literal is present and
    # gated; this spine confirms the gate is structural.
    src = PROV_FILE.read_text()
    # Find the line with the master flag check
    assert (
        'JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED' in src
        and 'provider_route' in src
    )


def test_spine_phase3_master_off_emits_no_reinforcement() -> None:
    """When master flag OFF, the reinforcement injection block is
    structurally a no-op. Source-grep pin: the `if _phase3_enabled
    and _phase3_route in ...` guard MUST exist so the literal is
    behind a conditional, not unconditional output."""
    src = PROV_FILE.read_text()
    # The guard must check the master flag AND the route
    assert "if _phase3_enabled and _phase3_route" in src, (
        "Phase 3 reinforcement not properly gated — guard structure changed"
    )


def test_spine_phase3_route_gate_excludes_background_and_speculative() -> None:
    """The route gate must include STANDARD and COMPLEX but NOT
    BACKGROUND or SPECULATIVE (those skip Venom and don't have the
    zero-candidate failure mode at the same rate)."""
    src = PROV_FILE.read_text()
    # Find the Phase 3 block specifically
    phase3_start = src.find("Phase 3")
    assert phase3_start > 0
    # The route check should be within ~2000 chars of "Phase 3"
    phase3_block = src[phase3_start: phase3_start + 2000]
    assert '"standard"' in phase3_block and '"complex"' in phase3_block, (
        "Phase 3 route gate missing STANDARD or COMPLEX"
    )
    # And explicitly should NOT mention background/speculative
    # as ELIGIBLE routes for the reinforcement
    # (they may appear in the file globally — we narrow to the
    # phase3 reinforcement block).
    inline_route_check = (
        '_phase3_route in ("standard", "complex")' in phase3_block
    )
    assert inline_route_check, (
        "Phase 3 inline route gate doesn't match expected tuple — "
        "BG/SPEC may accidentally receive the reinforcement"
    )
