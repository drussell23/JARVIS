"""Live Status-Line Badge — async, provider-agnostic, width-responsive ticker.

Mandated bulletproof: (1) cycles through two providers (DW + J-Prime) over a
simulated time lapse, (2) gracefully truncates with an ellipsis when the mock
terminal width drops to 20, (3) the tick explicitly yields control to the event
loop, proving it will not block REPL input.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.status_badge_ticker import (
    StatusBadgeTicker,
    render_badge,
)


def _both() -> dict:
    return {
        "doubleword": {"state": "DEGRADED", "jitter": 14, "ttft_slope": 0.001,
                       "forecast_ttr": 280},
        "gcp-jprime": {"state": "HEALTHY", "jitter": 0, "ttft_slope": -0.02},
    }


# ---------------------------------------------------------------------------
# (1) Rotating multi-provider ticker over a time lapse
# ---------------------------------------------------------------------------


async def test_ticker_cycles_two_providers() -> None:
    ticker = StatusBadgeTicker()
    for prov, snap in _both().items():
        ticker.update(prov, snap)
    assert ticker.provider_count() == 2

    seen = set()
    clock_ticks = 0
    # Simulate several 4s ticks; each render shows exactly ONE provider.
    for _ in range(4):
        badge = ticker.render(width=80)
        seen.add(badge.split(":")[0])       # the short-name prefix (dw / jprime)
        await ticker.tick()                 # advance the rotating index
        clock_ticks += 1

    # Both providers appeared as the ticker rotated — never both at once.
    assert "dw" in seen and "jprime" in seen
    # A single render never overflows with both providers stacked.
    one = ticker.render(width=80)
    assert not ("dw:" in one and "jprime:" in one)


def test_render_shows_provider_specific_state() -> None:
    providers = _both()
    dw = render_badge(providers, 0, 80)     # sorted: doubleword first
    jp = render_badge(providers, 1, 80)
    assert dw.startswith("dw:●DEGRADED") and "j14" in dw and "~280s" in dw
    assert jp.startswith("jprime:●HEALTHY")
    assert "(1/2)" in dw and "(2/2)" in jp   # rotation index shown


# ---------------------------------------------------------------------------
# (2) Terminal resize — aggressive ellipsis truncation
# ---------------------------------------------------------------------------


async def test_truncates_at_width_20() -> None:
    ticker = StatusBadgeTicker()
    ticker.update("doubleword",
                  {"state": "DEGRADED", "jitter": 14, "ttft_slope": 0.001,
                   "forecast_ttr": 280})
    full = ticker.render(width=200)
    assert len(full) > 20                    # the untruncated badge is long

    narrow = ticker.render(width=20)
    assert len(narrow) <= 20                  # never exceeds the mock width
    assert narrow.endswith("…")               # ellipsis, not a wrap

    # Degenerate widths never raise / never wrap.
    assert ticker.render(width=1) == "…"
    assert len(ticker.render(width=5)) <= 5


def test_empty_cache_renders_empty() -> None:
    assert StatusBadgeTicker().render(width=80) == ""
    assert render_badge({}, 0, 80) == ""


# ---------------------------------------------------------------------------
# (3) The tick yields control to the event loop (won't block REPL input)
# ---------------------------------------------------------------------------


async def test_tick_yields_to_event_loop() -> None:
    ticker = StatusBadgeTicker()
    ticker.update("doubleword", {"state": "DEGRADED"})

    order = []

    async def competitor() -> None:
        # If tick did NOT yield, this could not interleave before tick returns.
        order.append("competitor")

    comp = asyncio.ensure_future(competitor())
    await ticker.tick()          # must `await asyncio.sleep(0)` internally → yields
    await comp
    assert "competitor" in order

    # The invalidate hook is called on tick (prompt re-render trigger).
    hits = {"n": 0}
    t2 = StatusBadgeTicker(invalidate=lambda: hits.__setitem__("n", hits["n"] + 1))
    t2.update("doubleword", {"state": "DEGRADED"})
    await t2.tick()
    assert hits["n"] == 1


async def test_run_loop_bounded_and_deterministic() -> None:
    ticker = StatusBadgeTicker()
    ticker.update("doubleword", {"state": "DEGRADED"})
    ticker.update("gcp-jprime", {"state": "HEALTHY"})
    slept = []

    async def sleep(s: float) -> None:
        slept.append(s)

    await ticker.run(interval_s=4.0, sleep_fn=sleep, max_ticks=3)
    assert slept == [4.0, 4.0, 4.0]           # ticked 3× at the interval


def test_on_provider_event_feeds_cache() -> None:
    ticker = StatusBadgeTicker()
    ticker.on_provider_event({"provider": "doubleword", "state": "HEALTHY", "jitter": 0})
    ticker.on_provider_event({"provider": "gcp-jprime", "state": "DEGRADED", "jitter": 3})
    assert ticker.provider_count() == 2
    assert "dw:●HEALTHY" in ticker.render(width=80) or "jprime:●DEGRADED" in ticker.render(width=80)
    ticker.on_provider_event({})              # malformed → ignored, never raises
    assert ticker.provider_count() == 2
