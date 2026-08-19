"""The ambient deck — severity wins slots, not arrival order.

A FIFO ring treats every event as equally worth a slot, so the loudest
producer takes the screen. The agora posts on its own initiative and never
stops; a five-row FIFO becomes five rows of personas, and the line that said
"DoubleWord failed over" is gone in under a second. That is a monitoring
surface losing the only row that mattered, to a joke.

These tests pin the three properties that prevent it: operational rows pin,
social compacts rather than evicting, and the addressed/ambient split sends
command answers to the scrollback while ambient chatter goes to the deck.
"""
from __future__ import annotations

from typing import List

import pytest

from backend.core.ouroboros.battle_test.ambient_deck import (
    DeckManager,
    Severity,
    classify,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _texts(deck: DeckManager) -> List[str]:
    return [t for _s, t in deck.rows()]


# --------------------------------------------------------------------------
# 1. the DOS case — the requirement
# --------------------------------------------------------------------------

def test_social_flood_cannot_evict_a_fatal_row() -> None:
    """The whole reason the deck is not a deque."""
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=3)
    deck.push("DW provider failover", severity=Severity.FATAL, key="dw")

    for i in range(50):
        clock.t += 0.1
        deck.push(f"@the-pit joke #{i}", severity=Severity.SOCIAL,
                  author="@the-pit")

    rows = _texts(deck)
    assert any("failover" in r for r in rows), (
        "50 social events buried the FATAL row — the deck is a FIFO again"
    )
    assert "dw" in deck.pinned_keys()


def test_a_full_operational_deck_compacts_social_instead_of_evicting() -> None:
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=2)
    deck.push("budget exhausted", severity=Severity.FATAL, key="budget")
    deck.push("DW degraded", severity=Severity.WARN, key="dw")

    for i in range(3):
        clock.t += 0.1
        deck.push(f"@cassandra post {i}", severity=Severity.SOCIAL,
                  author="@cassandra")

    rows = _texts(deck)
    assert len(rows) == 2, "the deck grew past its row budget"
    assert any("budget exhausted" in r for r in rows)
    assert any("DW degraded" in r for r in rows)
    assert deck.hidden_social == 3
    # The count still surfaces — hidden is not the same as discarded.
    assert any("hidden" in r for r in rows), (
        "chatter was withheld with no indication any existed"
    )


def test_social_takes_a_free_slot_when_one_exists() -> None:
    deck = DeckManager(clock=_Clock(), rows=4)
    deck.push("DW degraded", severity=Severity.WARN, key="dw")
    deck.push("@cassandra says hello", severity=Severity.SOCIAL,
              author="@cassandra")
    rows = _texts(deck)
    assert any("cassandra" in r for r in rows)


def test_the_agora_is_one_voice_on_the_deck() -> None:
    """Many residents talking must not consume many rows — otherwise a lively
    conversation evicts info rows one at a time."""
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=4)
    for who in ("@cassandra", "@the-pit", "@the-skeptic", "@ouroboros"):
        clock.t += 0.1
        deck.push(f"{who} weighs in", severity=Severity.SOCIAL, author=who)
    social_rows = [t for s, t in deck.rows() if s is Severity.SOCIAL]
    assert len(social_rows) == 1, f"agora took {len(social_rows)} rows"


# --------------------------------------------------------------------------
# 2. pinning + resolution
# --------------------------------------------------------------------------

def test_operational_rows_outrank_info_regardless_of_recency() -> None:
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=2)
    deck.push("DW failover", severity=Severity.WARN, key="dw")
    for i in range(5):
        clock.t += 0.1
        deck.push(f"info {i}", severity=Severity.INFO, key=f"i{i}")
    assert any("failover" in r for r in _texts(deck))


def test_severity_orders_the_rows() -> None:
    deck = DeckManager(clock=_Clock(), rows=4)
    deck.push("chatter", severity=Severity.SOCIAL, author="@x")
    deck.push("info line", severity=Severity.INFO, key="i")
    deck.push("fatal line", severity=Severity.FATAL, key="f")
    deck.push("warn line", severity=Severity.WARN, key="w")
    order = [s for s, _t in deck.rows()]
    assert order == sorted(order, reverse=True), "rows are not severity-ordered"
    assert deck.rows()[0][1] == "fatal line"


def test_resolution_is_the_intended_exit_for_a_pinned_row() -> None:
    deck = DeckManager(clock=_Clock(), rows=3)
    deck.push("DW degraded", severity=Severity.WARN, key="dw")
    assert deck.resolve("dw") is True
    assert not any("degraded" in r for r in _texts(deck))
    assert deck.resolve("dw") is False


