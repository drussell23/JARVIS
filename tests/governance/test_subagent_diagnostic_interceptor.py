"""Subagent Diagnostic Interceptor — subagent failures learn, in the open.

22 failed L3 units on 2026-09-07 carried ``error="validation failed"`` and
nothing else, reached no WARNING and wrote no lesson. These tests pin the
contract that closes that gap for fixed-type subagents, L3 units and crashed
fan-outs alike: delegate first, classify deterministically, route into the
ONE LessonMemory substrate, never perturb the caller.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance import subagent_diagnostic_interceptor as sdi
from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentResult,
    SubagentStatus,
    SubagentType,
)


class _Inner:
    def __init__(self, raise_on_result: bool = False) -> None:
        self.events: List[str] = []
        self.raise_on_result = raise_on_result

    def emit_spawn(self, *_a: Any) -> None:
        self.events.append("spawn")

    def emit_result(self, *_a: Any) -> None:
        self.events.append("result")
        if self.raise_on_result:
            raise RuntimeError("inner sink broke")


class _Recorder:
    def __init__(self, raise_: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.raise_ = raise_

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.raise_:
            raise RuntimeError("store locked")
        return "recorded"


def _result(status: SubagentStatus, *, error_class: str = "", detail: str = "",
            subagent_type: SubagentType = SubagentType.REVIEW,
            files_read: tuple = ("backend/x/y.py",)) -> SubagentResult:
    return SubagentResult(
        subagent_id="op-1::sub-01", subagent_type=subagent_type, status=status,
        goal="review the candidate patch for regressions", files_read=files_read,
        error_class=error_class, error_detail=detail, provider_used="doubleword-local",
    )


# --------------------------------------------------------------------------
# classification is deterministic and shape-based
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,error_class,detail,expected", [
    (SubagentStatus.COMPLETED, "", "", None),
    (SubagentStatus.CANCELLED, "", "", None),
    (SubagentStatus.NOT_IMPLEMENTED, "NotImplementedYet", "scaffolding", None),
    (SubagentStatus.FAILED, "SubagentTimeout", "exceeded 90s", "subagent_timeout"),
    (SubagentStatus.FAILED, "SubagentSemanticFirewallRejection", "write outside scope", "cage_breach"),
    (SubagentStatus.FAILED, "BlockedPathError", "/etc/passwd", "cage_breach"),
    (SubagentStatus.FAILED, "MalformedReviewInput", "missing verdict", "schema_hallucination"),
    (SubagentStatus.FAILED, "InvalidPlanDag", "cycle", "schema_hallucination"),
    (SubagentStatus.DIVERSITY_REJECTED, "IronGateDiversityRejection", "1 tool", "diversity_rejected"),
    (SubagentStatus.BUDGET_EXHAUSTED, "", "", "budget_exhausted"),
    (SubagentStatus.FAILED, "RuntimeError", "call timed out after 60s", "subagent_timeout"),
    (SubagentStatus.FAILED, "TypeError", "TypeError: review() got an unexpected keyword argument 'x'", "api_signature_mismatch"),
    (SubagentStatus.FAILED, "ValueError", "boom", "exception"),
    (SubagentStatus.PARTIAL, "SubagentTimeout", "fallback merged", "subagent_timeout"),
])
def test_classify_subagent_failure(status, error_class, detail, expected) -> None:
    assert sdi.classify_subagent_failure(_result(status, error_class=error_class, detail=detail)) == expected


class _Unit:
    unit_id = "unit-1"
    goal = "author tests for speech_provider"
    target_files = ("tests/governance/comms/karen_synth/test_speech_provider.py",)


class _UnitResult:
    def __init__(self, status: str, failure_class: str, error: str) -> None:
        self.status = type("S", (), {"value": status})()
        self.failure_class = failure_class
        self.error = error


@pytest.mark.parametrize("status,fc,error,expected", [
    ("completed", "", "", None),
    ("cancelled", "cancelled", "cancelled", None),
    ("failed", "infra", "OSError: disk", "subagent_infra"),
    ("failed", "worktree_isolation", "worktree_create_failed:RuntimeError:x", "worktree_isolation"),
    ("failed", "budget", "pipeline budget exhausted", "budget_exhausted"),
    ("failed", "security", "escapes the unit worktree", "cage_breach"),
    ("failed", "syntax", "SyntaxError: bad", "syntax_error"),
    ("failed", "test", "python test; 1 failed: tests/t.py::test_a | assert [\"Hello\"] == ['Fix']", "expected_value_mismatch"),
    ("failed", "test", "python infra; timed out", "timeout"),
    ("failed", "test", "validation failed", "exception"),
])
def test_classify_unit_failure(status, fc, error, expected) -> None:
    assert sdi.classify_unit_failure(_UnitResult(status, fc, error)) == expected


# --------------------------------------------------------------------------
# the sink delegates FIRST and routes failures into the lesson store
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sink_delegates_then_records_failure_with_shared_schema() -> None:
    inner, rec = _Inner(), _Recorder()
    sink = sdi.SubagentDiagnosticSink(inner, recorder=rec)
    res = _result(SubagentStatus.FAILED, error_class="SubagentTimeout", detail="exceeded 90s")
    sink.emit_spawn("op-1", res.subagent_id, SubagentType.REVIEW, res.goal)
    sink.emit_result("op-1", res.subagent_id, res)
    await sink.drain()
    assert inner.events == ["spawn", "result"]
    assert len(rec.calls) == 1
    kw = rec.calls[0]
    assert kw["op_id"] == "op-1"
    assert kw["phase"] == "SUBAGENT_REVIEW"
    assert kw["error_class"] == "subagent_timeout"
    assert kw["failure_class"] == "failed"
    assert kw["target_files"] == ("backend/x/y.py",)
    assert "SubagentTimeout" in kw["error_text"] and "exceeded 90s" in kw["error_text"]
    assert kw["summary"].startswith("review the candidate")


@pytest.mark.asyncio
async def test_sink_ignores_success_and_wiring_placeholders() -> None:
    rec = _Recorder()
    sink = sdi.SubagentDiagnosticSink(_Inner(), recorder=rec)
    for st in (SubagentStatus.COMPLETED, SubagentStatus.CANCELLED, SubagentStatus.NOT_IMPLEMENTED):
        sink.emit_result("op-1", "s", _result(st))
    await sink.drain()
    assert rec.calls == []


@pytest.mark.asyncio
async def test_sink_survives_inner_and_store_faults() -> None:
    """A broken inner sink or a locked store never reaches the orchestrator."""
    inner, rec = _Inner(raise_on_result=True), _Recorder(raise_=True)
    sink = sdi.SubagentDiagnosticSink(inner, recorder=rec)
    sink.emit_result("op-1", "s", _result(SubagentStatus.FAILED, error_class="ValueError", detail="x"))
    await sink.drain()
    assert inner.events == ["result"]
    assert len(rec.calls) == 1  # attempted, failed quietly


def test_sink_without_running_loop_does_not_raise() -> None:
    sink = sdi.SubagentDiagnosticSink(_Inner(), recorder=_Recorder())
    sink.emit_result("op-1", "s", _result(SubagentStatus.FAILED, error_class="ValueError", detail="x"))
    assert sink.pending == set()


def test_disabled_flag_returns_inner_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SUBAGENT_DIAGNOSTICS_ENABLED", "0")
    inner = _Inner()
    assert sdi.wrap_subagent_diagnostics(inner) is inner
    monkeypatch.setenv("JARVIS_SUBAGENT_DIAGNOSTICS_ENABLED", "1")
    assert isinstance(sdi.wrap_subagent_diagnostics(inner), sdi.SubagentDiagnosticSink)


# --------------------------------------------------------------------------
# every failure is loud in a headless soak
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_emits_one_warning_line(caplog) -> None:
    rec = _Recorder()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.SubagentDiagnostics"):
        out = await sdi.record_subagent_failure(
            "op-1", _result(SubagentStatus.FAILED, error_class="SubagentTimeout", detail="90s"), recorder=rec,
        )
    assert out == "recorded"
    lines = [r for r in caplog.records if "[SubagentDiagnostics]" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert "class=subagent_timeout" in lines[0].getMessage()


@pytest.mark.asyncio
async def test_unit_failure_routes_with_unit_files_and_evidence(caplog) -> None:
    rec = _Recorder()
    ur = _UnitResult("failed", "test", "python test; 1 failed: tests/t.py::test_dw | assert ['Hello'] == ['Fix']")
    with caplog.at_level(logging.WARNING, logger="Ouroboros.SubagentDiagnostics"):
        out = await sdi.record_unit_failure("op-9", _Unit(), ur, recorder=rec)
    assert out == "recorded"
    kw = rec.calls[0]
    assert kw["phase"] == sdi.PHASE_UNIT
    assert kw["target_files"] == _Unit.target_files
    assert kw["error_class"] == "expected_value_mismatch"
    assert "tests/t.py::test_dw" in kw["error_text"]
    assert any("unit failed op=op-9" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unit_success_and_cancel_are_skipped() -> None:
    rec = _Recorder()
    assert await sdi.record_unit_failure("op", _Unit(), _UnitResult("completed", "", ""), recorder=rec) == "skipped"
    assert await sdi.record_unit_failure("op", _Unit(), _UnitResult("cancelled", "cancelled", "cancelled"), recorder=rec) == "skipped"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_fanout_crash_routes_as_its_own_class() -> None:
    rec = _Recorder()
    out = await sdi.record_fanout_crash("op-3", ValueError("graph has a cycle"), target_files=("a.py",), recorder=rec)
    assert out == "recorded"
    kw = rec.calls[0]
    assert kw["phase"] == sdi.PHASE_FANOUT and kw["error_class"] == "fanout_crash"
    assert "ValueError: graph has a cycle" == kw["error_text"]


# --------------------------------------------------------------------------
# the real store accepts the record (no schema fork)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_records_land_in_the_real_lesson_store(tmp_path, monkeypatch) -> None:
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    from backend.core.ouroboros.governance import lesson_memory as lm
    monkeypatch.setenv("JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path / "fm"))
    out = await sdi.record_subagent_failure(
        "op-real", _result(SubagentStatus.FAILED, error_class="SubagentTimeout", detail="exceeded 90s"),
    )
    assert out not in ("error", "unavailable", "disabled"), out
    matches = await lm.retrieve_lessons(["backend/x/y.py"])
    assert any(getattr(m.record, "error_class", "") == "subagent_timeout" for m in matches), [
        getattr(m.record, "error_class", "") for m in matches
    ]
    assert lm.compose_lessons_block(matches)
