"""Stream Rupture Breaker — shared exception + constants.

Provides the typed ``StreamRuptureError`` exception and the env-driven
timeout knobs used by both ClaudeProvider and DoublewordProvider to detect
and sever hung API streams.

Two-Phase Watchdog
------------------
Phase 1 (TTFT): Generous timeout while waiting for the first token.
    Deep-thinking models may pause 30-60s before emitting anything.
    Default: 120s via ``JARVIS_STREAM_RUPTURE_TIMEOUT_S``.

Phase 2 (Inter-Chunk): Tight timeout once streaming has started.
    If no chunk arrives for 30s after the stream is already producing
    tokens, the connection is ruptured.
    Default: 30s via ``JARVIS_STREAM_INTER_CHUNK_TIMEOUT_S``.

Authority Invariant
-------------------
This module imports only from stdlib. No governance, orchestrator, or
provider imports permitted.
"""
from __future__ import annotations

import os
from typing import Optional


# ---------------------------------------------------------------------------
# Env-driven timeout knobs
# ---------------------------------------------------------------------------

def stream_rupture_timeout_s(*, thinking_enabled: bool = False) -> float:
    """Phase 1 (TTFT): max seconds waiting for the first token.

    Deliberately generous to accommodate extended thinking models.

    Task #88 — thinking-aware TTFT (2026-05-13)
    -------------------------------------------
    When ``thinking_enabled=True``, the model emits ``thinking_delta``
    events through its REASONING phase BEFORE the first text content
    block.  The SDK's ``stream.text_stream`` filters those out — to
    a text-only consumer, the stream looks silent while the model is
    actively producing thinking output.  For complex prompts (e.g.
    17k-char SWE-Bench-Pro prompts under thinking_budget=16k tokens),
    thinking can legitimately run 3-5 minutes BEFORE text starts.

    The legacy 120s cap was insufficient for that regime — empirical
    evidence in v14-rev3/4/5 SWE-Bench-Pro soaks: 0 successful Claude
    completions across 30+ attempts, all with
    ``first_token=NEVER bytes_received=0 elapsed≥220s``.  Direct
    streaming probes from the same host succeed in 1.6s for trivial
    prompts WITHOUT thinking and stream thinking_delta events
    immediately when thinking is enabled, proving the API is healthy.

    The thinking-aware default (360s = 6 min) widens the TTFT window
    for thinking-enabled streams while keeping the legacy 120s for
    non-thinking calls.  Operator-tunable via the env knob below.

    Parameters
    ----------
    thinking_enabled:
        ``True`` iff the caller has enabled extended thinking on the
        SDK call (Anthropic ``thinking={"type":"enabled", ...}``).
        Default ``False`` preserves legacy behavior for callers
        unaware of the thinking-aware widening.

    Returns
    -------
    float
        Seconds.  Caller passes this as ``asyncio.wait_for(... timeout=)``
        for the first-token-arrival watchdog.
    """
    if thinking_enabled:
        # Thinking-aware widening — env-tunable.  Default 360s = 6 min,
        # which covers empirical thinking durations observed in
        # SWE-Bench-Pro prompts under thinking_budget=16k tokens.
        return float(
            os.environ.get("JARVIS_STREAM_RUPTURE_TIMEOUT_THINKING_S", "360")
        )
    return float(
        os.environ.get("JARVIS_STREAM_RUPTURE_TIMEOUT_S", "120")
    )


def stream_inter_chunk_timeout_s() -> float:
    """Phase 2 (Inter-Chunk): max seconds of silence after first token.

    Once tokens are flowing, a 30s gap signals a dead connection.
    """
    return float(
        os.environ.get("JARVIS_STREAM_INTER_CHUNK_TIMEOUT_S", "30")
    )


def cognitive_stall_timeout_s() -> float:
    """Slice 87 — max seconds a DW reasoning model may stream REASONING with
    ZERO functional content before the cognitive-stall watchdog fires and
    cascades to the Tier-1 fallback.

    Distinct from the inter-chunk watchdog (which sees reasoning bytes and stays
    alive): this measures elapsed-since-first-progress while ``content`` is still
    empty. Default 90s — comfortably above the legitimate reasoning-then-content
    band (direct probes: DW emits content at 21-34s at ``medium`` on a 21k-token
    prompt) yet far below the 240s budget the capability-stalled ops burned. So
    a model that WILL emit content is never killed mid-think, while a model stuck
    in a reasoning loop it can't exit cascades ~150s sooner. ``0`` disables the
    watchdog (legacy: wait the full primary budget). Invalid → default. Never
    raises."""
    raw = os.environ.get("JARVIS_DW_COGNITIVE_STALL_S", "").strip()
    if not raw:
        return 90.0
    try:
        v = float(raw)
        return v if v >= 0 else 90.0
    except ValueError:
        return 90.0


