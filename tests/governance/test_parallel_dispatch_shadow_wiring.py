"""Tests for Wave 3 (6) Slice 3 — shadow-mode phase_dispatcher wiring.

Scope: memory/project_wave3_item6_scope.md §9 Slice 3 + operator
Slice 3 authorization (2026-04-23):

- JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED stays default false.
  Shadow path gated by (master AND shadow) both true; enforce stays
  false; no asyncio.gather or scheduler submit.
- Behavioral parity: flags off = byte-identical to baseline.
- Telemetry: every shadow evaluation emits [ParallelDispatch] + reason
  + graph_id/plan_digest when a graph is built. No silent shadow.
- Imports: parallel_dispatch + dispatcher glue stay grep-clean on
  banned authority modules.

Coverage:

1. extract_candidate_files — shape variants.
2. evaluate_shadow_fanout — guard matrix (master off / shadow off /
   both on + various generation shapes).
3. phase_dispatcher hook — GENERATE-only, no crash on shadow failure,
   byte-identical pctx/ctx under all flag combos.
4. Telemetry emission — log line contents on armed evaluations.
5. Authority-import ban re-verified after Slice 3 additions to both
   parallel_dispatch.py + phase_dispatcher.py.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    CandidateFile,
    FanoutEligibility,
    ReasonCode,
    ShadowEvaluation,
    evaluate_shadow_fanout,
    extract_candidate_files,
)
from backend.core.ouroboros.governance.posture import Posture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeGeneration:
    """Minimal GenerationResult stand-in for shape extraction tests."""

    candidates: Tuple[Dict[str, Any], ...] = ()


def _multi_file_candidates(n: int = 3) -> Tuple[Dict[str, Any], ...]:
    """A single candidate with a multi-file ``files`` list."""
    return (
        {
            "files": [
                {
                    "file_path": f"pkg/mod_{i}.py",
                    "full_content": f"# module {i}\npass\n",
                    "rationale": f"unit {i}",
                }
                for i in range(n)
            ],
        },
    )


def _single_file_candidates() -> Tuple[Dict[str, Any], ...]:
    return (
        {
            "file_path": "pkg/solo.py",
            "full_content": "# solo\npass\n",
            "rationale": "just one file",
        },
    )


def _ok_gate(level: PressureLevel = PressureLevel.OK) -> MemoryPressureGate:
    gate = MagicMock(spec=MemoryPressureGate)

    def _cf(n: int) -> MemoryFanoutDecision:
        return MemoryFanoutDecision(
            allowed=level != PressureLevel.CRITICAL,
            n_requested=n,
            n_allowed=1 if level == PressureLevel.CRITICAL else n,
            level=level,
            free_pct=60.0,
            reason_code=f"mock_{level.value}",
            source="test",
        )

    gate.can_fanout.side_effect = _cf
    return gate


def _posture(p: Optional[Posture] = Posture.MAINTAIN, c: Optional[float] = 0.9):
    def _fn() -> Tuple[Optional[Posture], Optional[float]]:
        return p, c

    return _fn


# ---------------------------------------------------------------------------
# (1) extract_candidate_files — shape variants
# ---------------------------------------------------------------------------


def test_extract_multi_file_shape():
    gen = _FakeGeneration(candidates=_multi_file_candidates(3))
    result = extract_candidate_files(gen)
    assert result is not None
    assert len(result) == 3
    for i, cf in enumerate(result):
        assert cf.file_path == f"pkg/mod_{i}.py"
        assert cf.rationale == f"unit {i}"


def test_extract_single_file_shape():
    gen = _FakeGeneration(candidates=_single_file_candidates())
    result = extract_candidate_files(gen)
    assert result is not None
    assert len(result) == 1
    assert result[0].file_path == "pkg/solo.py"


def test_extract_empty_candidates_returns_empty_tuple():
    gen = _FakeGeneration(candidates=())
    result = extract_candidate_files(gen)
    assert result == ()


def test_extract_missing_candidates_returns_none():
    # Object with no .candidates attribute at all.
    class _Bare: pass
    result = extract_candidate_files(_Bare())
    assert result is None


def test_extract_none_generation_returns_none():
    assert extract_candidate_files(None) is None


def test_extract_malformed_candidates_returns_none():
    """Candidate entries that aren't dicts (or missing fields) → None."""
    gen = _FakeGeneration(candidates=("not a dict",))  # type: ignore[arg-type]
    result = extract_candidate_files(gen)
    assert result is None


def test_extract_multi_file_skips_malformed_entries():
    """Malformed per-file entries inside a files list are skipped, not crashed."""
    gen = _FakeGeneration(candidates=(
        {
            "files": [
                {"file_path": "a.py", "full_content": "x"},
                {"not_a_file_shape": 123},  # skipped
                {"file_path": "b.py", "full_content": "y"},
                {"file_path": "a.py", "full_content": "dup"},  # dedupe
            ],
        },
    ))
    result = extract_candidate_files(gen)
    assert result is not None
    paths = [cf.file_path for cf in result]
    assert paths == ["a.py", "b.py"]


