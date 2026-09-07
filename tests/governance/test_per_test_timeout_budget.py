"""A per-test cap must be able to run this repo's own suite.

2026-09-07: every VALIDATE of a candidate touching ``dw_capacity_probe.py``
failed with a bare ``[python:FAIL] 8, in select``. The runner passed
``--timeout=10`` (a flat constant unrelated to the run budget) while this
repo's own longest legitimate test —
``test_dream_engine.py::test_hanging_primary_is_bounded_by_wait_for`` —
deliberately holds for 10.01s to prove a ``wait_for`` bound. pytest-timeout
killed the process, no JSON report was written, the 109 tests that had passed
became unattributable, and the harness reported a TEST failure: it taught the
model that correct code was bad.

Two rules close it: the cap is a FRACTION of the run budget (so it cannot
drift below the suite it must run), and a plugin kill is INFRASTRUCTURE.
"""
from __future__ import annotations

import importlib

import pytest

from backend.core.ouroboros.governance import test_runner as tr


def _reload(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    return importlib.reload(tr)


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    importlib.reload(tr)


# --------------------------------------------------------------------------
# 1. the cap is derived from the run budget
# --------------------------------------------------------------------------

def test_per_test_cap_is_a_fraction_of_the_run_budget(monkeypatch):
    m = _reload(
        monkeypatch, JARVIS_TEST_TIMEOUT_S=200, JARVIS_TEST_PER_TEST_FRACTION=0.25,
        JARVIS_TEST_PER_TEST_TIMEOUT_S=None,
    )
    assert m._TEST_PER_TEST_TIMEOUT_S == 50


def test_a_bigger_run_budget_raises_the_cap_with_it(monkeypatch):
    small = _reload(monkeypatch, JARVIS_TEST_TIMEOUT_S=120,
                    JARVIS_TEST_PER_TEST_FRACTION=None, JARVIS_TEST_PER_TEST_TIMEOUT_S=None)._TEST_PER_TEST_TIMEOUT_S
    big = _reload(monkeypatch, JARVIS_TEST_TIMEOUT_S=600,
                  JARVIS_TEST_PER_TEST_FRACTION=None, JARVIS_TEST_PER_TEST_TIMEOUT_S=None)._TEST_PER_TEST_TIMEOUT_S
    assert big > small, "a cap unrelated to the budget is the defect this closes"


def test_default_cap_clears_this_repos_longest_legitimate_test(monkeypatch):
    """`test_hanging_primary_is_bounded_by_wait_for` holds for 10.01s BY DESIGN."""
    m = _reload(monkeypatch, JARVIS_TEST_TIMEOUT_S=None,
                JARVIS_TEST_PER_TEST_FRACTION=None, JARVIS_TEST_PER_TEST_TIMEOUT_S=None)
    assert m._TEST_PER_TEST_TIMEOUT_S > 11


def test_an_explicit_override_still_wins(monkeypatch):
    m = _reload(monkeypatch, JARVIS_TEST_TIMEOUT_S=600, JARVIS_TEST_PER_TEST_TIMEOUT_S=7)
    assert m._TEST_PER_TEST_TIMEOUT_S == 7


def test_the_cap_never_collapses_to_zero(monkeypatch):
    m = _reload(monkeypatch, JARVIS_TEST_TIMEOUT_S=1, JARVIS_TEST_PER_TEST_FRACTION=0.001,
                JARVIS_TEST_PER_TEST_TIMEOUT_S=None)
    assert m._TEST_PER_TEST_TIMEOUT_S >= 1, "--timeout=0 disables the guard entirely"


# --------------------------------------------------------------------------
# 2. a plugin kill is INFRASTRUCTURE, not a verdict on the code
# --------------------------------------------------------------------------

_BANNER = (
    "tests/x.py .\n"
    "+++++++++++++++++++++++++++++++++++ Timeout ++++++++++++++++++++++++++++++++++++\n"
    "  File \"/usr/lib/python3.11/selectors.py\", line 468, in select\n"
    "    fd_event_list = self._selector.poll(timeout, max_ev)\n"
)


def test_a_pytest_timeout_kill_is_marked_timed_out():
    res = tr.TestRunner._fallback_parse(1, 12.0, _BANNER)
    assert res.timed_out is True
    assert res.passed is False


def test_the_per_test_header_form_is_recognised_too():
    res = tr.TestRunner._fallback_parse(1, 12.0, "Timeout: 10.0s method: 'thread'\n")
    assert res.timed_out is True


def test_an_ordinary_failure_is_still_a_verdict_on_the_code():
    out = "tests/x.py F\n1 failed, 2 passed in 0.4s\nE  assert 1 == 2\n"
    res = tr.TestRunner._fallback_parse(1, 0.4, out)
    assert res.timed_out is False and res.passed is False
    assert res.total == 3 and res.failed == 1


def test_a_clean_run_without_a_report_is_still_a_pass():
    res = tr.TestRunner._fallback_parse(0, 0.4, "3 passed in 0.4s\n")
    assert res.passed is True and res.timed_out is False


def test_the_adapter_reports_a_killed_run_as_infra_not_test():
    """The mapping that decides what the model is TAUGHT."""
    killed = tr.TestRunner._fallback_parse(1, 12.0, _BANNER)
    ordinary = tr.TestRunner._fallback_parse(1, 0.4, "1 failed in 0.4s\nE assert\n")
    cls = lambda r: "none" if r.passed else ("infra" if r.timed_out else "test")  # noqa: E731
    assert cls(killed) == "infra"
    assert cls(ordinary) == "test"