# ---------------------------------------------------------------------------
# Slice 12F-B — Budget-aware dispatch floor
# ---------------------------------------------------------------------------


def stream_minimum_read_budget_s() -> float:
    """Phase 3 (Budget Floor): the minimum ``wall_remaining`` budget
    required before dispatching a request to the provider.

    When semaphore wait or upstream cascade burns the op's wall
    budget down to a sliver, opening a stream is futile — the
    very first ``await __anext__`` will fire the inter-chunk
    watchdog at a misleading "no event for 0s" timeout. That
    looks like a network-side stream rupture but the actual cause
    is local: we never gave the stream a chance to talk.

    Slice 12F-B's contract: refuse to dispatch when
    ``wall_remaining < this floor`` — raise
    ``StreamBudgetTooShortError`` so the orchestrator's existing
    Slice 7 fallback handles it as a transient transport fault
    (RETRY_TRANSIENT, NOT terminal structural).

    Default: 10s — generous enough to cover Claude's typical
    TTFT for warm sessions; tight enough to fail fast when the
    op is genuinely starved.
    """
    return float(
        os.environ.get("JARVIS_STREAM_MINIMUM_READ_BUDGET_S", "10")
    )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class StreamRuptureError(RuntimeError):
    """Raised when a provider token stream goes silent beyond the watchdog.

    Carries structured diagnostic fields so the postmortem and FSM
    classifier can make informed decisions without parsing the message.

    Attributes
    ----------
    provider : str
        Provider name (``"claude-api"``, ``"doubleword"``).
    elapsed_s : float
        Total wall-clock seconds from stream open to rupture.
    bytes_received : int
        Total bytes of content received before the stream died.
    rupture_timeout_s : float
        The watchdog timeout that fired (Phase 1 or Phase 2 value).
    phase : str
        ``"ttft"`` (Phase 1 — no tokens ever arrived) or
        ``"inter_chunk"`` (Phase 2 — tokens were flowing, then stopped).
    """

    def __init__(
        self,
        *,
        provider: str,
        elapsed_s: float,
        bytes_received: int,
        rupture_timeout_s: float,
        phase: str = "ttft",
    ) -> None:
        self.provider = provider
        self.elapsed_s = elapsed_s
        self.bytes_received = bytes_received
        self.rupture_timeout_s = rupture_timeout_s
        self.phase = phase
        super().__init__(
            f"provider_stream_rupture:{provider}:"
            f"phase={phase}:"
            f"elapsed={elapsed_s:.1f}s:"
            f"bytes={bytes_received}:"
            f"timeout={rupture_timeout_s:.0f}s"
        )


class CognitiveStallError(StreamRuptureError):
    """Slice 87 — the stream is ALIVE (reasoning deltas flowing) but the model
    has emitted ZERO functional content for longer than the cognitive-stall
    watchdog. Distinct from a byte-level rupture: bytes ARE arriving, the model
    is just stuck in a reasoning loop it can't exit (the 240s/0-content stalls
    on capability-hard problems, sweep bt-2026-06-04-061913).

    Subclasses :class:`StreamRuptureError` so the FSM classifier's existing
    ``isinstance`` check routes it to TRANSIENT_TRANSPORT → an IMMEDIATE cascade
    to the Tier-1 fallback (Claude), instead of burning the rest of the DW
    primary budget. ``phase`` is fixed to ``"cognitive_stall"`` and the message
    carries a distinct ``cognitive_stall:`` prefix so postmortems can tell a
    reasoning-stall apart from a network rupture."""

    def __init__(
        self,
        *,
        provider: str,
        elapsed_s: float,
        bytes_received: int,
        stall_timeout_s: float,
        reasoning_seen: bool = True,
    ) -> None:
        self.reasoning_seen = reasoning_seen
        self.stall_timeout_s = stall_timeout_s
        super().__init__(
            provider=provider,
            elapsed_s=elapsed_s,
            bytes_received=bytes_received,
            rupture_timeout_s=stall_timeout_s,
            phase="cognitive_stall",
        )
        # Override the message with a distinct, greppable prefix.
        self.args = (
            f"cognitive_stall:{provider}:"
            f"elapsed={elapsed_s:.1f}s:"
            f"reasoning_seen={reasoning_seen}:"
            f"content_bytes={bytes_received}:"
            f"stall_timeout={stall_timeout_s:.0f}s",
        )


