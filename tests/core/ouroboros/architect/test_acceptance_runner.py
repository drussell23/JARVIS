"""
Tests for AcceptanceRunner
===========================

Covers subprocess execution, regex matching, timeout handling,
sandbox skipping, and the AcceptanceResult dataclass.
"""

from __future__ import annotations

import asyncio
import pytest

from backend.core.ouroboros.architect.acceptance_runner import AcceptanceResult, AcceptanceRunner
from backend.core.ouroboros.architect.plan import AcceptanceCheck, CheckKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_check(
    check_id: str,
    check_kind: CheckKind,
    command: str,
    expected: str = "",
    timeout_s: float = 10.0,
    sandbox_required: bool = False,
) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_id=check_id,
        check_kind=check_kind,
        command=command,
        expected=expected,
        cwd=".",
        timeout_s=timeout_s,
        sandbox_required=sandbox_required,
    )


RUNNER = AcceptanceRunner()
SAGA_ID = "test-saga-001"


# ---------------------------------------------------------------------------
# AcceptanceResult dataclass
# ---------------------------------------------------------------------------


def test_result_dataclass():
    result = AcceptanceResult(check_id="chk-001", passed=True, output="ok", error="")
    assert result.check_id == "chk-001"
    assert result.passed is True
    assert result.output == "ok"
    assert result.error == ""


def test_result_dataclass_defaults():
    result = AcceptanceResult(check_id="chk-002", passed=False)
    assert result.output == ""
    assert result.error == ""


# ---------------------------------------------------------------------------
# EXIT_CODE checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_code_success():
    check = _make_check("chk-exit-ok", CheckKind.EXIT_CODE, "echo ok")
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].error == ""


@pytest.mark.asyncio
async def test_exit_code_failure():
    check = _make_check("chk-exit-fail", CheckKind.EXIT_CODE, "false")
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert len(results) == 1
    assert results[0].passed is False


# ---------------------------------------------------------------------------
# REGEX_STDOUT checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regex_stdout_match():
    check = _make_check(
        "chk-regex-match",
        CheckKind.REGEX_STDOUT,
        "echo hello world",
        expected="hello",
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_regex_stdout_no_match():
    check = _make_check(
        "chk-regex-nomatch",
        CheckKind.REGEX_STDOUT,
        "echo goodbye",
        expected="hello",
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is False


# ---------------------------------------------------------------------------
# IMPORT_CHECK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_check_success():
    check = _make_check(
        "chk-import-ok",
        CheckKind.IMPORT_CHECK,
        "python3 -c 'import os'",
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is True


@pytest.mark.asyncio
async def test_import_check_failure():
    check = _make_check(
        "chk-import-fail",
        CheckKind.IMPORT_CHECK,
        "python3 -c 'import _nonexistent_module_xyz'",
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is False


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_failed():
    check = _make_check(
        "chk-timeout",
        CheckKind.EXIT_CODE,
        "sleep 10",
        timeout_s=0.5,
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is False
    assert "Timeout" in results[0].error or "timeout" in results[0].error.lower()


# ---------------------------------------------------------------------------
# Sandbox skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_required_skipped():
    check = _make_check(
        "chk-sandbox",
        CheckKind.EXIT_CODE,
        "false",  # would fail if run
        sandbox_required=True,
    )
    results = await RUNNER.run_checks((check,), SAGA_ID)
    assert results[0].passed is True
    assert "skipped" in results[0].output.lower()


# ---------------------------------------------------------------------------
# Multiple checks — all run independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_checks_independent():
    checks = (
        _make_check("chk-a", CheckKind.EXIT_CODE, "echo a"),
        _make_check("chk-b", CheckKind.EXIT_CODE, "false"),
        _make_check("chk-c", CheckKind.REGEX_STDOUT, "echo hello", expected="hello"),
    )
    results = await RUNNER.run_checks(checks, SAGA_ID)
    assert len(results) == 3
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[2].passed is True
