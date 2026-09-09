"""Work that has landed is not work.

Session ``bt-2026-09-08-225144`` produced this system's first autonomous
landing — and then re-dispatched the same goal 29 times in 8 seconds. The
ledger's own rows tell it exactly::

    dispatched  op-01a0834a
    satisfied   sha=bb575e9b28  op-01a0834a   <- the landing
    terminal    op-01a0834a
    dispatched  op-01a0834d                   <- re-dispatched anyway
    terminal    op-01a0834d
    dispatched  op-01a08352                   <- and again
    terminal    op-01a08352

The record was written, correct, and read by nobody before choosing the next
piece of work.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance import goal_reconciliation_ledger as GRL
from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

LANDED = "ov-auto-uncovered-module-apply-emergency-cpu-fix"


class _Rec:
    def __init__(self, event, goal_id, sha=""):
        self.event, self.goal_id, self.commit_sha = event, goal_id, sha


class _Work:
    def __init__(self, goal_id, target):
        self._g, self.target_file, self.weight = goal_id, target, 1.0
        self.kind = "uncovered_module"

    @property
    def goal_id(self):
        return self._g


# --------------------------------------------------------------------------
# The ledger query
# --------------------------------------------------------------------------

def test_a_satisfied_goal_is_reported_settled():
    recs = [_Rec("dispatched", "g"), _Rec("satisfied", "g", "abc123")]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset({"g"})


def test_later_dispatch_and_terminal_do_not_unsettle_it():
    """Exactly the live sequence: satisfaction, then more dispatches."""
    recs = [
        _Rec("dispatched", "g"), _Rec("satisfied", "g", "abc123"),
        _Rec("terminal", "g"), _Rec("dispatched", "g"), _Rec("terminal", "g"),
    ]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset({"g"})


def test_reactivated_retires_the_satisfaction():
    """The binding's commit vanished (reset / amend / branch rewind), so the
    work no longer exists and the goal must become selectable again."""
    recs = [_Rec("satisfied", "g", "abc"), _Rec("reactivated", "g", "abc")]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset()


def test_a_re_satisfaction_after_reactivation_settles_again():
    recs = [
        _Rec("satisfied", "g", "abc"), _Rec("reactivated", "g", "abc"),
        _Rec("satisfied", "g", "def"),
    ]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset({"g"})


def test_an_unknown_goal_is_not_settled():
    recs = [_Rec("satisfied", "other", "abc")]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset()


def test_only_requested_ids_are_returned():
    recs = [_Rec("satisfied", "a"), _Rec("satisfied", "b")]
    assert GRL.satisfied_goal_ids(["a"], records=recs) == frozenset({"a"})


def test_this_is_NOT_the_promotion_question():
    """`reconcile_goal` asks whether the commit is reachable from
    ``landing_ref``. Autonomous work lands on an ``ouroboros/auto/<session>``
    branch by design, so that answer is ACTIVE until an operator merges —
    correct for promotion, and fatal if used for scheduling."""
    src = inspect.getsource(GRL.satisfied_goal_ids)
    assert "landing_ref" in src, "the distinction is not documented at the seam"
    assert "is_reachable" not in src, "the scheduling query depends on git"


@pytest.mark.parametrize("hostile", [None, [], [object()], 42, "x"])
def test_the_query_never_raises(hostile):
    assert isinstance(GRL.satisfied_goal_ids(hostile), frozenset)


def test_a_bad_row_does_not_poison_the_verdict():
    class _Bad:
        @property
        def goal_id(self):
            raise RuntimeError("boom")

    recs = [_Bad(), _Rec("satisfied", "g")]
    assert GRL.satisfied_goal_ids(["g"], records=recs) == frozenset({"g"})


# --------------------------------------------------------------------------
# Discovery consults it
# --------------------------------------------------------------------------

class _Oracle:
    def __init__(self, settled):
        self._s = frozenset(settled)

    def satisfied_goal_ids(self, ids):
        return frozenset(i for i in ids if i in self._s)


def test_discovery_drops_a_settled_candidate(tmp_path):
    pool = [_Work(LANDED, "tests/test_a.py"), _Work("ov-open", "tests/test_b.py")]
    got = asyncio.run(
        GD._settled_goal_ids(pool, repo_root=tmp_path, settled=_Oracle([LANDED]))
    )
    assert got == frozenset({LANDED})


def test_the_ranking_loop_skips_settled_work():
    src = inspect.getsource(GD.discover)
    assert "settled_ids" in src
    assert "item.goal_id in settled_ids" in src


def test_the_skip_happens_before_the_cooldown_check():
    """Success CLEARS the cooldown, so after a landing the cooldown is not a
    brake. Settlement has to be the one that stops re-selection."""
    src = inspect.getsource(GD.discover)
    assert src.index("settled_ids") < src.index("is_cooling")


def test_discovery_is_injectable_and_defaults_to_the_real_ledger():
    sig = inspect.signature(GD.discover)
    assert "settled" in sig.parameters
    assert sig.parameters["settled"].default is None


def test_the_ledger_read_is_off_the_event_loop():
    """Discovery's contract is that a pass completes at the speed of the cheap
    tier; a synchronous file read on the loop would break it."""
    src = inspect.getsource(GD._settled_goal_ids)
    assert "asyncio.to_thread" in src


@pytest.mark.parametrize("hostile", [None, [], [object()], "x"])
def test_the_discovery_filter_never_raises(hostile, tmp_path):
    got = asyncio.run(GD._settled_goal_ids(hostile, repo_root=tmp_path))
    assert isinstance(got, frozenset)


def test_a_hostile_oracle_degrades_to_no_suppression(tmp_path):
    class _Boom:
        def satisfied_goal_ids(self, ids):
            raise RuntimeError("boom")

    got = asyncio.run(
        GD._settled_goal_ids([_Work("g", "t")], repo_root=tmp_path, settled=_Boom())
    )
    assert got == frozenset(), "a broken oracle must not suppress real work"