def test_a_pinned_row_expires_on_its_own_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL is the backstop, not the mechanism — but it must exist, or a
    resolved-but-unreported condition pins forever."""
    monkeypatch.setenv("JARVIS_AMBIENT_DECK_OP_TTL_S", "10")
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=3)
    deck.push("DW degraded", severity=Severity.WARN, key="dw")
    clock.t += 11
    assert _texts(deck) == []


def test_a_repeating_producer_updates_one_row(
) -> None:
    """A flapping provider must not fill the deck with itself."""
    clock = _Clock()
    deck = DeckManager(clock=clock, rows=4)
    for i in range(10):
        clock.t += 0.1
        deck.push(f"DW failover (attempt {i})", severity=Severity.WARN,
                  key="dw")
    assert len(deck.rows()) == 1
    assert "attempt 9" in deck.rows()[0][1]


# --------------------------------------------------------------------------
# 3. bulletproof
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, "", "   ", 12345, object()])
def test_push_never_raises_on_junk(junk: object) -> None:
    deck = DeckManager(clock=_Clock(), rows=3)
    deck.push(junk)  # type: ignore[arg-type]


def test_rows_never_raises_even_with_a_broken_clock() -> None:
    def _boom() -> float:
        raise RuntimeError("clock on fire")

    deck = DeckManager(clock=_boom, rows=3)
    assert deck.rows() == []


def test_classify_recognises_operational_language() -> None:
    assert classify("DW provider failover")[0] is Severity.WARN
    assert classify("budget exhausted, cannot continue")[0] is Severity.FATAL
    assert classify("🐍 @cassandra said something")[0] is Severity.SOCIAL
    assert classify("post", kind="molt_post")[0] is Severity.SOCIAL
    assert classify("read 4 files")[0] is Severity.INFO


# --------------------------------------------------------------------------
# 4. the addressed/ambient split at the client
# --------------------------------------------------------------------------

def test_addressed_goes_to_scrollback_ambient_goes_to_the_deck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routing rule, asserted at the seam where both frames arrive."""
    from backend.core.ouroboros.cli import ov

    scrollback: List[str] = []
    monkeypatch.setattr(
        ov, "_render_markup_frame",
        lambda text, console=None: scrollback.append(text),
    )
    ui = ov.AttachUI()

    def _markup_sink(text: str, addressed: bool = False) -> None:
        if addressed:
            ov._render_markup_frame(text, None)
            return
        ui.on_ambient(text)

    _markup_sink("[bold]🐍 Moltbook[/bold] — 12 posts", addressed=True)
    _markup_sink("🐍 @cassandra: the soak refused", addressed=False)

    assert scrollback == ["[bold]🐍 Moltbook[/bold] — 12 posts"], (
        "the command answer did not reach the permanent scrollback"
    )
    assert any("cassandra" in t for t in _texts(ui.deck)), (
        "the ambient post did not reach the deck"
    )
    assert not any("Moltbook" in t for t in _texts(ui.deck)), (
        "an addressed answer leaked into the ephemeral deck and will vanish"
    )


def test_live_region_grows_above_the_caret() -> None:
    """The pulse and deck render ABOVE the input line (operator layout), so
    the block is part of the prompt, not the bottom toolbar.

    The contract is POSITION. This asserted line COUNT as a proxy for "the
    deck row appeared" -- ``len(splitlines()) > base_lines`` -- and the proxy
    is wrong at exactly one point: the FIRST row does not grow the block, it
    SUPERSEDES the cold-boot ignition skeleton (``⏺ awaiting daemon
    telemetry…``), which occupies that slot precisely so the gap before
    hydration does not read as an empty screen. Cold 3 lines -> one post 3
    lines -> two posts 4. The row was there and named in the output the whole
    time; only the counter disagreed.

    So assert the property directly -- the row is present and ABOVE the caret
    -- and keep the growth check where growth genuinely happens, on the post
    that has no placeholder left to consume.
    """
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    cold = ui.prompt().splitlines()
    ui.on_ambient("DW provider failover")
    out = ui.prompt().splitlines()

    assert any("failover" in ln for ln in out), "the deck row did not appear"
    assert out[-1].strip().endswith("›"), (
        "the caret must be the LAST line — the live region sits above it"
    )
    # ...and it is genuinely IN the region, not merely somewhere in the block.
    assert any("failover" in ln for ln in out[:-1])
    # The skeleton was replaced, not accumulated beside the real row.
    assert not any("awaiting daemon" in ln for ln in out), (
        "the ignition skeleton outlived the first real row"
    )
    assert len(out) == len(cold)

    # A second post has no placeholder to consume, so NOW the block grows.
    ui.on_ambient("second ambient post")
    grown = ui.prompt().splitlines()
    assert len(grown) > len(out), "a second deck row did not extend the region"
    assert grown[-1] == out[-1], "the caret moved when the region grew"


def test_deck_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.ouroboros.cli import ov

    monkeypatch.setenv("JARVIS_AMBIENT_DECK", "0")
    ui = ov.AttachUI()
    ui.on_ambient("DW provider failover")
    assert "failover" not in ui.prompt(), "OFF must hide the deck rows"
