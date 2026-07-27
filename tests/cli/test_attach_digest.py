"""The attach summary answers questions instead of dumping fields.

From a real boot:

    ⎿ active ops: op-019fa4d2-246e-7759-86, op-019fa4d2-2468-794f-bc, ...
    ⎿ liquidity anthropic: 5,000,000 tokens
    ⚠ a provider runway is dry

Three problems, and they compound. The warning names nothing — while the
per-provider rows that answer "which one" are already in hand. Those rows were
sliced `[:3]` in DICT ORDER, so an exhausted provider sitting fourth was never
shown and the warning referred to something invisible. And "5,000,000 tokens"
beside "a runway is dry" reads as a contradiction until you know they describe
different providers.

The op ids were UUIDv7 — time-ordered, so same-millisecond ops share their
entire prefix. The identifying bytes are at the END, which is exactly where
the truncation cut.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any

import pytest


def _fns():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import (
            _active_ops_line, _liquidity_lines,
        )
    return _active_ops_line, _liquidity_lines


# --------------------------------------------------------------------------
# 1. the warning names the provider
# --------------------------------------------------------------------------

def test_the_dry_provider_is_named() -> None:
    """'a provider' is the one thing the operator cannot look up."""
    _ops, liq = _fns()
    lines = liq({"anthropic": {"tokens_remaining": 5_000_000},
                 "doubleword": {"tokens_remaining": 0}}, any_exhausted=True)
    warning = [l for l in lines if l.startswith("⚠")]
    assert warning and "doubleword" in warning[0]
    assert "a provider runway is dry" not in warning[0]


def test_an_exhausted_provider_can_never_be_truncated_away() -> None:
    """THE compounding bug: the rows were sliced in dict order, so the
    warning could reference a provider that was never displayed."""
    _ops, liq = _fns()
    providers = {f"p{i}": {"tokens_remaining": 1_000_000} for i in range(6)}
    providers["last-and-dry"] = {"tokens_remaining": 0}
    body = "\n".join(liq(providers, any_exhausted=True))
    assert "last-and-dry" in body, "the dry provider was cut from the list"


def test_dry_providers_sort_first() -> None:
    _ops, liq = _fns()
    lines = liq({"healthy": {"tokens_remaining": 9_000_000},
                 "empty": {"tokens_remaining": 0}}, any_exhausted=True)
    rows = [l for l in lines if l.startswith("⎿")]
    assert "empty" in rows[0]


def test_the_thinnest_runway_ranks_above_the_fattest() -> None:
    _ops, liq = _fns()
    rows = [l for l in liq({"fat": {"tokens_remaining": 9_000_000},
                            "thin": {"tokens_remaining": 12}})
            if l.startswith("⎿")]
    assert "thin" in rows[0]


def test_an_undeclared_runway_is_not_treated_as_a_problem() -> None:
    """Unknown is not evidence of exhaustion, and promoting it would push a
    real problem off the list."""
    _ops, liq = _fns()
    lines = liq({"unknown": {"tokens_remaining": None},
                 "empty": {"tokens_remaining": 0}}, any_exhausted=True)
    rows = [l for l in lines if l.startswith("⎿")]
    assert "empty" in rows[0]
    assert "undeclared" in "\n".join(rows)
    assert "unknown" not in [l for l in lines if l.startswith("⚠")][0]


def test_a_dry_row_is_marked_where_it_is_read() -> None:
    """The mark sits ON the row, so the number and its meaning are read
    together rather than two lines apart."""
    _ops, liq = _fns()
    rows = [l for l in liq({"empty": {"tokens_remaining": 0}}) if "empty" in l]
    assert "dry" in rows[0]


def test_a_flag_no_row_supports_is_reported_as_a_disagreement() -> None:
    """Repeating an unsupported claim is how a warning loses its meaning."""
    _ops, liq = _fns()
    warning = [l for l in liq({"anthropic": {"tokens_remaining": 9}},
                              any_exhausted=True) if l.startswith("⚠")]
    assert warning and "no provider row shows it" in warning[0]


def test_a_healthy_organism_gets_no_warning() -> None:
    _ops, liq = _fns()
    assert [l for l in liq({"a": {"tokens_remaining": 5}}) if l.startswith("⚠")] == []


@pytest.mark.parametrize("junk", [None, {}, [], "nonsense", {"x": None}])
def test_liquidity_never_raises(junk: Any) -> None:
    _ops, liq = _fns()
    assert isinstance(liq(junk), list)


# --------------------------------------------------------------------------
# 2. op ids a human can tell apart
# --------------------------------------------------------------------------

def test_the_distinguishing_END_of_a_uuidv7_is_kept() -> None:
    """UUIDv7 is time-ordered: same-millisecond ops share every leading byte,
    so a prefix distinguishes nothing."""
    ops, _liq = _fns()
    line = ops(["op-019fa4d2-246e-7759-86", "op-019fa4d2-2468-794f-bc"])
    assert "7759-86" in line and "794f-bc" in line
    assert "019fa4d2" not in line, "the shared prefix is still being printed"


def test_whole_segments_never_a_character_slice() -> None:
    """A mid-segment cut yields '-7759-86', whose leading dash reads as a
    typo."""
    ops, _liq = _fns()
    assert ": -" not in ops(["op-019fa4d2-246e-7759-86"])


def test_the_count_leads_because_that_is_the_glance_question() -> None:
    ops, _liq = _fns()
    assert ops(["a-b-c"]).startswith("⎿ 1 active op:")
    assert ops(["a-b-c", "d-e-f"]).startswith("⎿ 2 active ops:")


def test_hidden_ops_are_declared_not_silently_dropped() -> None:
    """A silent truncation reads as 'that is all of them'."""
    ops, _liq = _fns()
    line = ops([f"op-x-y-{i}" for i in range(9)])
    assert "9 active ops" in line and "+5 more" in line


def test_an_empty_roster_says_so() -> None:
    ops, _liq = _fns()
    assert "none" in ops([])


@pytest.mark.parametrize("junk", [None, [None], ["", "  "], [123], "str"])
def test_the_ops_line_never_raises(junk: Any) -> None:
    ops, _liq = _fns()
    assert isinstance(ops(junk), str)
