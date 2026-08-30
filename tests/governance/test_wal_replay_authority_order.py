"""Parked work must re-drain by what it IS, not by when it was parked.

`_replay_wal` iterated pending WAL rows in raw insertion order, so a row's
position was decided by WHEN the pool happened to be full when it arrived.
Under sustained backpressure the parked set is dominated by background sensor
churn (177 `pool_capacity_full` parks in soak bt-2026-08-30-085852), so an
operator's signed goal re-dispatches somewhere in the middle of a queue of
trivia on every drain.

Admission was fixed at intake step 4 -- the ledger went from
`idempotency_key: "backpressure"` to `"enqueued"`. This is the second half of
the same problem one layer down: winning the door does not help if the
re-drain deals you back into the pack.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as R


class _Entry:
    """Minimal WAL row: the replay path only reads `.envelope_dict`."""
    def __init__(self, tag, source="doc_staleness", goal_id=None):
        self.tag = tag
        self.envelope_dict = {"tag": tag, "source": source, "goal_id": goal_id}

    def __repr__(self):
        return "<Entry %s>" % self.tag


class _Env:
    def __init__(self, d):
        self.tag = d.get("tag")
        self.source = d.get("source")
        gid = d.get("goal_id")
        self.evidence = {"provenance": {"goal_id": gid}} if gid else {}


class _IE:
    @staticmethod
    def from_dict(d):
        if d.get("tag") == "BROKEN":
            raise ValueError("unrebuildable row")
        return _Env(d)


@pytest.fixture
def router():
    return R.UnifiedIntakeRouter.__new__(R.UnifiedIntakeRouter)


def _authority(monkeypatch, holder):
    """Only the envelope whose goal_id is in *holder* carries authority."""
    monkeypatch.setattr(
        R, "_carries_verified_operator_authority",
        lambda env: getattr(env, "evidence", {})
        .get("provenance", {})
        .get("goal_id") in holder,
    )


def test_signed_intent_moves_to_the_head(router, monkeypatch):
    """The whole point. A signed goal parked LAST must re-drain FIRST."""
    _authority(monkeypatch, {"g-signed"})
    monkeypatch.setattr(R, "_compute_priority", lambda e, *a, **k: (50, None))
    rows = [
        _Entry("noise-1"),
        _Entry("noise-2"),
        _Entry("signed", source="roadmap", goal_id="g-signed"),
    ]
    out = router._order_replay_by_authority(rows, _IE)
    assert out[0].tag == "signed"


def test_equal_priority_rows_keep_insertion_order(router, monkeypatch):
    """Stability is load-bearing: this ADDS a tier discipline over FIFO, it
    does not replace it with churn. A repeated drain must not reshuffle."""
    _authority(monkeypatch, set())
    monkeypatch.setattr(R, "_compute_priority", lambda e, *a, **k: (50, None))
    rows = [_Entry("a"), _Entry("b"), _Entry("c")]
    out = router._order_replay_by_authority(rows, _IE)
    assert [e.tag for e in out] == ["a", "b", "c"]


def test_priority_score_orders_ordinary_rows(router, monkeypatch):
    """Ordinary rows use the module's OWN _compute_priority, so replay order
    and live-queue order cannot drift into disagreement."""
    _authority(monkeypatch, set())
    scores = {"low": 90, "high": 2, "mid": 40}
    monkeypatch.setattr(
        R, "_compute_priority", lambda e, *a, **k: (scores[e.tag], None)
    )
    rows = [_Entry("low"), _Entry("high"), _Entry("mid")]
    out = router._order_replay_by_authority(rows, _IE)
    assert [e.tag for e in out] == ["high", "mid", "low"]


def test_an_unrebuildable_row_does_not_raise(router, monkeypatch):
    """Replay must survive a malformed row -- the alternative is losing
    durably parked work."""
    _authority(monkeypatch, set())
    monkeypatch.setattr(R, "_compute_priority", lambda e, *a, **k: (10, None))
    rows = [_Entry("ok-1"), _Entry("BROKEN"), _Entry("ok-2")]
    out = router._order_replay_by_authority(rows, _IE)
    assert len(out) == 3
    assert {e.tag for e in out} == {"ok-1", "BROKEN", "ok-2"}


def test_a_throwing_scorer_leaves_the_batch_intact(router, monkeypatch):
    """Ordering is an optimisation and must never be a risk."""
    _authority(monkeypatch, set())

    def _boom(*a, **k):
        raise RuntimeError("scorer down")

    monkeypatch.setattr(R, "_compute_priority", _boom)
    rows = [_Entry("a"), _Entry("b")]
    out = router._order_replay_by_authority(rows, _IE)
    assert len(out) == 2


def test_master_flag_off_preserves_insertion_order(router, monkeypatch):
    """Falsey master restores raw FIFO byte-for-byte."""
    _authority(monkeypatch, {"g-signed"})
    monkeypatch.setattr(R, "_compute_priority", lambda e, *a, **k: (50, None))
    monkeypatch.setenv("JARVIS_WAL_REPLAY_AUTHORITY_ORDER", "false")
    rows = [_Entry("noise"), _Entry("signed", "roadmap", "g-signed")]
    out = router._order_replay_by_authority(rows, _IE)
    assert [e.tag for e in out] == ["noise", "signed"]


def test_single_row_is_returned_untouched(router):
    """No sort, no rebuild, no scoring for a batch that cannot be reordered."""
    rows = [_Entry("solo")]
    assert router._order_replay_by_authority(rows, _IE) is rows


def test_sovereign_priority_outranks_every_mapped_source():
    """The authority key is not a hardcoded tier -- it is derived from the
    map's own floor, and must sit strictly below it."""
    assert R._sovereign_human_priority() < min(R._PRIORITY_MAP.values())


def test_replay_actually_calls_the_ordering():
    """WIRING PIN -- a correct sorter nothing invokes is invisible to every
    test above."""
    import ast
    import io as _io

    src = _io.open(R.__file__, encoding="utf-8").read()
    called = {
        n.func.attr
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_order_replay_by_authority" in called, "sorter is unwired"
