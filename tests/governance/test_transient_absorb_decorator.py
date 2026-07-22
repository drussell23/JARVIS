"""Idempotent Transient-Absorb Decorator — state-safe per-round self-healing.

Mandated bulletproof: a DW round emits a partial (corrupted) JSON tool-call
into the ReAct transcript and then throws a synthetic Watchdog Abort. The
decorator must catch the abort, PURGE the partial artifact, RESTORE the
pre-round transcript, and successfully retry the round.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.stream_rupture import StreamRuptureError
from backend.core.ouroboros.governance.transient_absorb import (
    with_transient_absorb,
)


def _abort() -> StreamRuptureError:
    return StreamRuptureError(
        provider="doubleword", elapsed_s=0.3, bytes_received=12,
        rupture_timeout_s=0.2, phase="inter_chunk",
    )


async def test_partial_json_purged_state_restored_then_retry(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TRANSIENT_ABSORB_DECORATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_BACKOFF_BASE_S", "0.01")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_BACKOFF_CAP_S", "0.02")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_MAX_RETRIES", "2")

    # The ReAct message transcript — the mutable state a mid-stream abort
    # would corrupt. It starts with the prior clean turns.
    transcript = [{"role": "user", "content": "fix _topological_sort"}]
    pre_round = [dict(m) for m in transcript]
    calls = {"n": 0}

    @with_transient_absorb(transcript=lambda self: self["transcript"])
    async def _dw_round(self):
        calls["n"] += 1
        if calls["n"] == 1:
            # Round 1: the LLM streams a PARTIAL, corrupted tool-call, then the
            # watchdog fast-aborts mid-stream (socket torn down).
            self["transcript"].append(
                {"role": "assistant", "content": '{"tool":"edit_file","args":{"pa'}
            )
            raise _abort()
        # Round 2 (post-restore): clean completion.
        self["transcript"].append(
            {"role": "assistant", "content": '{"tool":"edit_file","args":{"path":"x"}}'}
        )
        return "clean-result"

    holder = {"transcript": transcript}
    result = await _dw_round(holder)

    # Retried exactly once (abort → restore → retry).
    assert calls["n"] == 2
    assert result == "clean-result"
    # The partial corrupted artifact was PURGED — the transcript now holds the
    # pre-round turns plus ONLY the clean round-2 assistant message.
    assert transcript[: len(pre_round)] == pre_round
    assert len(transcript) == len(pre_round) + 1
    assert transcript[-1]["content"].endswith('"path":"x"}}')
    assert not any("pa'" in m.get("content", "") for m in transcript)  # no partial


async def test_non_transient_error_not_retried(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TRANSIENT_ABSORB_DECORATOR_ENABLED", "true")
    calls = {"n": 0}

    @with_transient_absorb()
    async def _round():
        calls["n"] += 1
        raise ValueError("structural bug — retrying cannot help")

    with pytest.raises(ValueError):
        await _round()
    assert calls["n"] == 1  # not retried


async def test_disabled_runs_once_byte_identical(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TRANSIENT_ABSORB_DECORATOR_ENABLED", "false")
    calls = {"n": 0}

    @with_transient_absorb()
    async def _round():
        calls["n"] += 1
        raise _abort()

    with pytest.raises(StreamRuptureError):
        await _round()
    assert calls["n"] == 1  # master-off → single attempt, no absorb


async def test_budget_exhaustion_stops_retry(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TRANSIENT_ABSORB_DECORATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_TRANSIENT_MAX_RETRIES", "5")
    calls = {"n": 0}

    @with_transient_absorb(remaining_s=lambda: 0.0)  # no budget left
    async def _round():
        calls["n"] += 1
        raise _abort()

    with pytest.raises(StreamRuptureError):
        await _round()
    assert calls["n"] == 1  # zero budget → no retry despite transient
