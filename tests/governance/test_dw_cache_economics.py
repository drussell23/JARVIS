"""Adaptive Cache Economics Engine — frequency over length.

The static ``JARVIS_DW_PROMPT_CACHE_MIN_CHARS`` floor priced the wrong variable.
For a prefix used N times the cost in input-units is
``write_mult + (N-1)*read_mult`` cached vs ``N`` uncached — and every term scales
linearly with prefix length, so LENGTH CANCELS OUT. Profitability depends only
on reuse count. The floor therefore blocked a 1,500-char probe firing 40x/hour
(hugely profitable) while admitting a 15,000-char one-off (a guaranteed loss).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_cache_economics import (
    PromptSignatureTracker,
    break_even_uses,
    prompt_signature,
    reset_default_tracker,
    should_cache_write,
)

W, R = 1.25, 0.1   # provider defaults: write premium, read discount


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_default_tracker()
    yield
    reset_default_tracker()


# ---------------------------------------------------------------------------
# Break-even is DERIVED, never hardcoded
# ---------------------------------------------------------------------------


def test_break_even_derived_from_multipliers():
    # (1.25 - 0.1) / (1 - 0.1) = 1.278 -> first whole N that beats it is 2
    assert break_even_uses(W, R) == 2


def test_break_even_tracks_changed_economics():
    """A pricier write demands more reuse — the threshold must move with it,
    which a hardcoded constant could never do."""
    assert break_even_uses(3.0, 0.1) > break_even_uses(1.25, 0.1)
    # A free write is profitable immediately.
    assert break_even_uses(0.05, 0.1) == 1


def test_break_even_degenerate_read_multiplier():
    """If a cache read costs as much as fresh input, caching can NEVER pay —
    the engine must not claim a finite threshold."""
    assert break_even_uses(1.25, 1.0) > 1_000_000
    assert break_even_uses(1.25, 1.5) > 1_000_000


# ---------------------------------------------------------------------------
# (1) small + frequent  ->  CACHE
# ---------------------------------------------------------------------------


def test_small_high_frequency_prompt_triggers_cache_write():
    """A 1,000-char Sentinel-probe-shaped prompt — far below the old 4,096
    floor — must be cached once reuse is demonstrated."""
    trk = PromptSignatureTracker(ttl_s=3600)
    probe = "S" * 1000

    first, why1 = should_cache_write(probe, write_mult=W, read_mult=R, tracker=trk, now=100.0)
    assert first is False, "first sighting has no reuse evidence — must not pay the write premium"
    assert why1["reason"] == "insufficient_reuse"

    second, why2 = should_cache_write(probe, write_mult=W, read_mult=R, tracker=trk, now=101.0)
    assert second is True, "a demonstrated repeat must be cached regardless of length"
    assert why2["uses_in_window"] == 2
    assert why2["chars"] == 1000


def test_high_frequency_probe_stays_cached_across_many_firings():
    """40 firings in an hour — the scenario the char floor silently excluded."""
    trk = PromptSignatureTracker(ttl_s=3600)
    probe = "P" * 1500
    decisions = [
        should_cache_write(probe, write_mult=W, read_mult=R, tracker=trk, now=float(i * 90))[0]
        for i in range(40)
    ]
    assert decisions[0] is False          # first pays nothing
    assert all(decisions[1:]), "sustained reuse must stay cached"


# ---------------------------------------------------------------------------
# (2) huge + one-off  ->  BYPASS
# ---------------------------------------------------------------------------


def test_massive_zero_frequency_prompt_bypasses_cache_write():
    """A 15,000-char one-off would sail past the old floor and lose 0.25x on a
    write that is never read back."""
    trk = PromptSignatureTracker(ttl_s=3600)
    giant = "G" * 15000

    ok, why = should_cache_write(giant, write_mult=W, read_mult=R, tracker=trk, now=100.0)

    assert ok is False, "a one-off must not pay the write premium"
    assert why["reason"] == "insufficient_reuse"
    assert why["chars"] == 15000


def test_length_alone_never_decides():
    """The core inversion: a tiny frequent prefix outranks a giant rare one."""
    trk = PromptSignatureTracker(ttl_s=3600)
    tiny, giant = "t" * 200, "g" * 50000

    should_cache_write(tiny, write_mult=W, read_mult=R, tracker=trk, now=1.0)
    tiny_ok, _ = should_cache_write(tiny, write_mult=W, read_mult=R, tracker=trk, now=2.0)
    giant_ok, _ = should_cache_write(giant, write_mult=W, read_mult=R, tracker=trk, now=3.0)

    assert tiny_ok is True and giant_ok is False


# ---------------------------------------------------------------------------
# (3) sliding window evicts — no memory leak
# ---------------------------------------------------------------------------


def test_window_evicts_stale_signatures():
    trk = PromptSignatureTracker(ttl_s=100.0)
    sig = prompt_signature("payload")

    trk.observe(sig, now=0.0)
    trk.observe(sig, now=10.0)
    assert trk.frequency(sig, now=20.0) == 2

    # Both observations fall outside a 100s window by t=200.
    assert trk.frequency(sig, now=200.0) == 0
    assert trk.size() == 0
    assert trk.distinct() == 0, "counts dict leaked after eviction"


def test_eviction_resets_the_cache_decision():
    """A prefix that stops recurring must stop being cached — otherwise the
    engine predicts hits against an already-expired provider cache."""
    trk = PromptSignatureTracker(ttl_s=100.0)
    p = "recurring"

    should_cache_write(p, write_mult=W, read_mult=R, tracker=trk, now=0.0)
    assert should_cache_write(p, write_mult=W, read_mult=R, tracker=trk, now=1.0)[0] is True

    later, why = should_cache_write(p, write_mult=W, read_mult=R, tracker=trk, now=5000.0)
    assert later is False, "stale history must not justify a write"
    assert why["uses_in_window"] == 1


def test_hard_entry_cap_bounds_memory_under_churn():
    """TTL alone cannot bound growth if every payload is unique — the cap is the
    backstop against unbounded memory."""
    trk = PromptSignatureTracker(ttl_s=10_000.0, max_entries=64)
    for i in range(500):
        trk.observe(prompt_signature(f"unique-{i}"), now=float(i))
    assert trk.size() <= 64
    assert trk.distinct() <= 64, "counts dict grew past the cap"


def test_counts_never_go_negative_under_repeated_eviction():
    trk = PromptSignatureTracker(ttl_s=50.0)
    sig = prompt_signature("x")
    for i in range(20):
        trk.observe(sig, now=float(i))
    for t in (100.0, 200.0, 300.0):
        assert trk.frequency(sig, now=t) == 0
    assert trk.distinct() == 0


# ---------------------------------------------------------------------------
# Robustness — economics must never break generation
# ---------------------------------------------------------------------------


def test_bad_input_never_raises_and_fails_closed():
    for bad in ("", None, 12345, b"bytes"):
        ok, why = should_cache_write(bad, write_mult=W, read_mult=R)  # type: ignore[arg-type]
        assert ok is False, f"must fail CLOSED on {bad!r}"
        assert "reason" in why


def test_observe_false_does_not_mutate_the_window():
    trk = PromptSignatureTracker(ttl_s=3600)
    p = "peek"
    should_cache_write(p, write_mult=W, read_mult=R, tracker=trk, now=1.0, observe=False)
    assert trk.size() == 0, "a read-only probe must not record a sighting"


def test_distinct_prefixes_do_not_share_credit():
    """Signature isolation: one prefix's frequency must never authorise another."""
    trk = PromptSignatureTracker(ttl_s=3600)
    a, b = "A" * 5000, "B" * 5000

    should_cache_write(a, write_mult=W, read_mult=R, tracker=trk, now=1.0)
    should_cache_write(a, write_mult=W, read_mult=R, tracker=trk, now=2.0)

    b_ok, why = should_cache_write(b, write_mult=W, read_mult=R, tracker=trk, now=3.0)
    assert b_ok is False and why["uses_in_window"] == 1


