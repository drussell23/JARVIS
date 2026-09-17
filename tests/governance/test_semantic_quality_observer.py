"""Candidate quality is measurable at GENERATE, whatever VALIDATE later does.

Three soaks produced zero landings and taught us nothing about the model,
because every op was shed for reasons unrelated to the candidate. A candidate
is a text; the questions worth asking about it are answerable the moment it
arrives.

Proven against the three real commits before the observer ever ran in a soak:

    7e7fe18c3c  pure churn              null_churn=6/6   ratio=1.00  eff=0.00
    34b5bd8b33  mangle + redundant kwarg null_churn=11/13 ratio=0.85  eff=0.31
    7f8c686ce0  genuine + collateral     null_churn=14/16 ratio=0.88  eff=0.50
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import semantic_quality_observer as SQ


class _Ctx:
    op_id = "op-test"
    telemetry = None
    generate_retries_remaining = 2


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SEMANTIC_QUALITY_PATH", str(tmp_path / "q.jsonl"))
    SQ.reset_for_tests()
    yield
    SQ.reset_for_tests()


_BASE = '''import logging


def f(payload):
    try:
        return json.dumps(payload, separators=(",", ":"))
    except Exception:
        logging.exception("boom")
'''


def _obs(after, **kw):
    return SQ.observe_candidate(
        ctx=_Ctx(), target_file="m.py", original=_BASE, candidate_content=after,
        schema_requested=kw.pop("req", "diff"),
        schema_returned=kw.pop("ret", "diff"), **kw,
    )


# --------------------------------------------------------------------------
# The tic detector — by subtraction, never by rule
# --------------------------------------------------------------------------

def test_a_purely_cosmetic_change_is_all_null_churn():
    after = _BASE.replace('separators=(",", ":")', "separators=(',', ':')")
    o = _obs(after)
    assert o.changed_lines > 0
    assert o.null_churn_ratio == 1.0
    assert o.delta_efficiency == 0.0


def test_a_redundant_kwarg_is_caught_without_being_named():
    """THE point of the design: no rule lists exc_info, and none has to."""
    after = _BASE.replace('logging.exception("boom")',
                          'logging.exception("boom", exc_info=True)')
    o = _obs(after)
    assert o.null_churn_ratio == 1.0, "a known tic survived as 'meaning'"


def test_a_tic_nobody_has_met_yet_lands_in_the_same_number():
    """A kwarg restating a different stdlib default — never discussed, never
    enumerated, same subtraction."""
    base = "import json\n\n\ndef g(x):\n    return json.dumps(x)\n"
    after = base.replace("json.dumps(x)", "json.dumps(x, skipkeys=False)")
    o = SQ.observe_candidate(
        ctx=_Ctx(), target_file="g.py", original=base, candidate_content=after,
        schema_requested="diff", schema_returned="diff",
    )
    assert o.null_churn_ratio == 1.0


def test_real_work_is_not_null_churn():
    after = _BASE.replace('        logging.exception("boom")',
                          '        logging.exception("boom")\n        raise')
    o = _obs(after)
    assert o.null_churn_ratio < 1.0
    assert o.ast_delta_nodes > 0


def test_no_tic_patterns_are_hardcoded():
    import inspect

    body = inspect.getsource(SQ._null_churn_lines).split('"""')[-1]
    for suspect in ("exc_info", "separators", "quote", "docstring"):
        assert suspect not in body, f"a named rule crept in: {suspect}"


# --------------------------------------------------------------------------
# Schema adherence
# --------------------------------------------------------------------------

def test_adherence_is_recorded_both_ways():
    assert _obs(_BASE + "\n", req="diff", ret="diff").adhered is True
    assert _obs(_BASE + "\n", req="diff", ret="full_content").adhered is False


def test_a_malformed_diff_is_counted():
    o = SQ.observe_candidate(
        ctx=_Ctx(), target_file="m.py", original=_BASE, candidate_content=None,
        schema_requested="diff", schema_returned="diff", malformed_diff=True,
    )
    assert o.malformed_diff is True
    assert "malformed diffs  : 1/1" in SQ.render_report()


# --------------------------------------------------------------------------
# It reports distributions, never grades
# --------------------------------------------------------------------------

def test_the_report_states_no_verdict():
    _obs(_BASE.replace('separators=(",", ":")', "separators=(',', ':')"))
    report = SQ.render_report()
    for verdict in ("PASS", "FAIL", "good", "bad", "poor"):
        assert verdict not in report


def test_an_empty_run_says_so():
    assert "no candidates observed" in SQ.render_report()


# --------------------------------------------------------------------------
# It never perturbs generation
# --------------------------------------------------------------------------

def test_unparsable_input_does_not_raise():
    o = SQ.observe_candidate(
        ctx=_Ctx(), target_file="m.py", original="def (:\n",
        candidate_content="def (:\n x", schema_requested="diff",
        schema_returned="diff",
    )
    assert o is not None


def test_a_new_file_has_no_delta_metrics():
    o = SQ.observe_candidate(
        ctx=_Ctx(), target_file="new.py", original=None,
        candidate_content="def x():\n    pass\n",
        schema_requested="full_content", schema_returned="full_content",
    )
    assert o.changed_lines == 0 and o.delta_efficiency == 0.0


def test_disabled_observer_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_SEMANTIC_QUALITY_OBSERVER_ENABLED", "false")
    assert _obs(_BASE) is None


# --------------------------------------------------------------------------
# Phase 3 — the cascade ceiling is derived from the FSM's own budget
# --------------------------------------------------------------------------

def test_the_ceiling_is_the_ops_own_retry_budget():
    SQ.note_malformed("op-a")
    assert SQ.diff_cascade_exhausted("op-a", retries_remaining=3) is False
    SQ.note_malformed("op-a")
    SQ.note_malformed("op-a")
    assert SQ.diff_cascade_exhausted("op-a", retries_remaining=3) is True


def test_a_generous_budget_gets_more_attempts():
    for _ in range(3):
        SQ.note_malformed("op-b")
    assert SQ.diff_cascade_exhausted("op-b", retries_remaining=9) is False


def test_an_op_with_no_malformed_diffs_never_cascades():
    assert SQ.diff_cascade_exhausted("op-fresh", retries_remaining=0) is False


def test_ops_are_counted_independently():
    """Multi-agent: one op's cascade must not shed another's."""
    SQ.note_malformed("op-x")
    SQ.note_malformed("op-x")
    assert SQ.diff_cascade_exhausted("op-x", retries_remaining=2) is True
    assert SQ.diff_cascade_exhausted("op-y", retries_remaining=2) is False


def test_no_second_retry_constant_was_introduced():
    import inspect

    body = inspect.getsource(SQ.diff_cascade_exhausted).split('"""')[-1]
    assert "retries_remaining" in body
    for suspect in ("== 3", "> 3", "MAX_RETRIES", "= 5"):
        assert suspect not in body
