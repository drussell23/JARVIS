"""L2 repair must tell the model what failed.

bt-2026-09-21-235603: seven ops ended ``l2_stopped`` with
``class_retries_exhausted:test`` after 40 repair iterations, none converged, and
18 of them flagged ``oscillation_detected`` -- the model re-emitting the same
file. Reproduced through the real ``RepairSandbox`` + ``FailureClassifier``,
the model was being told nothing:

* ``failure_summary = (stdout + stderr)[:300]`` -- pytest's session header and
  a red progress bar; the assertion starts past character 300;
* ``failure_trace = stderr`` -- empty: pytest reports failures on stdout;
* ``failing_test_ids = ()`` -- ``pytest.ini`` forces ``--color=yes`` and every
  ``FAILED`` line began with an escape, so the anchored pattern never matched.
  Every iteration therefore carried the SAME id-less signature, which the
  engine read as "the same failure again" and answered by lowering the
  temperature toward its floor -- steering a model that had no information
  into re-emitting what it had already written.

``REAL_STDOUT`` below is that sandbox's actual output for two failing tests,
captured verbatim; it is the fixture because a hand-written one would not have
contained the escapes that caused all three faults.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import epistemic_feedback as ef
from backend.core.ouroboros.governance.failure_classifier import FailureClassifier
from backend.core.ouroboros.governance.pytest_traceback import (
    failure_evidence,
    report_sections,
)
from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine
from backend.core.ouroboros.governance.repair_sandbox import SandboxValidationResult

REAL_STDOUT = (
    '\x1b[1m============================= test session starts ==============================\x1b[0m\n'
    'collected 2 items\n\n'
    'tests/test_l2_repro_tmp.py \x1b[31mF\x1b[0m\x1b[31mF\x1b[0m\x1b[31m                                            [100%]\x1b[0m\n\n'
    '=================================== FAILURES ===================================\n'
    '\x1b[31m\x1b[1m______________________________ test_wrong_premise ______________________________\x1b[0m\n'
    '\x1b[1m\x1b[31mtests/test_l2_repro_tmp.py\x1b[0m:4: in test_wrong_premise\n'
    '    \x1b[0m\x1b[94massert\x1b[39;49;00m strip_ansi(\x1b[33m"\x1b[39;49;00m\x1b[33mabc\x1b[39;49;00m\x1b[33m"\x1b[39;49;00m) == \x1b[33m"\x1b[39;49;00m\x1b[33mabd\x1b[39;49;00m\x1b[33m"\x1b[39;49;00m\x1b[90m\x1b[39;49;00m\n'
    "\x1b[1m\x1b[31mE   AssertionError: assert 'abc' == 'abd'\x1b[0m\n"
    '\x1b[1m\x1b[31mE     \x1b[0m\n'
    '\x1b[1m\x1b[31mE     \x1b[0m\x1b[91m- abd\x1b[39;49;00m\x1b[90m\x1b[39;49;00m\x1b[0m\n'
    '\x1b[1m\x1b[31mE     \x1b[92m+ abc\x1b[39;49;00m\x1b[90m\x1b[39;49;00m\x1b[0m\n'
    '\x1b[31m\x1b[1m_________________________________ test_second __________________________________\x1b[0m\n'
    '\x1b[1m\x1b[31mtests/test_l2_repro_tmp.py\x1b[0m:8: in test_second\n'
    '    \x1b[0m\x1b[94massert\x1b[39;49;00m value[\x1b[33m"\x1b[39;49;00m\x1b[33ma\x1b[39;49;00m\x1b[33m"\x1b[39;49;00m] \x1b[95mis\x1b[39;49;00m \x1b[94mTrue\x1b[39;49;00m\x1b[90m\x1b[39;49;00m\n'
    '\x1b[1m\x1b[31mE   assert 1 is True\x1b[0m\n'
    '\x1b[36m\x1b[1m=========================== short test summary info ============================\x1b[0m\n'
    "\x1b[31mFAILED\x1b[0m tests/test_l2_repro_tmp.py::\x1b[1mtest_wrong_premise\x1b[0m - AssertionError: assert 'abc' == 'abd'\n"
    '\x1b[31mFAILED\x1b[0m tests/test_l2_repro_tmp.py::\x1b[1mtest_second\x1b[0m - assert 1 is True\n'
    '\x1b[31m============================== \x1b[31m\x1b[1m2 failed\x1b[0m\x1b[31m in 0.15s\x1b[0m\x1b[31m ===============================\x1b[0m\n'
)
IDS = (
    "tests/test_l2_repro_tmp.py::test_wrong_premise",
    "tests/test_l2_repro_tmp.py::test_second",
)


def _svr(stdout: str, stderr: str = "") -> SandboxValidationResult:
    return SandboxValidationResult(
        passed=False, stdout=stdout, stderr=stderr, returncode=1, duration_s=0.1,
    )


# ---------------------------------------------------------------------------
# The classifier sees through the colour
# ---------------------------------------------------------------------------

def test_failing_ids_are_read_from_coloured_output():
    got = FailureClassifier().classify(_svr(REAL_STDOUT))
    assert got.failing_test_ids == IDS


def test_different_failures_get_different_signatures():
    """The id-less signature was a constant: every iteration 'repeated'."""
    one = FailureClassifier().classify(_svr(REAL_STDOUT))
    other = FailureClassifier().classify(_svr(REAL_STDOUT.replace("test_second", "test_third")))
    assert one.failure_signature_hash != other.failure_signature_hash


def test_a_setup_error_is_a_failing_id_too():
    out = "\x1b[31mERROR\x1b[0m tests/test_x.py::test_a - fixture 'db' not found\n"
    assert FailureClassifier().classify(_svr(out)).failing_test_ids == ("tests/test_x.py::test_a",)


# ---------------------------------------------------------------------------
# The evidence is what failed, not the header
# ---------------------------------------------------------------------------

def test_sections_are_named_by_pytests_own_rules():
    sections = report_sections(REAL_STDOUT)
    assert {"failures", "short test summary info"} <= set(sections)
    assert "\x1b" not in "".join(sections.values())


def test_evidence_carries_every_assertion_and_no_noise():
    ev = failure_evidence(REAL_STDOUT, "")
    assert "assert 'abc' == 'abd'" in ev.summary and "assert 1 is True" in ev.summary
    assert "E   AssertionError: assert 'abc' == 'abd'" in ev.trace
    assert "E   assert 1 is True" in ev.trace
    for text in (ev.summary, ev.trace):
        assert "\x1b" not in text
        assert "test session starts" not in text


def test_stderr_is_kept_alongside_the_report():
    ev = failure_evidence(REAL_STDOUT, "DeprecationWarning: loud\n")
    assert "DeprecationWarning: loud" in ev.trace
    assert "assert 1 is True" in ev.trace


@pytest.mark.parametrize("stdout,stderr,needle", [
    ("", "timeout", "timeout"),                                   # sandbox timeout
    ("", "sandbox not initialised", "sandbox not initialised"),   # infra
    ("ImportError while loading conftest '/x/conftest.py'.\n"
     "E   ModuleNotFoundError: No module named 'fastapi'\n", "", "fastapi"),
])
def test_a_run_without_a_failures_section_is_its_own_evidence(stdout, stderr, needle):
    ev = failure_evidence(stdout, stderr)
    assert needle in ev.trace
    assert ev.summary, "a run that said something must not summarise to nothing"


def test_evidence_is_bounded_by_the_one_trace_budget(monkeypatch):
    monkeypatch.setenv("JARVIS_EPISTEMIC_TRACE_MAX_CHARS", "120")
    ev = failure_evidence(REAL_STDOUT * 20, "")
    assert len(ev.trace) < 120 + 64, "the elision marker is the only overhead"
    assert "elided" in ev.trace


def test_empty_trace_adds_no_labelled_block():
    out = ef.build_failure_context(
        prior_src="a\n", failed_src="b\n", stderr="", failing_tests=["t"],
    )
    assert "FAILING TEST STDERR" not in out


# ---------------------------------------------------------------------------
# End to end: what the repair model is actually handed
# ---------------------------------------------------------------------------

class _RecordingProvider:
    def __init__(self) -> None:
        self.contexts: List[Any] = []

    async def generate(self, ctx, deadline, *, repair_context=None,
                       hypothesis_seed=None, temperature=None):
        self.contexts.append(repair_context)
        n = len(self.contexts)

        class _R:
            # A different candidate each call, so nothing here oscillates by
            # construction: the loop's view of the failure is what is under test.
            candidates = [{"file_path": "tests/test_l2_repro_tmp.py",
                           "full_content": f"def test_a():\n    assert {n}\n"}]
            model_id = "stub"
            provider_name = "stub"
        return _R()


class _PytestSandbox:
    """Answers every run with the real coloured pytest transcript."""

    def __init__(self, repo_root, test_timeout_s):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def sandbox_root(self):
        return Path("/nonexistent-l2-evidence-root")

    async def apply_full_content(self, content, file_path):
        return None

    async def apply_patch(self, unified_diff, file_path):
        return None

    async def run_tests(self, test_targets, timeout_s):
        return _svr(REAL_STDOUT)


class _Ctx:
    op_id = "op-l2-evidence"

    class generation:  # noqa: N801 — attribute shape the engine reads
        candidates = [{"file_path": "tests/test_l2_repro_tmp.py",
                       "full_content": "def test_a():\n    assert 0\n"}]


def _run_loop(monkeypatch, **env):
    for var in ("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", "JARVIS_L2_MULTIFILE_ENABLED",
                "JARVIS_EPISTEMIC_TRACE_MAX_CHARS"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    provider = _RecordingProvider()
    engine = RepairEngine(
        budget=RepairBudget.from_env(), prime_provider=provider,
        repo_root=Path("."), sandbox_factory=_PytestSandbox,
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=300)
    result = asyncio.run(engine._run_inner(_Ctx(), object(), deadline))
    return provider, result


def test_every_repair_iteration_is_told_what_failed(monkeypatch):
    provider, _ = _run_loop(monkeypatch)
    assert provider.contexts, "the loop never asked for a repair"
    for rc in provider.contexts:
        assert rc.failing_tests == IDS
        assert "assert 1 is True" in rc.failure_summary
        assert "E   AssertionError: assert 'abc' == 'abd'" in rc.failure_trace
        assert "\x1b" not in rc.failure_summary + rc.failure_trace


def test_the_error_reaches_the_model_with_epistemic_feedback_off(monkeypatch):
    provider, _ = _run_loop(monkeypatch, JARVIS_EPISTEMIC_FEEDBACK_ENABLED="false")
    assert provider.contexts
    assert all("assert 1 is True" in rc.failure_trace for rc in provider.contexts)


def test_the_rendered_prompt_carries_the_assertion(monkeypatch, tmp_path):
    """Through the real prompt builder, not just the dataclass."""
    from unittest.mock import MagicMock

    from backend.core.ouroboros.governance.providers import _build_codegen_prompt

    provider, _ = _run_loop(monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_l2_repro_tmp.py").write_text("def test_a():\n    assert 0\n")
    ctx = MagicMock()
    ctx.op_id, ctx.description = "op-l2-evidence", "write tests"
    ctx.target_files = ["tests/test_l2_repro_tmp.py"]
    ctx.human_instructions = ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo, ctx.repo_scope, ctx.telemetry, ctx.is_read_only = False, set(), None, False
    prompt = _build_codegen_prompt(
        ctx=ctx, repo_root=tmp_path, repo_roots=None,
        repair_context=provider.contexts[0],
    )
    assert "FULL FAILURE TRACE" in prompt
    # Both summary lines, whole: the renderer no longer re-cuts at 300.
    assert "test_wrong_premise - AssertionError: assert 'abc' == 'abd'" in prompt
    assert "test_second - assert 1 is True" in prompt