# ---------------------------------------------------------------------------
# Integration with the shaping chokepoint
# ---------------------------------------------------------------------------


def test_shape_cached_system_consults_acee(monkeypatch):
    """The ONE chokepoint both handlers consume must route through ACEE — and a
    sub-floor prefix must now become cacheable on repeat, which the old
    min_chars gate made impossible."""
    from backend.core.ouroboros.governance import doubleword_provider as d

    monkeypatch.setenv("JARVIS_DW_ACEE_ENABLED", "true")
    reset_default_tracker()
    small = "s" * 1000            # well under the legacy 4096 floor

    first = d._dw_shape_cached_system(small, enabled=True, min_chars=4096, ttl="1h")
    assert isinstance(first, str), "first sighting must stay uncached"

    second = d._dw_shape_cached_system(small, enabled=True, min_chars=4096, ttl="1h")
    assert isinstance(second, list), "repeat sub-floor prefix must now cache"
    assert second[0]["cache_control"]["type"] == "ephemeral"
    assert second[0]["text"] == small, "text must stay byte-identical"


def test_acee_disabled_restores_legacy_floor(monkeypatch):
    """Rollback must be byte-identical legacy."""
    from backend.core.ouroboros.governance import doubleword_provider as d

    monkeypatch.setenv("JARVIS_DW_ACEE_ENABLED", "false")
    reset_default_tracker()

    small = "s" * 1000
    for _ in range(5):
        assert isinstance(
            d._dw_shape_cached_system(small, enabled=True, min_chars=4096, ttl="1h"), str
        ), "legacy floor must still gate sub-floor prefixes"

    big = "b" * 5000
    assert isinstance(
        d._dw_shape_cached_system(big, enabled=True, min_chars=4096, ttl="1h"), list
    )
