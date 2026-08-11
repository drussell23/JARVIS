"""How often to send, derived from what is actually happening.

`StreamMirror` batches model tokens into committed frames for the cockpit.
Its flush policy was three module literals::

    _FLUSH_CHARS      = 160
    _FLUSH_INTERVAL_S = 0.35
    _MAX_BUFFER_CHARS = 8192

Every one of them is a guess about a situation nobody measured: how fast this
model emits, how quickly this socket drains, how wide this terminal is. A
200-column cockpit and an 80-column one get identically-sized frames; a
congested socket is pushed at the same rate as an idle one; a burst and a
trickle are batched the same way.

This derives all three from signals the stream already carries.

The three measurements
----------------------
``lam``   chars/sec arriving      — sampled from the gap between tokens
``drain`` seconds per frame       — sampled around the sink call
``jitter`` mean abs deviation of drain

Nothing is probed. Each is recorded where the data already passes.

The three derivations
---------------------
**Char threshold ``C = W``** — the subscriber's declared columns. One display
line is the natural frame unit: a frame that fills exactly one line on THAT
terminal. Wide cockpits get larger frames because they can absorb them.

**Time threshold ``T = drain + jitter``** — never emit faster than the sink
absorbs, plus its observed variance. This IS the backpressure mechanism and
it is self-regulating: congestion raises ``drain``, which widens ``T``, which
backs the producer off. No polling, no sleeps, no frame budget.

**Ring cap = ``W x H``** — the subscriber's screenful. Past one screen the
operator cannot read it anyway, so the oldest text is provably the right
thing to drop.

The behaviour is not coded, it falls out::

    burst        lam UP   -> C reached fast, T throttles  -> frames widen
    trickle      lam DOWN -> C rarely reached, drain small -> T fires promptly
    congestion   drain UP -> T UP                          -> back off
    resize       W change -> C and cap move with it

Eliminating the smoothing constant
----------------------------------
An EWMA needs a weight. The usual answer is a time constant ``tau`` and a
literal for it — which would put back exactly the kind of unmeasured guess
this module exists to remove.

So the weight is derived from the signal's own surprise::

    residual = |sample - mean|
    alpha    = residual / (residual + deviation)

``deviation`` is the running mean-absolute-deviation — the scale of "normal"
for this signal. The ratio is dimensionless and self-normalising:

  * a sample far outside normal variation (``residual >> deviation``) drives
    ``alpha -> 1``: adopt it, the world changed;
  * a sample inside the noise band (``residual << deviation``) drives
    ``alpha -> 0``: ignore it, that is just jitter;
  * ``residual ~ deviation`` gives ``alpha ~ 0.5``.

This is a normalised innovation — the same shape as a Kalman gain, where the
weight is the ratio of innovation to total expected variance. It answers the
"second derivative" question directly: ``residual`` measured against a
*moving* mean IS the acceleration term, and ``deviation`` is the scale that
makes it unitless.

Cold start needs no seed either. With no history ``deviation == 0``, so
``alpha = residual / residual = 1`` and the first sample is adopted whole —
which is the correct thing to do with the only observation you have.

The single degenerate case is ``residual == 0 and deviation == 0``: a signal
that has never varied. ``alpha = 0`` there is not a chosen default, it is the
limit — a sample identical to the mean carries no information, so the mean
does not move.

**There are no thresholds in this module.** Every number is measured or
derived from a measurement. The only literals are the mathematical identities
above (0 and 1 as the bounds of a ratio) and the structural guards that keep
division defined.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

logger = logging.getLogger("Ouroboros.AdaptiveWindow")

__all__ = ["AdaptiveSignal", "FlushPolicy", "FlushDecision"]


@dataclass
class AdaptiveSignal:
    """One scalar tracked with a self-deriving smoothing weight.

    Not a generic EWMA: the weight comes from the sample's own surprise
    relative to the running deviation, so there is no time constant to pick
    and no cold-start seed to invent. See the module docstring.

    NEVER raises. A non-finite or negative sample is refused rather than
    absorbed — a NaN reaching the mean would poison every future decision,
    and this sits on the token hot path.
    """

    mean: float = 0.0
    deviation: float = 0.0
    samples: int = 0

    def observe(self, sample: float) -> None:
        try:
            x = float(sample)
        except (TypeError, ValueError):
            return
        # Reject non-finite without importing math for one predicate: NaN is
        # the only value unequal to itself, and infinities blow up the ratio.
        if x != x or x in (float("inf"), float("-inf")):
            return
        if x < 0.0:
            return                      # durations and rates are non-negative

        if self.samples == 0:
            # No history: adopt. `deviation` stays 0 so the NEXT sample's
            # alpha is 1 as well, which is right — two points do not yet
            # describe a noise band.
            self.mean = x
            self.samples = 1
            return

        residual = abs(x - self.mean)
        denom = residual + self.deviation
        # residual == 0 and deviation == 0 -> a signal that has never varied.
        # alpha = 0 is the limit, not a fallback: a sample identical to the
        # mean carries no information.
        alpha = (residual / denom) if denom > 0.0 else 0.0

        self.mean += alpha * (x - self.mean)
        # The deviation tracks the same way, one order down: it is the scale
        # that makes the next residual dimensionless.
        self.deviation += alpha * (residual - self.deviation)
        self.samples += 1

    @property
    def upper(self) -> float:
        """mean + deviation — the value plus its observed variance.

        The quantity a scheduler actually wants: not "how long does this
        usually take" but "how long before I should be surprised".
        """
        return self.mean + self.deviation


@dataclass(frozen=True)
class FlushDecision:
    """Why a flush should or should not happen. Rendered into telemetry, so
    an operator can ask the window what it thinks it is doing."""

    should_flush: bool
    reason: str
    char_threshold: int
    time_threshold_s: float
    ring_cap: int


@dataclass
class FlushPolicy:
    """Derives the flush thresholds from live measurements. NEVER raises.

    `display` is injected rather than imported so this stays testable with no
    capability layer, no cockpit and no daemon — the same discipline
    `StreamMirror` already applies to its sink and clock. It returns
    ``(cols, rows)``; ``None`` means nothing has declared, and the policy
    falls back to *measurement-only* behaviour rather than to a literal.
    """

    display: Optional[Callable[[], Tuple[int, int]]] = None
    arrival: AdaptiveSignal = field(default_factory=AdaptiveSignal)
    drain: AdaptiveSignal = field(default_factory=AdaptiveSignal)
    #: Inter-token GAP, tracked alongside the rate. The rate answers "how
    #: much text is coming"; the gap answers "is it still coming". Only the
    #: second can distinguish a stream that is mid-burst from one that has
    #: stopped — and that is the question a partial line depends on.
    gap: AdaptiveSignal = field(default_factory=AdaptiveSignal)

    #: Set once a frame has actually been emitted. Before that the policy
    #: says "emit now" at the first safe boundary, so the first frame's own
    #: drain time becomes the first measurement. The system bootstraps from
    #: its own behaviour instead of from a guess.
    _primed: bool = False

    # -- measurement ingress ------------------------------------------------

    def observe_arrival(self, chars: int, gap_s: float) -> None:
        """One token arrived: `chars` characters, `gap_s` since the last.

        Records a RATE, not a duration, so a burst and a trickle are
        comparable. A zero gap (two tokens in the same clock tick) carries no
        rate information and is skipped rather than dividing by zero.
        """
        try:
            if gap_s > 0.0 and chars > 0:
                self.arrival.observe(float(chars) / float(gap_s))
                self.gap.observe(float(gap_s))
        except Exception:  # noqa: BLE001 — telemetry cannot break a stream
            pass

    def observe_drain(self, seconds: float) -> None:
        """One frame took `seconds` to hand to the sink."""
        try:
            self.drain.observe(seconds)
            self._primed = True
        except Exception:  # noqa: BLE001
            pass

    # -- derivations --------------------------------------------------------

    def _display(self) -> Optional[Tuple[int, int]]:
        """Read the display EVERY time, never cache.

        SIGWINCH is asynchronous: the operator can resize mid-stream, and a
        cached width would keep framing for a window that no longer exists.
        Reading per decision makes a resize take effect on the very next
        token, with no invalidation protocol and nothing to tear.
        """
        try:
            fn = self.display
            if fn is None:
                return None
            cols, rows = fn()
            cols, rows = int(cols), int(rows)
            return (cols, rows) if cols > 0 and rows > 0 else None
        except Exception:  # noqa: BLE001
            return None

    def char_threshold(self) -> int:
        """C — one display line at the subscriber's current width.

        Without a declared display there is no line to fill, so the char
        trigger is disabled (returned as 0 = "never trips") and the time
        threshold alone governs. That is the honest degradation: emit on a
        measured cadence rather than at an invented size.
        """
        d = self._display()
        return d[0] if d is not None else 0

    def time_threshold_s(self) -> float:
        """T — drain plus its jitter. Zero until the first frame has drained.

        Zero means "no minimum spacing yet", which is what lets the first
        frame go out immediately and produce the first measurement.
        """
        return self.drain.upper if self._primed else 0.0

    def ring_cap(self) -> int:
        """One screenful at the current geometry, or 0 for "no cap known".

        A caller seeing 0 must NOT invent one — it should hold the buffer
        uncapped rather than drop text on a guess. Unbounded growth is
        prevented by the cap existing the moment any cockpit declares.
        """
        d = self._display()
        return d[0] * d[1] if d is not None else 0

    # -- the decision -------------------------------------------------------

    def evaluate(self, buffered_chars: int, since_last_flush_s: float,
                 idle_s: float = 0.0) -> FlushDecision:
        """Should the mirror flush now? Pure; NEVER raises."""
        c = self.char_threshold()
        t = self.time_threshold_s()
        cap = self.ring_cap()

        if not self._primed:
            return FlushDecision(
                True, "cold_start", c, t, cap,
            )
        # The socket floor comes first: nothing may be emitted faster than the
        # sink has shown it can absorb, whatever the char count says. This is
        # the whole backpressure mechanism and it must not be overridable by
        # a burst — a burst is exactly when it matters.
        if since_last_flush_s < t:
            return FlushDecision(False, "drain_floor", c, t, cap)
        if c > 0 and buffered_chars >= c:
            return FlushDecision(True, "line_full", c, t, cap)
        if buffered_chars <= 0:
            return FlushDecision(False, "empty", c, t, cap)

        # Past the drain floor with a PARTIAL line. Whether to send it is the
        # question that decides burst behaviour, and answering it "yes" is
        # what turns a fast stream into one frame per token — the socket is
        # always ready a moment after a drain, so an eagerness test on
        # readiness alone degenerates to no batching at all.
        #
        # The real question is what waiting COSTS, and that is measurable:
        # how long until the rest of the line arrives.
        #
        #     eta = (C - buffered) / arrival_rate
        #
        # If the line will complete sooner than one drain period, waiting is
        # free — the frame we would send now and the fuller frame we send
        # after eta reach the operator at indistinguishable times, and the
        # fuller one costs the socket less. If it will take longer, waiting
        # is a stall the operator can see, so send.
        #
        # Both sides of the comparison are measured. Fast stream -> eta small
        # -> accumulate. Slow stream -> eta large -> emit promptly. No rate
        # threshold anywhere: the crossover is wherever the two measured
        # quantities happen to meet.
        # A PARTIAL line past the drain floor. Whether to send it is the
        # question that governs everything, and the answer is a comparison
        # BETWEEN THE TWO MEASURED DOMAINS rather than within either one.
        #
        #     idle_s  = the gap the generator just left between tokens
        #     t       = drain + jitter, what the sink needs per frame
        #
        # Both are times, so the comparison is dimensionally honest, and it
        # asks the only question that matters: can the sink keep up?
        #
        #   idle_s > t   the generator is slower than the sink. The socket is
        #                STARVING — there is no congestion to cause, so a
        #                partial line costs nothing and waiting only makes
        #                the operator watch a still screen. Send it.
        #
        #   idle_s <= t  the generator is outrunning the sink. Flushing now
        #                queues frames faster than they drain, which is the
        #                flood. Accumulate to C instead.
        #
        # Earlier attempts compared idle against the TOKEN envelope, which a
        # uniform stream can never exceed — a regular 200ms trickle is never
        # surprising to itself, so it never emitted. The gap has to be judged
        # against the sink, not against its own history.
        if idle_s > t:
            return FlushDecision(True, "sink_starving", c, t, cap)
        if c <= 0:
            # No declared width: no line to accumulate toward, so the drain
            # floor alone governs and there is nothing to wait for.
            return FlushDecision(True, "no_line_target", c, t, cap)
        return FlushDecision(False, "filling", c, t, cap)

    def snapshot(self) -> dict:
        """Observable state — §7. NEVER raises."""
        try:
            return {
                "arrival_chars_per_s": round(self.arrival.mean, 2),
                "arrival_deviation": round(self.arrival.deviation, 2),
                "drain_s": round(self.drain.mean, 4),
                "drain_jitter_s": round(self.drain.deviation, 4),
                "char_threshold": self.char_threshold(),
                "time_threshold_s": round(self.time_threshold_s(), 4),
                "ring_cap": self.ring_cap(),
                "primed": self._primed,
                "samples": {
                    "arrival": self.arrival.samples,
                    "drain": self.drain.samples,
                },
            }
        except Exception:  # noqa: BLE001
            return {}
