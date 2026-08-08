"""The session ceiling is derived from receipts, not declared as a literal.

`"2.50"` appeared twice in thin_client.py — once for the detached cold boot,
once for the launchd agent — with no record of what it was based on. It was not
even a bad guess: 772 recorded sessions put the 95th percentile at $0.77. That
is the problem. A number that happens to be right, asserted with no basis, is
indistinguishable from one that happens to be wrong, and nothing announces the
day it drifts.

The statistical care here is not decoration. A cap censors the record of its
own effect: sessions it stops are written down at almost exactly the cap, so
estimating a new ceiling from that tail extrapolates from numbers the old
ceiling chose.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.battle_test import session_economics as se  # noqa: E402


@pytest.fixture()
def history(tmp_path, monkeypatch):
    """A synthetic session history, addressed the way the real one is."""
    def _write(costs):
        for i, c in enumerate(costs):
            d = tmp_path / f"bt-2026-08-{i // 24 + 1:02d}-{i % 24:02d}0000"
            d.mkdir(parents=True, exist_ok=True)
            if c is not None:
                (d / "summary.json").write_text(json.dumps({"cost_total": c}))
        monkeypatch.setenv("JARVIS_SESSIONS_DIR", str(tmp_path))
        for var in ("JARVIS_COCKPIT_COST_CAP", "JARVIS_AEGIS_SESSION_CAP_USD"):
            monkeypatch.delenv(var, raising=False)
    return _write


# ---------------------------------------------------------------------------
# reading the receipts
# ---------------------------------------------------------------------------

def test_free_sessions_are_excluded(history) -> None:
    """Two thirds of the real history spent nothing — they booted, found no
    work, and exited. Counting them drags the quantile toward a ceiling that
    stops the first session that tries to do anything."""
    history([0.0, 0.0, 0.0, 1.0, 2.0])
    assert sorted(se.observed_session_costs()) == [1.0, 2.0]


def test_an_unreadable_summary_is_skipped_not_counted_as_free(history) -> None:
    history([1.0, 2.0])
    bad = se.sessions_dir() / "bt-2026-09-01-000000"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "summary.json").write_text("{ truncated")
    assert sorted(se.observed_session_costs()) == [1.0, 2.0]


def test_a_missing_history_is_not_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_SESSIONS_DIR", str(tmp_path / "nope"))
    assert se.observed_session_costs() == []


def test_booleans_are_not_costs(history) -> None:
    """`True` is an int in Python and would arrive as $1.00."""
    history([1.0])
    d = se.sessions_dir() / "bt-2026-09-02-000000"
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({"cost_total": True}))
    assert se.observed_session_costs() == [1.0]


# ---------------------------------------------------------------------------
# the derivation
# ---------------------------------------------------------------------------

def test_the_ceiling_is_derived_and_says_so(history, monkeypatch) -> None:
    history([0.10] * 19 + [1.00])
    monkeypatch.setenv("JARVIS_COCKPIT_CAP_HEADROOM", "3")
    usd, basis = se.derived_cost_cap()
    assert usd > 0
    assert "observed" in basis and "sessions" in basis


def test_no_history_is_reported_as_unmeasured(monkeypatch, tmp_path) -> None:
    """The fallback is a real number, and must never be mistaken for one that
    was measured."""
    monkeypatch.setenv("JARVIS_SESSIONS_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_COCKPIT_COST_CAP", raising=False)
    usd, basis = se.derived_cost_cap()
    assert usd > 0
    assert "unmeasured" in basis


def test_a_quiet_history_lands_on_the_floor_and_admits_it(history, monkeypatch) -> None:
    """Pennies-per-session weeks are real. A ceiling derived from them would
    stop the first session that tried to do anything — so the floor holds, and
    the basis says the floor is why."""
    history([0.001] * 30)
    monkeypatch.setenv("JARVIS_COCKPIT_CAP_FLOOR", "0.50")
    usd, basis = se.derived_cost_cap()
    assert usd == pytest.approx(0.50)
    assert "floor" in basis


# ---------------------------------------------------------------------------
# the statistical trap
# ---------------------------------------------------------------------------

def test_a_natural_tail_is_not_treated_as_censored() -> None:
    """Today's real history: one session at the top, the rest spread. That is
    an ordinary tail and the estimate should use it."""
    assert se._censored([0.1, 0.5, 1.4, 1.7, 2.3, 2.5]) is False


def test_a_pile_up_at_the_maximum_is_detected_as_censoring() -> None:
    """Several sessions within a hair of one value is a wall, not a
    coincidence — the old cap stopping them and recording the stop."""
    assert se._censored([0.1, 0.5, 2.50, 2.4999, 2.4998]) is True


def test_a_censored_history_estimates_from_the_body_and_says_so(
        history, monkeypatch) -> None:
    history([0.05] * 12 + [2.50, 2.4999, 2.4998])
    monkeypatch.setenv("JARVIS_COCKPIT_CAP_FLOOR", "0.01")
    _usd, basis = se.derived_cost_cap()
    assert "censored" in basis, basis


# ---------------------------------------------------------------------------
# not competing with the budget that already exists
# ---------------------------------------------------------------------------

def test_it_never_proposes_more_than_aegis_allows(history, monkeypatch) -> None:
    """Two budgets means the effective limit is whichever one nobody
    remembered. A ceiling above the enforced cap is a number that can never be
    reached, displayed as though it were the limit."""
    history([5.0] * 20)
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "2.00")
    usd, basis = se.derived_cost_cap()
    assert usd == pytest.approx(2.00)
    assert "Aegis" in basis
    assert "would allow" in basis


def test_a_generous_aegis_cap_does_not_inflate_the_ceiling(
        history, monkeypatch) -> None:
    """The clamp is a ceiling, not a target."""
    history([0.10] * 20)
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "999")
    usd, _ = se.derived_cost_cap()
    assert usd < 10.0


# ---------------------------------------------------------------------------
# the operator, and never failing a boot
# ---------------------------------------------------------------------------

def test_an_explicit_operator_cap_wins_and_is_labelled(history, monkeypatch) -> None:
    history([0.10] * 20)
    monkeypatch.setenv("JARVIS_COCKPIT_COST_CAP", "0.50")
    assert se.cockpit_cost_cap() == ("0.50", "operator")


@pytest.mark.parametrize("bad", ["abc", "-1", "", "  "])
def test_malformed_knobs_fall_back_rather_than_crash(
        history, monkeypatch, bad) -> None:
    history([0.10] * 20)
    monkeypatch.setenv("JARVIS_COCKPIT_CAP_HEADROOM", bad)
    usd, _ = se.derived_cost_cap()
    assert usd > 0


def test_the_literal_is_gone_from_both_call_sites() -> None:
    """Structural. The defect was DUPLICATION as much as the magic number: two
    copies drift, and the survivor is discovered by an operator wondering why
    the screen disagrees with their config."""
    import inspect
    from backend.core.ouroboros.cli import thin_client

    src = inspect.getsource(thin_client)
    # One tolerated occurrence: the last-resort fallback if the estimator
    # itself cannot be imported.
    assert src.count('"2.50"') <= 1, "the literal is still duplicated"
    assert src.count("_cockpit_cost_cap()") >= 2, (
        "both the detached spawn and the launchd agent must share it"
    )