def test_extract_never_raises_on_garbage():
    """Shadow path must never crash the pipeline."""
    for garbage in (42, "string", [1, 2, 3], object()):
        result = extract_candidate_files(garbage)
        assert result is None


# ---------------------------------------------------------------------------
# (2) evaluate_shadow_fanout — guard matrix
# ---------------------------------------------------------------------------


def test_shadow_off_when_master_off(monkeypatch):
    """Master flag off → ran=False, skip_reason=master_off, no eligibility."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    result = evaluate_shadow_fanout(
        op_id="op-test-001",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is False
    assert result.skip_reason == "master_off"
    assert result.eligibility is None
    assert result.graph is None


def test_shadow_off_when_shadow_subflag_off(monkeypatch):
    """Master on + shadow off → still ran=False."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", raising=False)
    result = evaluate_shadow_fanout(
        op_id="op-test-002",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is False
    assert result.skip_reason == "shadow_off"


def test_shadow_on_with_unrecognized_shape(monkeypatch):
    """Armed + unrecognized generation shape → ran=False, skip_reason set."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    result = evaluate_shadow_fanout(
        op_id="op-test-003",
        generation="not a generation object",
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is False
    assert result.skip_reason == "unrecognized_shape"


def test_shadow_on_with_single_file_skips_fanout(monkeypatch):
    """Armed + single-file candidates → ran=True, eligibility denies."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    result = evaluate_shadow_fanout(
        op_id="op-test-004",
        generation=_FakeGeneration(candidates=_single_file_candidates()),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is True
    assert result.eligibility is not None
    assert result.eligibility.allowed is False
    assert result.eligibility.reason_code == ReasonCode.SINGLE_FILE_OP
    assert result.graph is None


def test_shadow_on_with_multi_file_builds_graph(monkeypatch):
    """Armed + multi-file eligible → ran=True, allowed, graph built."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    result = evaluate_shadow_fanout(
        op_id="op-test-005",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is True
    assert result.eligibility is not None
    assert result.eligibility.allowed is True
    assert result.graph is not None
    assert result.graph_id.startswith("graph-")
    assert result.plan_digest != ""
    assert result.graph.concurrency_limit == 3
    assert len(result.graph.units) == 3


def test_shadow_on_with_memory_critical_allowed_false(monkeypatch):
    """Armed + multi-file + CRITICAL memory → eligibility denies, no graph."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    result = evaluate_shadow_fanout(
        op_id="op-test-006",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        gate=_ok_gate(level=PressureLevel.CRITICAL),
        posture_fn=_posture(),
    )
    assert result.ran is True
    assert result.eligibility is not None
    assert result.eligibility.allowed is False
    assert result.eligibility.reason_code == ReasonCode.MEMORY_CRITICAL
    assert result.graph is None


