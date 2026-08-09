"""`/liquidity` was a rate-limit dashboard wearing a funding dashboard's name.

On 2026-08-08, with both paid lanes returning "account balance too low", it
rendered ``anthropic: 5,000,000 tokens · status: 200 · runway: ok`` while a
live `dw realtime` probe returned ``402 Account balance too low`` and every op
died at GENERATE. No field was lying about the thing it measured. There was no
field for the thing that was wrong.

The ledger went blind because `record_headers` returns early when a response
declares no rate-limit telemetry — and an economic refusal never does, since
the problem is not quota. The response that PROVES a lane is dead was the one
response the ledger discarded.

These pin the repair: a table-driven classifier that speaks every vendor's
economic dialect, a fold through the EXISTING `record_quota_exhaustion` hook,
a display that distinguishes hard-open from lapsed-on-a-timer, and a blast
radius that refuses to report a handoff into a lane that is equally dead.

Ledger writes are isolated to `tmp_path` — the bug that opened this arc was
fixture residue (`recorded_unix: 1010.0`, `tokens_remaining: 5000000`) left in
the real gitignored ledger by a historical un-isolated run.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance.economic_state import (
    ECONOMIC,
    HEALTHY,
    RATE_LIMITED,
    UNKNOWN,
    blast_radius,
    classify_refusal,
    economic_view,
    fold_economic_state,
)


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: Any):
    monkeypatch.setenv(
        "JARVIS_PROVIDER_LIQUIDITY_PATH", str(tmp_path / "liq.json"),
    )
    yield


# ---------------------------------------------------------------------------
# 1. the classifier speaks every dialect it has actually met
# ---------------------------------------------------------------------------

def test_the_two_shapes_that_broke_us_both_read_ECONOMIC() -> None:
    """Observed within one hour, from two vendors, with DIFFERENT statuses.

    A `status == 402` check catches DoubleWord and misses Anthropic — and
    Anthropic's was the one that had already tripped the Claude lane.
    """
    dw, _ = classify_refusal(
        402, "Account balance too low. Please add credits to continue.", {},
    )
    anthropic, _ = classify_refusal(
        400,
        {"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the "
                       "Anthropic API"}},
        {},
    )
    assert dw == ECONOMIC
    assert anthropic == ECONOMIC, "a status-only rule would miss this one"


def test_a_real_rate_limit_is_not_read_as_poverty() -> None:
    """An advertised reset window means a CLOCK fixes this. Calling it
    economic would send the operator to a billing page over a 20s wait."""
    v, why = classify_refusal(
        429, "Rate limit exceeded, try again in 20s", {"retry-after": "20"},
    )
    assert v == RATE_LIMITED
    assert "window" in why or "body" in why


def test_a_spend_cap_on_the_SAME_status_is_economic() -> None:
    """429 is the trap: providers use it for a per-minute ceiling AND a
    monthly spend cap. Status cannot decide; the phrase can."""
    v, _ = classify_refusal(429, "Monthly spending limit reached", {})
    assert v == ECONOMIC


def test_a_window_beats_a_money_word_only_when_no_money_word_is_present() -> None:
    """Precedence, pinned. A provider refusing you for funds has no window to
    advertise — but if it somehow sends both, the money word wins, because
    waiting will not fix an empty account."""
    assert classify_refusal(429, "slow down", {"retry-after": "5"})[0] == RATE_LIMITED
    assert classify_refusal(
        429, "credit balance too low", {"retry-after": "5"},
    )[0] == ECONOMIC


def test_403_auth_is_not_mistaken_for_403_billing() -> None:
    assert classify_refusal(403, "Billing account is past due", {})[0] == ECONOMIC
    assert classify_refusal(403, "Forbidden", {})[0] == UNKNOWN, (
        "a bare 403 is an auth problem; guessing economic sends the operator "
        "to top up an account that is fine"
    )


def test_success_is_not_a_refusal() -> None:
    assert classify_refusal(200, "{}", {})[0] == HEALTHY


def test_no_literal_status_comparison_survives_in_the_branching() -> None:
    """Mandate: table-driven, not `if status == 402`. The status numbers live
    in `_STATUS_CLASS` as DATA so a third vendor's shape is a data edit."""
    import inspect

    from backend.core.ouroboros.governance import economic_state as es

    src = inspect.getsource(es.classify_refusal)
    for literal in ("== 402", "==402", "!= 402", "== 403", "== 429"):
        assert literal not in src, f"{literal!r} hardcoded in the classifier"
    assert 402 in es._STATUS_CLASS, "402 must be table DATA, not absent"


# ---------------------------------------------------------------------------
# 2. resilience — a telemetry path may never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,headers", [
    (402, b"\xff\xfe malformed bytes", {}),
    (500, '{"truncated', {}),
    (None, None, None),
    (402, {"nested": {"deep": [1, 2, {"x": object()}]}}, {}),
    ("not-an-int", "whatever", {"retry-after": None}),
    (429, "", {"x": object()}),
    (402, "balance too low", object()),          # headers not map-like
])
def test_malformed_input_degrades_and_never_raises(status, body, headers) -> None:
    v, why = classify_refusal(status, body, headers)
    assert v in (ECONOMIC, RATE_LIMITED, HEALTHY, UNKNOWN)
    assert isinstance(why, str)


