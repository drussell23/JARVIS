"""The micro-fix loop spends a bounded quota and stops making things worse.

``InteractiveRepairLoop.repair`` was already bounded -- ``for iteration in
range(_max_iterations())``, no unbounded loop anywhere -- so the ceiling
these tests pin is the one that was genuinely missing: the ladder calls
``repair()`` once per VALIDATE_RETRY iteration, each call restarts the
counter, and nothing spanned them. A file could receive a whole iteration
budget per rung.

Two other severances matter more than arithmetic:

* a fix that introduces a syntax error makes the loop the author of every
  later traceback, and the rest of the budget goes on chasing its own edit;
* identical content twice is not convergence, and the iteration ceiling only
  noticed after paying for every iteration.

Cycle detection is not reimplemented here -- ``ForwardProgressDetector``
already does consecutive-content-hash detection for the GENERATE loop and is
composed, keyed per ``(op, file)``.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.interactive_repair import (
    InteractiveRepairLoop,
)
from backend.core.ouroboros.governance.micro_fix_governor import (
    MicroFixQuotaGovernor,
    content_fingerprint,
    default_governor,
    parses_cleanly,
)

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Quota arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_admits_exactly_its_limit():
    gov = MicroFixQuotaGovernor(max_attempts=3)
    verdicts = [
        await gov.admit(op_id="op", file_path="a.py") for _ in range(5)
    ]
    assert [v.permitted for v in verdicts] == [True, True, True, False, False]
    assert verdicts[3].reason == "quota_exhausted"
    assert verdicts[4].reason == "severed:quota"


@pytest.mark.asyncio
async def test_quota_is_per_file_not_per_op():
    """Two files in one op must not spend each other's budget."""
    gov = MicroFixQuotaGovernor(max_attempts=1)
    assert (await gov.admit(op_id="op", file_path="a.py")).permitted
    assert (await gov.admit(op_id="op", file_path="b.py")).permitted
    assert not (await gov.admit(op_id="op", file_path="a.py")).permitted


@pytest.mark.asyncio
async def test_quota_is_per_op_not_global():
    gov = MicroFixQuotaGovernor(max_attempts=1)
    assert (await gov.admit(op_id="op1", file_path="a.py")).permitted
    assert (await gov.admit(op_id="op2", file_path="a.py")).permitted


@pytest.mark.asyncio
async def test_claim_is_taken_at_admission():
    """An attempt that crashes still consumed a turn; a quota that only
    counts clean exits is one a crash loop can evade."""
    gov = MicroFixQuotaGovernor(max_attempts=2)
    await gov.admit(op_id="op", file_path="a.py")
    assert (await gov.state(op_id="op", file_path="a.py")).attempts == 1


@pytest.mark.asyncio
async def test_concurrent_admission_cannot_double_spend():
    """VALIDATE validates candidates with asyncio.gather, so admission is a
    read-modify-write two coroutines can enter at once."""
    gov = MicroFixQuotaGovernor(max_attempts=3)
    results = await asyncio.gather(*[
        gov.admit(op_id="op", file_path="a.py") for _ in range(10)
    ])
    assert sum(1 for v in results if v.permitted) == 3


@pytest.mark.asyncio
async def test_release_forgets_the_entry():
    gov = MicroFixQuotaGovernor(max_attempts=1)
    await gov.admit(op_id="op", file_path="a.py")
    assert not (await gov.admit(op_id="op", file_path="a.py")).permitted
    await gov.release(op_id="op", file_path="a.py")
    assert (await gov.admit(op_id="op", file_path="a.py")).permitted


def test_default_governor_is_stable():
    assert default_governor() is default_governor()


# ---------------------------------------------------------------------------
# Regression: the failure that justifies the mechanism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaking_a_parsing_file_severs():
    gov = MicroFixQuotaGovernor()
    verdict = await gov.observe_regression(
        op_id="op", file_path="a.py",
        before="x = 1\n", after="def f(:\n",
    )
    assert not verdict.permitted
    assert verdict.reason == "severed:regression"


@pytest.mark.asyncio
async def test_repairing_an_already_broken_file_is_not_a_regression():
    """A file that was already unparseable is exactly what micro-fix is for."""
    gov = MicroFixQuotaGovernor()
    verdict = await gov.observe_regression(
        op_id="op", file_path="a.py",
        before="def f(:\n", after="def f():\n    pass\n",
    )
    assert verdict.permitted


