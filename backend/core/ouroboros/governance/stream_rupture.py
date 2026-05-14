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
