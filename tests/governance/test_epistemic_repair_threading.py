"""Adaptive Epistemic Feedback Matrix — Task T2.

Threading verification: the pure ``epistemic_feedback`` primitives (T1) are
wired into the L2 ``repair_engine`` so each repair iteration receives:

  (a) the hybrid epistemic diff + FULL stderr trace in the new RepairContext
      fields (``prior_iteration_diff`` / ``failure_trace``); and
  (b) a dynamically-lowered temperature when the SAME failure signature
      repeats across iterations.

Constraints pinned here:
  * RepairContext accepts + defaults the two new fields (OFF byte-identical).
  * ``_generate_repair_candidate`` threads ``temperature`` into the provider
    ``generate`` call (and fails soft when the provider rejects the kwarg).
  * The real ``temperature_for_attempt`` / ``build_failure_context`` primitives
    are exercised — repeated signature lowers temperature; enabled flag toggles
    the rich-context population.

Spec: docs/superpowers/specs/2026-06-22-epistemic-feedback-and-lane-escalation.md §1.3
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.op_context import RepairContext
from backend.core.ouroboros.governance.repair_engine import (
    CandidateGenerationResult,
    RepairBudget,
    RepairEngine,
    _epistemic_base_temp,
)
from backend.core.ouroboros.governance import epistemic_feedback as ef


# ===========================================================================
# 1. RepairContext new fields — accept + default
# ===========================================================================


class TestRepairContextNewFields:
    def test_new_fields_default_empty(self):
        """Constructing RepairContext WITHOUT the new fields leaves them empty
        (OFF byte-identical — legacy construction sites unchanged)."""
        rc = RepairContext(
            iteration=1,
            max_iterations=8,
            failure_class="test",
            failure_signature_hash="abc",
            failing_tests=("t1",),
            failure_summary="boom",
            current_candidate_content="def f(): pass",
            current_candidate_file_path="f.py",
        )
        assert rc.prior_iteration_diff == ""
        assert rc.failure_trace == ""

    def test_new_fields_accept_values(self):
        rc = RepairContext(
            iteration=2,
            max_iterations=8,
            failure_class="test",
            failure_signature_hash="abc",
            failing_tests=("t1",),
            failure_summary="boom",
            current_candidate_content="x",
            current_candidate_file_path="f.py",
            prior_iteration_diff="--- DIFF ---",
            failure_trace="Traceback ...",
        )
        assert rc.prior_iteration_diff == "--- DIFF ---"
        assert rc.failure_trace == "Traceback ..."


# ===========================================================================
# 2. Real temperature_for_attempt — parametric degeneration
# ===========================================================================


class TestTemperatureForAttempt:
    def test_zero_repeat_returns_base(self):
        assert ef.temperature_for_attempt(0.2, 0) == pytest.approx(0.2)

    def test_repeat_lowers_temperature(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_DECAY", raising=False)
        monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_FLOOR", raising=False)
        t0 = ef.temperature_for_attempt(0.2, 0)
        t1 = ef.temperature_for_attempt(0.2, 1)
        t2 = ef.temperature_for_attempt(0.2, 2)
        assert t1 < t0
        assert t2 < t1

    def test_base_temp_default_is_shared_codegen_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EPISTEMIC_BASE_TEMP", raising=False)
        assert _epistemic_base_temp() == pytest.approx(0.2)


# ===========================================================================
# 3. Real build_failure_context — rich context populated when enabled, empty when disabled
# ===========================================================================


class TestBuildFailureContextGating:
    def test_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", raising=False)
        assert ef.epistemic_feedback_enabled() is True

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "false")
        assert ef.epistemic_feedback_enabled() is False

    def test_context_contains_diff_and_trace(self):
        out = ef.build_failure_context(
            prior_src="def f():\n    return 1\n",
            failed_src="def f():\n    return 2\n",
            stderr="AssertionError: 1 != 2\nFULL_TRACE_MARKER",
            failing_tests=["tests/test_x.py::test_a"],
            sub_goal_label="op-123",
        )
        assert "op-123" in out
        assert "FULL_TRACE_MARKER" in out
        assert "tests/test_x.py::test_a" in out
        # unified-diff markers present (prior vs failing)
        assert "Previous Stable Sub-Goal" in out or "+    return 2" in out


# ===========================================================================
# 4. _generate_repair_candidate threads temperature into provider.generate
# ===========================================================================


class _StubGenResult:
    def __init__(self, *, candidates: List[Any]):
        self.candidates = candidates
        self.model_id = "m"
        self.provider_name = "p"


class _TempRecordingProvider:
    """Provider that records the temperature kwarg it received."""

    def __init__(self, *, accepts_temperature: bool = True):
        self.accepts_temperature = accepts_temperature
        self.received_temperatures: List[Optional[float]] = []

    async def generate(self, ctx, deadline, *, repair_context=None, temperature=None):
        if not self.accepts_temperature and temperature is not None:
            raise TypeError("generate() got an unexpected keyword argument 'temperature'")
        self.received_temperatures.append(temperature)
        return _StubGenResult(candidates=[{"file_path": "x", "full_content": "y"}])


class _LegacyProvider:
    """Provider whose generate does NOT accept temperature at all."""

    def __init__(self):
        self.call_count = 0

    async def generate(self, ctx, deadline, *, repair_context=None):
        self.call_count += 1
        return _StubGenResult(candidates=[{"file_path": "x", "full_content": "y"}])


def _make_engine(provider) -> RepairEngine:
    return RepairEngine(
        budget=RepairBudget(),
        prime_provider=provider,
        repo_root=Path("."),
    )


def _invoke(engine, *, temperature):
    return asyncio.run(engine._generate_repair_candidate(
        ctx=object(),
        pipeline_deadline=datetime.now(timezone.utc),
        repair_context="rc",
        temperature=temperature,
    ))


class TestTemperatureThreading:
    def test_temperature_passed_to_generate(self):
        provider = _TempRecordingProvider()
        out = _invoke(_make_engine(provider), temperature=0.05)
        assert isinstance(out, CandidateGenerationResult)
        assert out.candidate is not None
        assert provider.received_temperatures == [0.05]

    def test_none_temperature_preserves_legacy_shape(self):
        provider = _TempRecordingProvider()
        _invoke(_make_engine(provider), temperature=None)
        # None override → legacy call shape (no temperature kwarg → default None recorded)
        assert provider.received_temperatures == [None]

    def test_legacy_provider_failsoft_retry_without_temperature(self):
        """A provider whose generate rejects ``temperature`` must NOT crash the
        repair loop — the engine retries WITHOUT the kwarg."""
        provider = _LegacyProvider()
        out = _invoke(_make_engine(provider), temperature=0.05)
        assert out.candidate is not None
        assert provider.call_count == 1


# ===========================================================================
# 5. Repeated signature lowers the temperature handed to generate (integration of
#    the real primitive into the engine-facing temperature computation)
# ===========================================================================


class TestPromptRendersEpistemicFields:
    """The provider prompt-render site (_build_codegen_prompt REPAIR MODE block)
    must surface prior_iteration_diff + failure_trace with clear labels when
    present, and stay byte-identical when both are empty (OFF case)."""

    def _build(self, tmp_path, rc):
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        (tmp_path / "module.py").write_text("def f():\n    return 1\n")
        ctx = MagicMock()
        ctx.op_id = "op-xyz"
        ctx.description = "fix it"
        ctx.target_files = ["module.py"]
        ctx.human_instructions = ""
        ctx.strategic_memory_prompt = ""
        ctx.expanded_context_files = ()
        ctx.cross_repo = False
        ctx.repo_scope = set()
        ctx.telemetry = None
        ctx.is_read_only = False
        return _build_codegen_prompt(
            ctx=ctx, repo_root=tmp_path, repo_roots=None, repair_context=rc,
        )

    def test_fields_rendered_when_present(self, tmp_path):
        rc = RepairContext(
            iteration=2, max_iterations=8, failure_class="test",
            failure_signature_hash="abc", failing_tests=("t1",),
            failure_summary="boom", current_candidate_content="x",
            current_candidate_file_path="module.py",
            prior_iteration_diff="EPISTEMIC_DIFF_MARKER_42",
            failure_trace="FULL_TRACE_MARKER_42",
        )
        prompt = self._build(tmp_path, rc)
        assert "EPISTEMIC DIFF" in prompt
        assert "EPISTEMIC_DIFF_MARKER_42" in prompt
        assert "FULL FAILURE TRACE" in prompt
        assert "FULL_TRACE_MARKER_42" in prompt

    def test_off_case_no_epistemic_headers(self, tmp_path):
        rc = RepairContext(
            iteration=2, max_iterations=8, failure_class="test",
            failure_signature_hash="abc", failing_tests=("t1",),
            failure_summary="boom", current_candidate_content="x",
            current_candidate_file_path="module.py",
        )  # new fields default empty
        prompt = self._build(tmp_path, rc)
        assert "EPISTEMIC DIFF" not in prompt
        assert "FULL FAILURE TRACE" not in prompt
        # Legacy REPAIR block still present
        assert "REPAIR ITERATION" in prompt


class TestRepeatedSignatureLowersTemperature:
    def test_repeat_count_monotonic_decay(self, monkeypatch):
        """Simulate the _run_inner per-iteration computation: a recurring signature
        increments its seen-count, and temperature_for_attempt(base, repeated) decays."""
        monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_DECAY", raising=False)
        monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_FLOOR", raising=False)
        base = _epistemic_base_temp()
        seen: dict = {}
        sig = "sig-A"
        temps = []
        for _ in range(3):
            seen[sig] = seen.get(sig, 0) + 1
            repeated = seen[sig] - 1  # 0 on first sight
            temps.append(ef.temperature_for_attempt(base, repeated))
        assert temps[0] == pytest.approx(base)   # first sight: base unchanged
        assert temps[1] < temps[0]               # second sight: lowered
        assert temps[2] < temps[1]               # third sight: lower still
