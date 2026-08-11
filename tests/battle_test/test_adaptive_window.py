"""The flush valve, and the four wrong answers that preceded it.

`StreamMirror` batched tokens into cockpit frames on three module literals —
160 chars, 0.35s, 8192 cap — each a guess about a situation nobody measured.
Replacing them took five attempts, and four of them were falsified only by
running the thing. Those failures are pinned here because each one is a
plausible design somebody will re-propose:

  1. *past drain floor + text waiting*  -> one frame per token (the flood)
  2. *eta > drain period*               -> same; compares a HUMAN latency
                                           against a SOCKET latency
  3. *idle > token-gap envelope*        -> a uniform stream never surprises
                                           itself, so a 200ms trickle never
                                           emitted
  4. all of the above, defeated by      -> the measured gap silently included
     OBSERVER CONTAMINATION                our own drain time

The answer is a comparison across the two measured domains::

    idle > drain + jitter   generator slower than sink -> sink is STARVING,
                            no congestion possible     -> send the partial line
    idle <= drain + jitter  generator outruns the sink -> flushing floods it
                                                       -> accumulate to C = W

Both are times. No threshold decides "burst" or "trickle": the crossover is
wherever the two measurements happen to meet.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.battle_test.adaptive_window import (
    AdaptiveSignal,
    FlushPolicy,
)
from backend.core.ouroboros.battle_test.stream_mirror import StreamMirror

WORDS = ["the ", "model ", "emits ", "prose. ", "It has ",
         "spaces, ", "commas, ", "and stops. "]


# ---------------------------------------------------------------------------
# 1. the smoothing weight derives itself — no tau, no seed
# ---------------------------------------------------------------------------

def test_the_first_sample_is_adopted_whole() -> None:
    """Cold start needs no seed constant: with no history the deviation is 0,
    so alpha = residual/residual = 1 and the only observation wins."""
    s = AdaptiveSignal()
    s.observe(0.1)
    assert s.mean == pytest.approx(0.1)
    assert s.samples == 1


def test_a_stable_signal_collapses_alpha() -> None:
    """Samples inside the noise band must barely move the mean, or the
    'envelope' becomes a copy of the last value."""
    s = AdaptiveSignal()
    for x in (0.100, 0.101, 0.099, 0.100, 0.102, 0.098, 0.100):
        s.observe(x)
    assert s.mean == pytest.approx(0.100, abs=0.003)
    assert 0.0 < s.deviation < 0.01, "deviation should describe the noise band"


def test_a_shock_is_adopted_in_ONE_sample() -> None:
    """The whole point of a self-deriving weight: a regime change is
    surprising, and surprise IS the weight."""
    s = AdaptiveSignal()
    for _ in range(8):
        s.observe(0.005)
    before = s.mean
    s.observe(1.0)
    assert before < 0.01 and s.mean > 0.9, (
        f"a 200x shock moved the mean only {before:.4f} -> {s.mean:.4f}"
    )


@pytest.mark.parametrize("poison", [
    float("nan"), float("inf"), float("-inf"), -1.0, "x", None, object(),
])
def test_a_poisoned_sample_cannot_reach_the_mean(poison: Any) -> None:
    """This sits on the token hot path. One NaN in the mean would corrupt
    every future flush decision, silently and permanently."""
    s = AdaptiveSignal()
    s.observe(0.005)
    before = s.mean
    s.observe(poison)
    assert s.mean == before


def test_a_never_varying_signal_neither_drifts_nor_divides_by_zero() -> None:
    """residual == 0 and deviation == 0 is the one degenerate case. alpha = 0
    is the limit there, not a chosen fallback."""
    s = AdaptiveSignal()
    for _ in range(5):
        s.observe(0.5)
    assert (s.mean, s.deviation) == (0.5, 0.0)


def test_there_is_no_time_constant_to_tune() -> None:
    """Structural: the module must not acquire a tau/half-life/window under
    maintenance. The whole claim is that the weight is derived."""
    import inspect

    from backend.core.ouroboros.battle_test import adaptive_window as aw

    src = inspect.getsource(aw.AdaptiveSignal)
    for banned in ("tau", "half_life", "halflife", "window_size", "0.9", "0.1"):
        assert banned not in src, f"{banned!r} appeared in the smoothing"


# ---------------------------------------------------------------------------
# 2. the valve — measured against the SOCKET, not against itself
# ---------------------------------------------------------------------------

def _policy(cols: int = 80, rows: int = 24) -> FlushPolicy:
    p = FlushPolicy(display=lambda: (cols, rows))
    p.observe_drain(0.005)          # prime: T = 0.005
    return p


def test_a_starving_sink_releases_a_partial_line() -> None:
    """Generator slower than the sink: no congestion is possible, so holding
    text only makes the operator watch a still screen."""
    d = _policy().evaluate(buffered_chars=10, since_last_flush_s=0.21,
                           idle_s=0.200)
    assert d.should_flush and d.reason == "sink_starving"


def test_a_burst_accumulates_instead_of_flooding() -> None:
    """Generator outrunning the sink: flushing queues frames faster than they
    drain, which is the flood this module exists to prevent."""
    d = _policy().evaluate(buffered_chars=10, since_last_flush_s=0.006,
                           idle_s=0.001)
    assert not d.should_flush and d.reason == "filling"


def test_the_crossover_is_the_drain_envelope_and_nothing_else() -> None:
    """No literal defines 'burst' or 'trickle'. Straddle T and the decision
    must flip on that boundary alone."""
    p = _policy()
    t = p.time_threshold_s()
    below = p.evaluate(10, t + 0.001, idle_s=t * 0.99)
    above = p.evaluate(10, t + 0.001, idle_s=t * 1.01)
    assert not below.should_flush and above.should_flush


def test_a_full_line_always_goes_regardless_of_rate() -> None:
    d = _policy().evaluate(buffered_chars=80, since_last_flush_s=0.006,
                           idle_s=0.001)
    assert d.should_flush and d.reason == "line_full"


def test_the_socket_floor_outranks_a_full_line() -> None:
    """A burst is exactly when the floor matters, so a full buffer must not
    be able to override it."""
    d = _policy().evaluate(buffered_chars=999, since_last_flush_s=0.001,
                           idle_s=0.0005)
    assert not d.should_flush and d.reason == "drain_floor"


# ---------------------------------------------------------------------------
# 3. geometry — derived per decision, so SIGWINCH needs no invalidation
# ---------------------------------------------------------------------------

def test_thresholds_track_the_subscriber_geometry() -> None:
    assert _policy(80, 24).char_threshold() == 80
    assert _policy(200, 50).char_threshold() == 200
    assert _policy(80, 24).ring_cap() == 80 * 24


def test_a_mid_stream_resize_is_picked_up_on_the_next_decision() -> None:
    """SIGWINCH is asynchronous. Reading the display per decision means a
    resize takes effect immediately, with nothing to invalidate and no
    buffer to tear."""
    geom = [(80, 24)]
    p = FlushPolicy(display=lambda: geom[0])
    p.observe_drain(0.005)
    assert (p.char_threshold(), p.ring_cap()) == (80, 1920)
    geom[0] = (200, 50)
    assert (p.char_threshold(), p.ring_cap()) == (200, 10000)


def test_no_declared_display_disables_the_line_trigger() -> None:
    """Honest degradation: emit on a measured cadence rather than at an
    invented size. `0` must mean 'never trips', not 'flush always'."""
    p = FlushPolicy(display=None)
    p.observe_drain(0.005)
    assert p.char_threshold() == 0 and p.ring_cap() == 0


def test_a_broken_display_callback_cannot_break_a_decision() -> None:
    def _boom() -> Tuple[int, int]:
        raise RuntimeError("cockpit died mid-stream")

    p = FlushPolicy(display=_boom)
    p.observe_drain(0.005)
    assert p.char_threshold() == 0
    assert isinstance(p.evaluate(10, 1.0, idle_s=1.0).should_flush, bool)


# ---------------------------------------------------------------------------
# 4. observer contamination — the bug that defeated three formulations
# ---------------------------------------------------------------------------

def _drive(gaps: List[float], *, cols: int = 80, drain: float = 0.005) -> tuple:
    frames: List[str] = []
    t = [0.0]

    def sink(x: str) -> None:
        frames.append(x)
        t[0] += drain                      # draining ADVANCES the clock

    pol = FlushPolicy(display=lambda: (cols, 24))
    m = StreamMirror(sink, clock=lambda: t[0], policy=pol)
    for i, g in enumerate(gaps):
        t[0] += g
        m.on_token(WORDS[i % len(WORDS)])
    m.end()
    return frames, pol


def test_our_own_drain_time_is_not_counted_as_generator_idle() -> None:
    """THE bug. The wall gap between two tokens includes however long we
    spent inside the sink, so every flush inflated the next measured gap by
    exactly one drain period — guaranteeing idle > T forever and collapsing
    the policy to one frame per token. Three separate flush rules were
    debugged against this without seeing it.

    A burst must batch. If self-time leaks back into the measurement it
    cannot.
    """
    frames, _ = _drive([0.001] * 40)
    assert len(frames) < 10, (
        f"{len(frames)} frames for 40 tokens — the observer is inside its "
        "own observation again"
    )


# ---------------------------------------------------------------------------
# 5. end-to-end regimes
# ---------------------------------------------------------------------------

def test_a_fast_burst_batches() -> None:
    frames, _ = _drive([0.001] * 40)
    assert 1 <= len(frames) <= 8


def test_a_trickle_emits_promptly() -> None:
    """A slow stream must not be batched: the operator is watching."""
    frames, _ = _drive([0.200] * 20)
    assert len(frames) >= 15


def test_a_congested_socket_backs_off() -> None:
    fast, _ = _drive([0.001] * 40, drain=0.005)
    slow, _ = _drive([0.001] * 40, drain=0.050)
    assert len(slow) <= len(fast)


def test_the_valve_opens_and_closes_across_an_oscillating_stream() -> None:
    """The hard case: massive bursts alternating with agonising trickles.
    The valve must close for every burst token and open for every trickle
    one, on the drain boundary alone."""
    frames: List[str] = []
    t = [0.0]

    def sink(x: str) -> None:
        frames.append(x)
        t[0] += 0.005

    pol = FlushPolicy(display=lambda: (80, 24))
    seen: List[tuple] = []
    original = pol.evaluate

    def traced(buf: int, since: float, idle_s: float = 0.0):
        d = original(buf, since, idle_s=idle_s)
        seen.append((idle_s, d.reason))
        return d

    pol.evaluate = traced  # type: ignore[assignment]
    m = StreamMirror(sink, clock=lambda: t[0], policy=pol)
    for i, g in enumerate(([0.001] * 10 + [0.200] * 5) * 3):
        t[0] += g
        m.on_token(WORDS[i % len(WORDS)])

    burst = [r for idle, r in seen if idle < 0.05]
    trickle = [r for idle, r in seen if idle >= 0.05]
    assert "sink_starving" not in burst, (
        f"the valve opened during a burst: {dict(Counter(burst))}"
    )
    assert all(r in ("sink_starving", "line_full") for r in trickle), (
        f"a trickle token was held: {dict(Counter(trickle))}"
    )


def test_jitter_and_gc_spikes_do_not_swallow_a_trickle() -> None:
    """Drain variance inflates T. That is correct — but it must stay well
    below the token gap, or a stalled stream would be mistaken for a burst
    and held. Verified against spiky drain rather than assumed.
    """
    import random

    random.seed(7)
    frames: List[str] = []
    t = [0.0]

    def sink(x: str) -> None:
        frames.append(x)
        t[0] += 0.050 if random.random() < 0.12 else random.uniform(0.002, 0.012)

    pol = FlushPolicy(display=lambda: (80, 24))
    m = StreamMirror(sink, clock=lambda: t[0], policy=pol)
    for i, g in enumerate(([0.001] * 10 + [0.200] * 5) * 3):
        t[0] += g
        m.on_token(WORDS[i % len(WORDS)])

    assert pol.drain.deviation > 0.0, "the spiky harness produced no jitter"
    assert pol.drain.upper < 0.200, (
        f"T={pol.drain.upper:.4f} reached the trickle gap — a stalled stream "
        "would now be mistaken for a burst"
    )


# ---------------------------------------------------------------------------
# 6. absent a policy, nothing changes
# ---------------------------------------------------------------------------

def test_without_a_policy_the_legacy_path_is_untouched() -> None:
    """The policy is optional so this class stays usable with no capability
    layer, no cockpit and no daemon — and so the change is inert until wired."""
    frames: List[str] = []
    m = StreamMirror(frames.append, flush_chars=20, flush_interval_s=99.0,
                     clock=lambda: 0.0)
    for _ in range(10):
        m.on_token("hello world. ")
    assert frames, "the static path stopped emitting"


def test_the_drop_notice_is_the_SHARED_implementation() -> None:
    """DRY: ConsoleSpooler and StreamMirror must confess drops through one
    function, in their own units."""
    import inspect

    from backend.core.ouroboros.battle_test import spooled_console, stream_mirror

    for mod in (spooled_console, stream_mirror):
        assert "coalesced_drop_notice" in inspect.getsource(mod)
