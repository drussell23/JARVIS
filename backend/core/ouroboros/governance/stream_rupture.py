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

def stream_rupture_timeout_s() -> float:
    """Phase 1 (TTFT): max seconds waiting for the first token.

    Deliberately generous to accommodate extended thinking models.
    """
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