def test_a_decisive_status_still_classifies_through_an_unreadable_body() -> None:
    """402 means Payment Required whether or not the payload survived."""
    assert classify_refusal(402, b"\xff\xfe", {})[0] == ECONOMIC


# ---------------------------------------------------------------------------
# 3. the fold reuses the existing hook (DRY) and writes nothing else
# ---------------------------------------------------------------------------

def test_an_economic_refusal_lands_via_record_quota_exhaustion() -> None:
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    assert fold_economic_state(
        "doubleword", status=402,
        body="Account balance too low. Please add credits to continue.",
    ) == ECONOMIC
    assert pll.quota_exhausted("doubleword") is True
    row = (pll._load().get("providers") or {}).get("doubleword") or {}
    assert "quota_exhausted_until" in row, "used a store other than the hook"
    assert "balance too low" in row.get("quota_reason", "").lower(), (
        "the vendor's own words must survive to the operator"
    )


def test_a_rate_limit_does_NOT_write_an_economic_flag() -> None:
    """One condition, one writer. The header path already owns rate limits."""
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    assert fold_economic_state(
        "doubleword", status=429, body="try again in 20s",
        headers={"retry-after": "20"},
    ) == RATE_LIMITED
    assert pll.quota_exhausted("doubleword") is False


def test_the_fold_builds_no_new_storage() -> None:
    import inspect

    from backend.core.ouroboros.governance import economic_state as es

    src = inspect.getsource(es)
    assert "record_quota_exhaustion" in src
    for forbidden in ("sqlite", "open(", "shelve", "pickle"):
        assert forbidden not in src, f"{forbidden!r} — a second store appeared"


# ---------------------------------------------------------------------------
# 4. hard-open vs lapsed-on-a-timer (mandate 4)
# ---------------------------------------------------------------------------

def test_a_live_outage_reads_hard_open() -> None:
    fold_economic_state("doubleword", status=402, body="balance too low")
    v = economic_view("doubleword")
    assert v["state"] == ECONOMIC
    assert v["hard_open"] is True
    assert (v["expires_in_s"] or 0) > 0
    assert v["unverified_since"] is None


def test_a_lapsed_flag_is_UNVERIFIED_not_healthy(monkeypatch: Any) -> None:
    """THE display defect. The 1800s TTL self-heals so a topped-up wallet
    recovers without manual clearing — right for routing. But it is an
    ASSUMPTION: money does not return on a timer. On 2026-08-08 the flag
    lapsed at 14:42 and the dashboard said `ok` for hours while the account
    stayed empty."""
    monkeypatch.setenv("JARVIS_QUOTA_EXHAUSTION_TTL_S", "10")
    t0 = time.time()
    fold_economic_state(
        "doubleword", status=402, body="balance too low", now=t0,
    )
    later = economic_view("doubleword", now=t0 + 999)
    assert later["hard_open"] is False
    assert later["state"] == UNKNOWN, "a lapsed economic flag is not 'healthy'"
    assert later["unverified_since"] is not None


def test_a_lapsed_RATE_LIMIT_may_honestly_read_healthy(monkeypatch: Any) -> None:
    """The asymmetry is the point: a quota window really does reset on a
    clock, so its expiry IS evidence. An empty wallet's is not."""
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    monkeypatch.setenv("JARVIS_QUOTA_EXHAUSTION_TTL_S", "10")
    t0 = time.time()
    pll.record_quota_exhaustion("doubleword", reason="rate limit", now=t0)
    v = economic_view("doubleword", now=t0 + 999)
    assert v["unverified_since"] is None
    assert v["state"] == HEALTHY


def test_the_stale_clock_guard_catches_the_fixture_residue() -> None:
    """The row that started this: `recorded_unix: 1010.0` → 1969-12-31, left
    in the real gitignored ledger by a historical un-isolated test run. It
    silently poisoned every reset-horizon subtraction that read it.

    Production writes `time.time()`, so there is no production time bug to
    patch — the honest fix guards the READER, which trusted any float.
    """
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    pll.record_headers(
        "doubleword", {"x-ratelimit-remaining": "5000000"}, now=1010.0,
    )
    assert economic_view("doubleword")["stale_clock"] is True

    pll.record_headers(
        "anthropic", {"x-ratelimit-remaining": "5000000"}, now=time.time(),
    )
    assert economic_view("anthropic")["stale_clock"] is False


def test_a_malformed_row_degrades_without_crashing_the_renderer() -> None:
    import json

    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    p = pll._ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"providers": {
        "doubleword": {"quota_exhausted_until": "not-a-number",
                       "quota_reason": "class=economic :: x"}}}))
    v = economic_view("doubleword")
    assert v["state"] in (UNKNOWN, ECONOMIC, HEALTHY, RATE_LIMITED)