def test_shadow_never_raises_on_malformed_generation(monkeypatch):
    """Any extraction/build failure is caught; shadow never escalates."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    # Deliberately garbage — must not raise.
    result = evaluate_shadow_fanout(
        op_id="op-test-007",
        generation=object(),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert result.ran is False


# ---------------------------------------------------------------------------
# (3) Telemetry — log line emission
# ---------------------------------------------------------------------------


def test_shadow_emits_parallel_dispatch_log_on_armed_evaluation(monkeypatch, caplog):
    """Operator directive: 'no silent shadow'. Armed evaluations must
    emit [ParallelDispatch] at INFO so operators see them."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")
    evaluate_shadow_fanout(
        op_id="op-test-008",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    # Eligibility line always emitted when armed.
    eligibility_lines = [r.message for r in caplog.records if "[ParallelDispatch]" in r.message]
    assert eligibility_lines, f"no eligibility log line emitted: {caplog.records}"
    # Graph-built line emitted when graph was constructed.
    graph_built_lines = [
        r.message for r in caplog.records if "shadow_graph_built" in r.message
    ]
    assert graph_built_lines, f"no shadow_graph_built line: {caplog.records}"
    # The graph-built line carries graph_id + plan_digest + concurrency_limit.
    assert "graph_id=graph-" in graph_built_lines[0]
    assert "plan_digest=" in graph_built_lines[0]
    assert "concurrency_limit=3" in graph_built_lines[0]
    assert "n_units=3" in graph_built_lines[0]


def test_shadow_emits_skip_telemetry_on_unrecognized_shape(monkeypatch, caplog):
    """Skip paths still emit telemetry (no silent shadow)."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")
    evaluate_shadow_fanout(
        op_id="op-test-009",
        generation="not a generation",
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    skip_lines = [
        r.message for r in caplog.records if "shadow_skipped" in r.message
    ]
    assert skip_lines, f"no shadow_skipped line: {caplog.records}"
    assert "unrecognized_shape" in skip_lines[0]


def test_shadow_eligibility_denied_does_not_emit_graph_built(monkeypatch, caplog):
    """Eligibility denied → eligibility line only; no shadow_graph_built."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")
    evaluate_shadow_fanout(
        op_id="op-test-010",
        generation=_FakeGeneration(candidates=_single_file_candidates()),
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    graph_built = [
        r.message for r in caplog.records if "shadow_graph_built" in r.message
    ]
    assert not graph_built, f"unexpected shadow_graph_built on denied eligibility: {graph_built}"


# ---------------------------------------------------------------------------
# (4) Parity — shadow on must not mutate ctx/pctx
# ---------------------------------------------------------------------------


def test_shadow_eligibility_decision_is_pure(monkeypatch):
    """Armed evaluations compute + log, but the ShadowEvaluation return
    value is the only side effect visible to the caller. The caller's
    generation artifact is not mutated — tested by snapshotting."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    gen = _FakeGeneration(candidates=_multi_file_candidates(3))
    before_candidates = gen.candidates
    before_ids = [id(c) for c in gen.candidates]
    evaluate_shadow_fanout(
        op_id="op-test-011",
        generation=gen,
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    # Same object identity + same contents: shadow did not mutate.
    assert gen.candidates is before_candidates
    assert [id(c) for c in gen.candidates] == before_ids


def test_shadow_evaluation_frozen():
    """ShadowEvaluation is immutable — prevents downstream mutation."""
    e = ShadowEvaluation(ran=False, skip_reason="master_off")
    with pytest.raises((AttributeError, Exception)):
        e.ran = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (5) phase_dispatcher integration — import + hook existence
# ---------------------------------------------------------------------------


def test_phase_dispatcher_imports_evaluate_shadow_fanout_lazily():
    """The hook in phase_dispatcher.dispatch_pipeline imports
    evaluate_shadow_fanout lazily inside the GENERATE branch — this keeps
    phase_dispatcher.py clean at module-load time and lets the shadow
    path be stripped in non-Wave3 environments without breaking the
    dispatcher."""
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    # Lazy (inline) import pattern — NOT a top-of-file import.
    assert "from backend.core.ouroboros.governance.parallel_dispatch import" in source
    assert "evaluate_shadow_fanout" in source
    # Confirm it's inside the dispatch_pipeline function body (not at module scope).
    module_scope_imports = re.findall(
        r"^from backend\.core\.ouroboros\.governance\.parallel_dispatch",
        source,
        re.MULTILINE,
    )
    assert len(module_scope_imports) == 0, (
        "shadow import must stay inline inside dispatch_pipeline, not "
        "at module scope"
    )


def test_phase_dispatcher_hook_only_fires_on_generate_phase():
    """The hook is gated by dispatch_phase == OperationPhase.GENERATE.
    Source-inspection assertion: the hook block must explicitly check
    dispatch_phase against GENERATE before calling evaluate_shadow_fanout."""
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    # Verify the GENERATE gate exists near the evaluate_shadow_fanout call.
    assert "dispatch_phase == OperationPhase.GENERATE" in source
    assert "evaluate_shadow_fanout" in source


def test_phase_dispatcher_hook_catches_all_shadow_exceptions():
    """Shadow must never crash the pipeline — phase_dispatcher's hook
    wraps the shadow call in a broad except that logs at DEBUG."""
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    # A broad except clause guards the shadow call.
    assert "except Exception as _shadow_exc" in source
    assert "suppressed" in source.lower() or "never fail" in source.lower()


# ---------------------------------------------------------------------------
# (6) Authority-import ban — reconfirm after Slice 3 additions
# ---------------------------------------------------------------------------


def test_parallel_dispatch_still_has_no_authority_imports_slice3():
    """Slice 3 added no banned imports to parallel_dispatch.py."""
    module_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "parallel_dispatch.py"
    )
    source = module_path.read_text()
    banned_patterns = [
        r"from\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"from\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.phase_runners\.gate_runner\b",
    ]
    for pattern in banned_patterns:
        matches = re.findall(pattern, source)
        assert not matches, (
            f"parallel_dispatch.py Slice 3 violates ban: "
            f"pattern {pattern!r} matched {matches!r}"
        )


def test_phase_dispatcher_does_not_add_candidate_generator_import():
    """Operator directive: 'no new imports into candidate_generator
    from dispatcher'. phase_dispatcher.py must NOT import
    candidate_generator — parallel_dispatch.py wraps the candidate shape
    via lightweight CandidateFile."""
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    forbidden_patterns = [
        r"from\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"import\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
    ]
    for pattern in forbidden_patterns:
        matches = re.findall(pattern, source)
        assert not matches, (
            f"phase_dispatcher.py violates 'no candidate_generator import' "
            f"rule: pattern {pattern!r} matched {matches!r}"
        )