# ---------------------------------------------------------------------------
# Slice 12F-B — Budget-too-short diagnostic exception
# ---------------------------------------------------------------------------


class StreamBudgetTooShortError(RuntimeError):
    """Raised when the orchestrator declines to dispatch a stream
    because ``wall_remaining`` after semaphore acquisition is
    below the ``stream_minimum_read_budget_s`` floor.

    This is structurally distinct from ``StreamRuptureError``:
    a rupture is a *network-side* fault (the provider stopped
    sending bytes); a budget-too-short refusal is a *local*
    decision (we never gave the stream a chance to talk).
    Conflating the two in the previous "no event for 0s"
    rupture log was the diagnostic noise Slice 12F-B closes.

    Both errors map to ``FailureMode.TRANSIENT_TRANSPORT`` →
    ``RetryDecision.RETRY_TRANSIENT`` in the classifier, so the
    Slice 7 fallback handles them with the same backoff /
    failover profile. The distinction lives in the postmortem
    and in IDE / dashboard telemetry, not in the breaker policy.

    Attributes
    ----------
    provider : str
        Target provider that would have received the dispatch.
    op_id : str
        Truncated op_id for correlation.
    wall_remaining_s : float
        ``wall_rem`` measured *after* semaphore acquisition.
    minimum_required_s : float
        The ``stream_minimum_read_budget_s()`` floor at decision
        time (env-knobbed, default 10s).
    sem_wait_s : float
        How long the op waited on the semaphore — the dominant
        contributor to wall-budget consumption.
    route : str
        The op's ``ProviderRoute`` value, for postmortem
        correlation with the priority-gate ordering.
    """

    def __init__(
        self,
        *,
        provider: str,
        op_id: str,
        wall_remaining_s: float,
        minimum_required_s: float,
        sem_wait_s: float,
        route: str = "",
    ) -> None:
        self.provider = provider
        self.op_id = op_id
        self.wall_remaining_s = wall_remaining_s
        self.minimum_required_s = minimum_required_s
        self.sem_wait_s = sem_wait_s
        self.route = route
        super().__init__(
            f"provider_stream_budget_too_short:{provider}:"
            f"op={op_id}:"
            f"wall_remaining={wall_remaining_s:.2f}s:"
            f"floor={minimum_required_s:.1f}s:"
            f"sem_wait={sem_wait_s:.1f}s:"
            f"route={route}"
        )


# ---------------------------------------------------------------------------
# CD-1 — Lag-aware inter-chunk timeout compensation
# ---------------------------------------------------------------------------


def stream_lag_compensation_enabled() -> bool:
    """Master gate for CD-1 lag-credit compensation. Default OFF.

    When OFF the ``lag_compensated_inter_chunk_timeout_s`` helper is
    a transparent passthrough (returns ``base_s`` unchanged) so the
    behaviour is byte-identical to the pre-CD-1 code path.
    """
    return os.environ.get(
        "JARVIS_STREAM_LAG_COMPENSATION_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")


def _lag_credit_cap_s() -> float:
    """Maximum seconds of lag that may be credited back (env-tunable).

    Default 60s — chosen to be 2× the default inter-chunk timeout so
    a single starved loop cycle never inflates the budget beyond 90s,
    which remains well inside typical provider health thresholds.
    """
    return float(os.environ.get("JARVIS_STREAM_LAG_CREDIT_CAP_S", "60"))


def lag_compensated_inter_chunk_timeout_s(
    *,
    base_s: float,
    lag_credit_s: float,
    max_credit_s: "Optional[float]" = None,
) -> float:
    """Inter-chunk timeout extended by credited loop-lag (capped).

    When the event loop has been starved during the inter-chunk window
    the delay is NOT real network silence — the provider was streaming
    but the loop could not consume chunks in time. Crediting that lag
    back widens the watchdog window by exactly the loop-busy time so
    a flowing stream is never false-ruptured.

    Returns ``base_s`` unchanged when lag-compensation is OFF or the
    credit is zero/negative. Never raises.
    """
    if not stream_lag_compensation_enabled():
        return float(base_s)
    cap = max_credit_s if max_credit_s is not None else _lag_credit_cap_s()
    credit = max(0.0, min(float(lag_credit_s), float(cap)))
    return float(base_s) + credit