# ---------------------------------------------------------------------------
# 5. blast radius — cascading saturation (mandate 2)
# ---------------------------------------------------------------------------

class _Snap:
    def __init__(self, c_ok: bool, d_ok: bool) -> None:
        self.claude_available, self.claude_reason = c_ok, "" if c_ok else "breaker_open_economic"
        self.dw_healthy, self.dw_reason = d_ok, "" if d_ok else "transport_degraded"
        self.kimi_available, self.kimi_reason = False, ""


def _patch_availability(monkeypatch: Any, c_ok: bool, d_ok: bool) -> None:
    import backend.core.ouroboros.governance.provider_availability as pa

    monkeypatch.setattr(
        pa, "collect_provider_availability", lambda **_k: _Snap(c_ok, d_ok),
    )


def test_a_handoff_into_a_LIVE_lane_is_reported_as_a_handoff(
    monkeypatch: Any,
) -> None:
    _patch_availability(monkeypatch, c_ok=False, d_ok=True)
    br = blast_radius()
    assert br["lanes"]["claude"]["viable"] is False
    assert br["lanes"]["claude"]["absorbed_by"] == ["doubleword"]
    assert br["cascading"] == []


def test_a_handoff_into_a_DEAD_lane_is_a_cascading_route_failure(
    monkeypatch: Any,
) -> None:
    """The 2026-08-08 shape exactly: the generator logged `IMMEDIATE reroute →
    DW` and `DW AUTARKY ENGAGED` seconds before failing, because DW was also
    out of credit. Naming a fallback without checking it reports a successful
    handoff into a corpse."""
    _patch_availability(monkeypatch, c_ok=False, d_ok=False)
    br = blast_radius()
    assert set(br["cascading"]) == {"claude", "doubleword"}
    assert br["degraded"] is True
    for lane in ("claude", "doubleword"):
        assert br["lanes"][lane]["absorbed_by"] == []


def test_an_economically_dead_lane_is_not_viable_even_when_available(
    monkeypatch: Any,
) -> None:
    """Availability and solvency are different questions. A lane whose
    transport is fine but whose wallet is empty absorbs nothing."""
    _patch_availability(monkeypatch, c_ok=True, d_ok=True)
    fold_economic_state("doubleword", status=402, body="balance too low")
    br = blast_radius()
    assert br["lanes"]["doubleword"]["viable"] is False
    assert br["lanes"]["doubleword"]["economic"] == ECONOMIC
    assert br["lanes"]["claude"]["viable"] is True


def test_blast_radius_never_raises_when_sensing_fails(monkeypatch: Any) -> None:
    import backend.core.ouroboros.governance.provider_availability as pa

    def _boom(**_k):
        raise RuntimeError("sensing down")

    monkeypatch.setattr(pa, "collect_provider_availability", _boom)
    br = blast_radius()
    assert br == {"lanes": {}, "cascading": [], "degraded": False}


# ---------------------------------------------------------------------------
# 6. the dashboard actually consumes it (the wired-but-inert guard)
# ---------------------------------------------------------------------------

def test_the_liquidity_verb_actually_RENDERS_the_economic_column(
    monkeypatch: Any,
) -> None:
    """This codebase's recurring defect is a correct feature with no live
    caller — and the recurring reason it survives is a test that asserts on
    SOURCE TEXT, which cannot fail for a runtime reason.

    So this CALLS the verb and reads what it printed. (The first draft of this
    test did assert on source text, and failed only because a string was split
    across two lines for width — proving the point at my own expense.)
    """
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    _patch_availability(monkeypatch, c_ok=False, d_ok=False)
    fold_economic_state("doubleword", status=402, body="balance too low")

    printed: list = []
    h = BattleTestHarness.__new__(BattleTestHarness)
    monkeypatch.setattr(
        BattleTestHarness, "_repl_print",
        lambda _self, msg: printed.append(str(msg)),
    )
    h._repl_cmd_liquidity()

    out = "\n".join(printed)
    assert out, "the verb printed nothing"
    assert "blast radius" in out
    assert "CASCADING ROUTE" in out, (
        "a handoff into a dead lane was reported as a handoff"
    )
    assert "OUT OF CREDIT" in out, "the funding column never rendered"


def test_the_verb_survives_a_ledger_that_cannot_be_read(monkeypatch: Any) -> None:
    """Mandate 4: the renderer must not wedge the terminal."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    monkeypatch.setattr(pll, "_load", lambda: (_ for _ in ()).throw(OSError("x")))
    printed: list = []
    h = BattleTestHarness.__new__(BattleTestHarness)
    monkeypatch.setattr(
        BattleTestHarness, "_repl_print",
        lambda _self, msg: printed.append(str(msg)),
    )
    h._repl_cmd_liquidity()          # must not raise
    assert printed, "degraded silently instead of saying so"


def test_the_aegis_chokepoint_folds_every_upstream_response() -> None:
    """`record_headers` discards economic refusals (they carry no rate-limit
    headers), so the fold must sit at the ONE place every response crosses."""
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/ouroboros/aegis/forwarding.py").read_text()
    assert "fold_economic_state" in src
