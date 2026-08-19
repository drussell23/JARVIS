"""The nameplate must not collide with the artwork, and every lane must speak.

Two defects from a live cockpit screenshot:

  1. The capability reason is long enough to overflow the identity column, and
     a wrapped continuation starts at COLUMN ZERO — on top of the crest. The
     header read "…configure a local lane to keep" followed by "background work
     moving" printed over the logo.
  2. The banner rendered "a runway is reported dry but no provider row shows
     it" — its own contradiction — because the daemon's economic payload
     consulted the CLAUDE circuit breaker alone and never represented
     Doubleword, the lane actually refusing with 402.
"""
from __future__ import annotations

import inspect

import pytest
from rich.text import Text

from backend.core.ouroboros.cli.ov import _liquidity_lines
from backend.core.ouroboros.ui.crest_animator import render_cockpit_header

LONG = ("● unfunded · ouroboros + venom · doubleword is out of credit — add "
        "credits, or configure a local lane to keep background work moving")


class TestTheNameplateStaysInItsColumn:
    def _rows(self, width, lines=None):
        lines = lines or [Text("O+V v0.1.0"), Text(LONG), Text("~/repo")]
        return render_cockpit_header(None, lines, width).split("\n")

    @pytest.mark.parametrize("width", [60, 80, 100, 120, 200])
    def test_no_row_ever_exceeds_the_terminal_width(self, width):
        """A row wider than the terminal wraps, and a wrapped continuation
        starts at column zero — which is where the crest is."""
        assert all(len(r) <= width for r in self._rows(width))

    def test_the_row_count_is_stable_regardless_of_reason_length(self):
        """The header is a NAMEPLATE — a fixed number of rows beside a
        fixed-height crest. If it grew, the layout would shift every time a
        provider changed state."""
        short = [Text("O+V v0.1.0"), Text("● healthy"), Text("~/repo")]
        long_ = [Text("O+V v0.1.0"), Text(LONG), Text("~/repo")]
        assert len(self._rows(80, short)) == len(self._rows(80, long_))

    def test_a_long_reason_is_ellipsised_not_wrapped(self):
        rows = self._rows(80)
        assert "…" in rows[1]
        assert "background work moving" not in "\n".join(rows)

    def test_the_callers_text_is_not_mutated(self):
        """`lines` is rebuilt per frame but shared within one; truncating in
        place would make the first narrow frame permanent."""
        reason = Text(LONG)
        before = len(reason)
        render_cockpit_header(None, [Text("x"), reason, Text("y")], 60)
        assert len(reason) == before

    def test_a_tiny_terminal_still_renders(self):
        for w in (20, 10, 1):
            assert isinstance(render_cockpit_header(None, [Text(LONG)], w), str)


class TestEveryLaneSpeaks:
    def test_the_daemon_asks_the_classifier_not_one_breaker(self):
        """`economic_view` handles Doubleword's 402 and Anthropic's 400 +
        'credit balance' alike, and is the same function the banner's renderer
        consumes. One source, every lane."""
        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        assert "economic_view" in src

    def test_a_populated_economic_map_removes_the_contradiction(self):
        """With lanes represented, the banner names them instead of reporting
        that it cannot."""
        out = " ".join(_liquidity_lines(
            {"anthropic": {"tokens_remaining": 5_000_000},
             "doubleword": {"tokens_remaining": None}},
            any_exhausted=True,
            economic={"doubleword": {"state": "economic", "hard_open": True,
                                     "reason": "status 402"}}))
        assert "OUT OF CREDIT" in out
        assert "no provider row shows it" not in out

    def test_the_contradiction_warning_survives_a_genuinely_empty_map(self):
        """It is still the honest thing to say when nothing can explain the
        aggregate flag — the fix is to populate the map, not to silence the
        warning."""
        out = " ".join(_liquidity_lines(
            {"anthropic": {"tokens_remaining": 5_000_000}},
            any_exhausted=True, economic={}))
        assert "no provider row shows it" in out

    def test_a_lapsed_lane_is_still_represented(self):
        out = " ".join(_liquidity_lines(
            {"anthropic": {"tokens_remaining": 5_000_000}},
            any_exhausted=True,
            economic={"anthropic": {"state": "unknown", "stale_clock": True,
                                    "unverified_since": 0.0,
                                    "reason": "Your credit balance is too low"}}))
        assert "last known OUT OF CREDIT" in out
        assert "no provider row shows it" not in out