@pytest.mark.asyncio
async def test_non_python_files_are_not_judged_by_python_grammar():
    gov = MicroFixQuotaGovernor()
    verdict = await gov.observe_regression(
        op_id="op", file_path="fixture.json",
        before='{"a": 1}', after='{"a": 2}',
    )
    assert verdict.permitted
    assert parses_cleanly('{"a": 1}', "fixture.json") is None


def test_parses_cleanly_tri_state():
    assert parses_cleanly("x = 1\n", "a.py") is True
    assert parses_cleanly("def f(:\n", "a.py") is False
    assert parses_cleanly("anything", "a.txt") is None


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_content_twice_severs():
    gov = MicroFixQuotaGovernor()
    first = await gov.observe(op_id="op", file_path="a.py", content="x = 1\n")
    assert first.permitted
    second = await gov.observe(op_id="op", file_path="a.py", content="x = 1\n")
    assert not second.permitted
    assert second.reason == "severed:no_progress"


@pytest.mark.asyncio
async def test_changing_content_keeps_going():
    gov = MicroFixQuotaGovernor()
    assert (await gov.observe(op_id="op", file_path="a.py", content="x = 1\n")).permitted
    assert (await gov.observe(op_id="op", file_path="a.py", content="x = 2\n")).permitted


def test_empty_content_has_no_fingerprint():
    """Absence of content is not evidence of repetition."""
    assert content_fingerprint("") == ""
    assert content_fingerprint("x") != ""


# ---------------------------------------------------------------------------
# Wired into the real loop
# ---------------------------------------------------------------------------


class _SyntaxBreakingProvider:
    """Emits a fix that turns a parsing file into a syntax error."""

    def __init__(self):
        self.calls = 0

    async def plan(self, prompt, deadline):
        self.calls += 1
        return json.dumps({
            "start_line": 1, "end_line": 1,
            "replacement": "def broken(:", "reasoning": "probe",
        })


def _failing_body() -> str:
    return "def test_x():\n    assert 1 == 2\n"


@pytest.mark.asyncio
async def test_loop_reverts_a_fix_that_breaks_the_file(tmp_path):
    """The loop must hand the candidate back no worse than it arrived."""
    (tmp_path / "tests").mkdir()
    shutil.copy(REPO / "pytest.ini", tmp_path / "pytest.ini")
    rel = "tests/test_case.py"
    body = _failing_body()

    provider = _SyntaxBreakingProvider()
    loop = InteractiveRepairLoop(
        provider=provider, project_root=tmp_path, isolated=True,
        governor=MicroFixQuotaGovernor(max_attempts=5),
    )
    result = await loop.repair(
        file_path=rel, file_content=body,
        test_argv=[sys.executable, "-m", "pytest", "-x", "-q", "--color=no", rel],
        op_id="op-regress",
    )

    assert result.fixed is False
    assert result.repaired_content == body, (
        "the loop kept a fix that made the file unparseable"
    )
    assert provider.calls == 1, (
        "the loop kept going after breaking the file — it is now repairing "
        "its own damage"
    )


@pytest.mark.asyncio
async def test_quota_spans_repair_invocations(tmp_path):
    """The gap the per-invocation ceiling could not close: the ladder calls
    repair() once per VALIDATE_RETRY iteration."""
    (tmp_path / "tests").mkdir()
    shutil.copy(REPO / "pytest.ini", tmp_path / "pytest.ini")
    rel = "tests/test_case.py"
    gov = MicroFixQuotaGovernor(max_attempts=2)

    async def _one():
        loop = InteractiveRepairLoop(
            provider=_SyntaxBreakingProvider(), project_root=tmp_path,
            isolated=True, governor=gov,
        )
        return await loop.repair(
            file_path=rel, file_content=_failing_body(),
            test_argv=[sys.executable, "-m", "pytest", "-x", "-q",
                       "--color=no", rel],
            op_id="op-span",
        )

    await _one()
    await _one()
    third = await _one()

    assert "MicroFixExhaustionFault" in third.final_output
    assert third.fixed is False
    assert third.repaired_content == _failing_body(), (
        "a refused invocation must hand back the text it was given"
    )
