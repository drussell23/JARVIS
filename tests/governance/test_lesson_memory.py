"""LessonMemory — cross-op, module-keyed lessons over the failure-mode memory.

Root cause covered: the graduated §31.4 arc had no production recorder, a
retriever that compared the query with itself, and records with no target
files — so every op relearned every lesson. These tests pin recording at
the VALIDATE/VERIFY seams, module-keyed retrieval, the immutable prompt
block, the RAG hook, and graceful degradation on a locked/corrupt store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance import failure_mode_memory as fmm
from backend.core.ouroboros.governance import lesson_memory as LM

_LOG = "Ouroboros.LessonMemory"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(fmm, "history_dir", lambda: tmp_path / "fmm")
    for env in (LM._ENV_ENABLED, LM._ENV_TOP_K, LM._ENV_MAX_CHARS, LM._ENV_TIMEOUT,
                LM._ENV_MIN_WEIGHT, LM._ENV_HALFLIFE, LM._ENV_EVIDENCE, LM._ENV_SITUATION_REL,
                "JARVIS_FAILURE_MODE_MEMORY_ENABLED"):
        monkeypatch.delenv(env, raising=False)
    yield


@dataclass(frozen=True)
class Ctx:
    op_id: str = "op-1"
    target_files: Tuple[str, ...] = ("tests/governance/test_model_physics.py",)
    strategic_intent_id: str = ""
    strategic_memory_fact_ids: Tuple[str, ...] = ()
    strategic_memory_prompt: str = ""
    strategic_memory_digest: str = ""

    def with_strategic_memory_context(self, **kw):
        return replace(self, **kw)


def _run(c):
    return asyncio.run(c)


def _record(files=("tests/governance/test_model_physics.py",), error="AssertionError: assert None is not None", summary="test_parse_valid", phase="VALIDATE", op="op-1"):
    return _run(LM.record_lesson(op_id=op, target_files=files, phase=phase, failure_class="test", error_text=error, summary=summary))


# -- taxonomy ---------------------------------------------------------------

@pytest.mark.parametrize("text,cls", [
    ("no tests ran in 0.31s", "no_tests_collected"),
    ("TypeError: parse_model_physics() takes 1 positional argument but 3 were given", "api_signature_mismatch"),
    ("AttributeError: module 'model_physics' has no attribute 'effective'", "api_signature_mismatch"),
    ("AssertionError: assert None is not None", "input_shape_mismatch"),
    ("AssertionError: assert 524288 == 16384", "expected_value_mismatch"),
    ("AssertionError: assert <OracleVerdict.HEALTHY: 'healthy'> == <OracleVerdict.FAILED: 'failed'>", "expected_value_mismatch"),
    ("assert ['Hello! It s...'] == ['Fix applied. Tests green.']", "expected_value_mismatch"),
    ("verify_regression: 3 previously passing tests failed", "verify_regression"),
    ("SyntaxError: invalid syntax", "syntax_error"),
    ("ModuleNotFoundError: No module named 'foo'", "import_error"),
    ("boom", "exception"),
])
def test_classify_error(text, cls):
    assert LM.classify_error(text) == cls


def test_module_keys_unify_test_and_impl():
    keys = LM.module_keys(("tests/governance/test_model_physics.py", "backend/core/x/model_physics.py", "tests/conftest.py"))
    assert keys == frozenset({"model_physics"})


def test_evidence_excerpt_is_bounded_and_clean():
    ex = LM.evidence_excerpt("\x1b[31mE   assert None is not None\x1b[0m\n=====\n" + "x" * 900, limit=60)
    assert ex.startswith("E assert None is not None") and len(ex) <= 60 and "\x1b" not in ex


# -- recording --------------------------------------------------------------

def test_record_persists_structured_lesson_and_index():
    assert _record() == "ok_new"
    recs = fmm.read_failure_mode_history()
    assert len(recs) == 1
    r = recs[0]
    assert r.error_class == "input_shape_mismatch" and r.phase == "VALIDATE"
    assert r.target_files == ("tests/governance/test_model_physics.py",)
    assert "assert None is not None" in r.lesson
    assert "input shape" in r.mitigation_summary
    idx = LM.index_path()
    assert idx.is_file() and "input_shape_mismatch" in idx.read_text()


def test_repeat_failure_merges_weight_and_keeps_lesson_fields():
    _record()
    _record(op="op-2", error="AssertionError: assert None is not None\nmore")
    recs = fmm.read_failure_mode_history()
    assert len(recs) == 1 and recs[0].weight == 2
    assert recs[0].target_files and recs[0].error_class == "input_shape_mismatch"


def test_legacy_row_without_lesson_fields_still_parses():
    rec = LM.build_lesson_record(op_id="o", target_files=("a.py",), phase="VALIDATE", failure_class="test", error_text="x")
    d = rec.to_dict()
    for k in ("target_files", "error_class", "lesson", "phase"):
        d.pop(k)
    legacy = fmm.FailureModeRecord.from_dict(d)
    assert legacy is not None and legacy.target_files == () and legacy.error_class == ""


def test_disabled_flag_records_nothing(monkeypatch):
    monkeypatch.setenv(LM._ENV_ENABLED, "false")
    assert _record() == "disabled"
    assert fmm.read_failure_mode_history() == ()


# -- retrieval --------------------------------------------------------------

def test_retrieval_is_module_keyed_across_test_impl_boundary():
    _record()
    hits = _run(LM.retrieve_lessons(("backend/core/ouroboros/governance/model_physics.py",)))
    assert len(hits) == 1 and hits[0].relevance == 1.0
    assert _run(LM.retrieve_lessons(("backend/core/other/unrelated_thing.py",))) == ()


def test_retriever_jaccard_uses_record_files():
    # the arc's retriever refuses the UNKNOWN sentinel — pin a known kind
    kind = fmm.SituationKind.MULTI_FILE_REFACTOR
    rec = replace(LM.build_lesson_record(op_id="o", target_files=("pkg/a.py",), phase="VALIDATE", failure_class="test", error_text="assert 1 == 2"), situation_kind=kind)
    fmm.record_failure_mode(rec)
    same = fmm.retrieve_failure_modes(situation_kind=kind, target_files=("pkg/a.py",), min_weight=1)
    other = fmm.retrieve_failure_modes(situation_kind=kind, target_files=("pkg/b.py",), min_weight=1)
    assert len(same) == 1 and same[0].jaccard_score == 1.0
    assert other == ()


def test_compose_block_is_bounded_and_ranked(monkeypatch):
    _record(error="AssertionError: assert 1 == 2", summary="test_a")
    _record(files=("tests/test_model_physics.py",), error="TypeError: f() takes 1 positional argument but 2 were given", summary="test_b")
    hits = _run(LM.retrieve_lessons(("tests/governance/test_model_physics.py",)))
    assert {h.record.error_class for h in hits} == {"expected_value_mismatch", "api_signature_mismatch"}
    block = LM.compose_lessons_block(hits)
    assert block.startswith(LM.SECTION_HEADER) and "Do instead:" in block
    small = LM.compose_lessons_block(hits, budget=len(LM.SECTION_HEADER) + 260)
    assert small == "" or small.count("Do instead:") == 1
    assert LM.compose_lessons_block(()) == ""


# -- RAG hook ---------------------------------------------------------------

def test_inject_lessons_stamps_immutable_block_on_strategic_channel(caplog):
    caplog.set_level(logging.INFO, logger=_LOG)
    _record()
    ctx = Ctx(strategic_memory_prompt="## Existing")
    out = _run(LM.inject_lessons(ctx))
    assert out is not ctx
    assert out.strategic_memory_prompt.startswith("## Existing\n\n" + LM.SECTION_HEADER)
    assert out.strategic_intent_id == LM.INTENT_ID and out.strategic_memory_fact_ids[0].startswith("lesson:")
    assert out.strategic_memory_digest and len(out.strategic_memory_digest) == 64
    # idempotent: a second pass does not double-inject
    assert _run(LM.inject_lessons(out)) is out
    assert any("injected 1 lesson" in r.getMessage() for r in caplog.records)


def test_inject_lessons_returns_same_context_when_nothing_matches():
    ctx = Ctx(target_files=("x/y.py",))
    assert _run(LM.inject_lessons(ctx)) is ctx


def test_corrupt_store_lines_are_skipped_not_fatal(tmp_path):
    _record()
    p = fmm.history_path()
    p.write_text("{not json\n" + p.read_text() + "\x00garbage\n", encoding="utf-8")
    hits = _run(LM.retrieve_lessons(("tests/governance/test_model_physics.py",)))
    assert len(hits) == 1


def test_locked_or_failing_store_degrades_with_telemetry_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_LOG)
    _record()

    def _boom():
        raise OSError("database is locked")

    monkeypatch.setattr(fmm, "read_failure_mode_history", _boom)
    ctx = Ctx()
    assert _run(LM.inject_lessons(ctx)) is ctx
    assert any("degraded" in r.getMessage() and "base anchor" in r.getMessage() for r in caplog.records)


def test_slow_store_times_out_and_degrades(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=_LOG)
    monkeypatch.setenv(LM._ENV_TIMEOUT, "0.2")

    def _slow(*a, **k):
        time.sleep(1.0)
        return ()

    monkeypatch.setattr(LM, "retrieve_lessons_sync", _slow)
    ctx = Ctx()
    assert _run(LM.inject_lessons(ctx)) is ctx
    assert any("timed out" in r.getMessage() for r in caplog.records)


def test_hook_is_wired_into_candidate_generator_and_orchestrator_seams():
    import inspect
    from backend.core.ouroboros.governance import candidate_generator as cg, orchestrator as orch
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner as s4b
    assert "inject_lessons" in inspect.getsource(cg.CandidateGenerator.generate)
    assert "record_lesson" in inspect.getsource(orch.Orchestrator._run_validation)
    assert "_goal_binding_kwargs" in inspect.getsource(s4b)


def test_fsm_resume_envelope_carries_goal_binding():
    from backend.core.ouroboros.governance.intake.unified_intake_router import _resume_envelope_kwargs
    env = {"op_id": "op-x", "target_files": ["a.py"], "resume_phase": "APPLY",
           "intake_evidence_json": json.dumps({"goal_id": "g-1", "goal_digest": "d" * 64, "success_criteria": "not carried"})}
    kw = _resume_envelope_kwargs(env)
    ev = kw["evidence"]
    assert ev["goal_id"] == "g-1" and ev["goal_digest"] == "d" * 64 and "success_criteria" not in ev
    assert json.loads(ev["intake_evidence_json"])["goal_id"] == "g-1"
    plain = _resume_envelope_kwargs({"op_id": "op-y", "intake_evidence_json": "not json"})
    assert "goal_id" not in plain["evidence"]
