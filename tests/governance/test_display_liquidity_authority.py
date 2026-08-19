"""A ceiling is a policy cap; a balance is money. The cockpit conflated them.

The bottom line read ``$0.00/$0.71`` while Claude answered 400 `credit balance
too low` and DoubleWord answered 402 — presenting $0.71 of headroom that
nothing could spend. Worse, the "⚠ … dry" token that exists for exactly this
case stayed silent, and the capability badge reported `healthy — lanes viable`.

ROOT CAUSE — a routing predicate answering a display question.

Every display consumer asked `provider_liquidity_ledger.runway_exhausted`,
which folds in `quota_exhausted`, which is `t < quota_exhausted_until`: a
WINDOW test. `record_quota_exhaustion` is TTL-bounded so a topped-up wallet
resumes routing without manual clearing — correct for routing, and it means the
predicate fail-opens the moment the window lapses. Both windows had lapsed
(84.0h and 6.4h), and the ledger still declared 5,000,000 tokens for Anthropic
from before the money ran out, so the token path agreed it was fine.

`economic_state.economic_view` already drew the distinction, in `unverified_since`
and in its own words: *routing keeps its fail-open optimism; the display stops
calling it knowledge*. `display_liquidity` makes that callable, once, for every
surface that shows money to a human.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.core.ouroboros.governance import economic_state as es


def _seed(tmp_path, monkeypatch, rows):
    """Write a ledger and point every reader at it."""
    path = tmp_path / "provider_liquidity.json"
    path.write_text(json.dumps({"providers": rows}))
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH", str(path))
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pl
    pl._read_cache.clear()          # mtime-keyed; a fresh path needs a fresh read
    return path


def _economic(reason, *, until):
    return {"quota_reason": f"class=economic::{reason}",
            "quota_exhausted_until": until}


class TestALapsedWindowIsNotPayment:
    """The defect, stated as a test."""

    def test_a_lapsed_economic_flag_is_still_dry_for_display(
            self, tmp_path, monkeypatch):
        past = time.time() - 3600            # window closed an hour ago
        _seed(tmp_path, monkeypatch,
              {"anthropic": _economic("Error code: 400 - credit balance too low",
                                      until=past)})
        view = es.display_liquidity()
        lane = view["lanes"]["anthropic"]
        assert lane["dry"] is True, (
            "a lapsed outage window means time passed, not that anyone paid"
        )
        assert lane["verified"] is False, (
            "an assumption must not be reported with the certainty of a "
            "measurement"
        )
        assert lane["stale_for_s"] and lane["stale_for_s"] >= 3599

    def test_the_routing_predicate_still_fails_open(self, tmp_path, monkeypatch):
        """Pins WHY the swap was needed — and that routing is left alone.

        `runway_exhausted` must keep its optimism: it is what lets a topped-up
        wallet resume without manual clearing. This test fails the day someone
        "fixes" it in place, which would change dispatch behaviour rather than
        display.
        """
        past = time.time() - 3600
        _seed(tmp_path, monkeypatch, {"anthropic": _economic("400", until=past)})
        from backend.core.ouroboros.governance import provider_liquidity_ledger as pl
        assert pl.runway_exhausted("anthropic") is False
        assert es.display_liquidity()["lanes"]["anthropic"]["dry"] is True

    def test_a_live_window_is_dry_AND_verified(self, tmp_path, monkeypatch):
        future = time.time() + 600
        _seed(tmp_path, monkeypatch, {"anthropic": _economic("402", until=future)})
        lane = es.display_liquidity()["lanes"]["anthropic"]
        assert lane["dry"] is True and lane["verified"] is True

    def test_rate_limited_is_dry_but_never_unfunded(self, tmp_path,
                                                    monkeypatch):
        """The two reasons a lane is unusable demand OPPOSITE actions.

        A rate-limited lane is genuinely dry — it earns the "⚠ … dry" token
        and its reset countdown — but it is not out of money. A funding
        verdict built on the display union would send an operator to a billing
        page to fix something a clock fixes, which is why `economic_dry` is
        kept apart from `dry` rather than derived from it.
        """
        _seed(tmp_path, monkeypatch, {
            "anthropic": {"quota_reason": "class=rate_limited::429 slow down",
                          "quota_exhausted_until": time.time() + 600}})
        view = es.display_liquidity()
        assert view["lanes"]["anthropic"]["dry"] is True
        assert view["lanes"]["anthropic"]["kind"] == "runway"
        assert view["economic_dry"] == []
        assert view["all_economic_dry"] is False
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        assert StatusLineBuilder()._sample_funding() == ("", ""), (
            "a per-minute token cap must never qualify the ceiling as unfunded"
        )

    def test_a_token_runway_exhaustion_is_dry_even_with_money(
            self, tmp_path, monkeypatch):
        """The case a straight predicate swap silently dropped.

        `runway_exhausted` answered TWO questions — economic outage AND
        declared tokens below the floor. Replacing it with the economic half
        made a lane with 500 tokens left render as perfectly healthy, because
        the money was fine and only the runway was gone. Display dryness is
        the UNION.
        """
        _seed(tmp_path, monkeypatch, {
            "anthropic": {"tokens_remaining": 500,
                          "reset_delta_s": 600,
                          "recorded_unix": time.time()}})
        lane = es.display_liquidity()["lanes"]["anthropic"]
        assert lane["dry"] is True
        assert lane["kind"] == "runway"
        assert es.display_liquidity()["economic_dry"] == []

    def test_no_lanes_recorded_is_ignorance_not_insolvency(
            self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, {})
        view = es.display_liquidity()
        assert view["all_dry"] is False, "an empty roster must never read broke"
        assert view["any_dry"] is False


class TestFractionalLiquidity:
    """The edge case a blanket verdict gets exactly wrong.

    One lane out does NOT make the ceiling inert — the funded lane can still
    spend the whole of it. So the decision is made over the lane ROSTER, never
    an any()/all() collapse of one boolean, and `partial` keeps the denominator
    while naming only the lane that is actually out.
    """

    def test_one_dry_lane_does_not_condemn_the_funded_one(
            self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, {
            "doubleword": _economic("status 402", until=time.time() - 60),
            "anthropic": {},                       # healthy, nothing recorded
        })
        view = es.display_liquidity()
        assert view["dry"] == ["doubleword"]
        assert view["funded"] == ["anthropic"]
        assert view["any_dry"] is True
        assert view["all_dry"] is False, (
            "a working Anthropic key must not be reported as broke"
        )

    def test_the_status_line_names_the_lane_not_the_fleet(
            self, tmp_path, monkeypatch):
        _seed(tmp_path, monkeypatch, {
            "doubleword": _economic("402", until=time.time() - 60),
            "anthropic": {},
        })
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        exhausted, label, _reset = StatusLineBuilder()._sample_liquidity()
        assert exhausted is True and label == "doubleword"
        mode, mode_label = StatusLineBuilder()._sample_funding()
        assert mode == "partial" and mode_label == "doubleword"

    def test_every_lane_dry_collapses_to_one_honest_phrase(
            self, tmp_path, monkeypatch):
        past = time.time() - 60
        _seed(tmp_path, monkeypatch, {
            "anthropic": _economic("400", until=past),
            "doubleword": _economic("402", until=past),
        })
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        exhausted, label, _reset = StatusLineBuilder()._sample_liquidity()
        assert exhausted is True
        assert label == "all lanes", (
            "naming an arbitrary first offender hides that BOTH are out"
        )


class TestTheCeilingIsQualifiedNeverDeleted:
    """`$0.71` stays on screen when it can still be spent."""

    @pytest.mark.parametrize("mode,label,expect,forbid", [
        ("",         "",           "$0.00/$0.71",       "unfunded"),
        ("partial",  "doubleword", "⚠ doubleword dry",  "unfunded"),
        ("unfunded", "",           "$0.00/$0.71 unfunded", "local tier"),
        ("local",    "rtx",        "local tier",        "/$0.71"),
    ])
    def test_the_chip_says_which_kind_of_number_it_is(
            self, mode, label, expect, forbid):
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            format_idle_breadcrumb,
        )
        out = format_idle_breadcrumb(cost_spent=0.0, cost_budget=0.71,
                                     detail="22 sensors", funding=mode,
                                     funding_label=label)
        assert expect in out
        assert forbid not in out

    def test_a_funded_fleet_renders_exactly_as_before(self):
        """No behaviour change on the healthy path."""
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            format_idle_breadcrumb,
        )
        assert format_idle_breadcrumb(
            cost_spent=0.04, cost_budget=0.50, detail="22 sensors",
        ) == "IDLE · 22 sensors · $0.04/$0.50"


class TestLocalTierMorph:
    """All paid lanes dry is not paralysis when something else is serving."""

    def test_a_serving_local_tier_replaces_the_dollar_ceiling(
            self, tmp_path, monkeypatch):
        past = time.time() - 60
        _seed(tmp_path, monkeypatch, {
            "anthropic": _economic("400", until=past),
            "doubleword": _economic("402", until=past),
        })
        from backend.core.ouroboros.governance import capability_state as cs
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_remote",
                            staticmethod(lambda: ("serving", "http://h:11434",
                                                  True)))
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        mode, label = StatusLineBuilder()._sample_funding()
        assert mode == "local" and label == "http://h:11434", (
            "work continues at zero marginal cost — a dollar ceiling is no "
            "longer the binding constraint"
        )

    def test_an_unreachable_local_tier_is_not_a_lifeline(
            self, tmp_path, monkeypatch):
        past = time.time() - 60
        _seed(tmp_path, monkeypatch, {
            "anthropic": _economic("400", until=past),
            "doubleword": _economic("402", until=past),
        })
        from backend.core.ouroboros.governance import capability_state as cs
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_remote",
                            staticmethod(lambda: ("unreachable", "http://h",
                                                  True)))
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        assert StatusLineBuilder()._sample_funding()[0] == "unfunded"


class TestOneAuthority:
    def test_the_badge_and_the_status_line_cannot_disagree(
            self, tmp_path, monkeypatch):
        """Both now ask `display_liquidity`. Before, the badge said `healthy —
        lanes viable` while the wallet was empty, because both asked the
        fail-open routing predicate."""
        past = time.time() - 60
        _seed(tmp_path, monkeypatch, {
            "anthropic": _economic("400", until=past),
            "doubleword": _economic("402", until=past),
        })
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        from backend.core.ouroboros.governance.capability_state import (
            CapabilityEvaluator,
        )
        line_dry = StatusLineBuilder()._sample_liquidity()[0]
        ev = CapabilityEvaluator()
        ev.invalidate()
        badge_dry = ev._read_lanes()[0]
        assert line_dry is badge_dry is True

    def test_the_render_path_never_touches_the_network(self):
        """`_sample_funding` runs at ~500ms on the UI thread. It reads the
        gateway's in-memory breaker snapshot; a LAN round-trip here would be a
        worse defect than the one this fixes."""
        import ast
        import inspect
        import textwrap
        from backend.core.ouroboros.battle_test.status_line import StatusLineBuilder
        src = textwrap.dedent(inspect.getsource(
            StatusLineBuilder._sample_funding))
        called = {
            n.func.attr for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "resident_models" not in called
        assert "ensure_model_resident" not in called
