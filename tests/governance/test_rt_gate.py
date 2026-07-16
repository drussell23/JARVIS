"""RT Gate Router — Claude-RT-first gate completions (Phase 2 positioning).

Proves the ONE unified wrapper the 8 synchronous gates route through:
Claude-RT first (injected provider, then the provider-less Aegis fallback),
DW-RT (complete_sync — never the batch queue) as the opportunistic fallback,
typed GateProviderExhaustedError on full exhaustion, master-flag rollback to
DW-first ordering, and hang-bounding via wait_for.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.core.ouroboros.claude_fallback as cf
import backend.core.ouroboros.governance.rt_gate as rtg


class _SyncResult:
    def __init__(self, content):
        self.content = content
        self.model = "dw-x"
        self.latency_s = 0.2


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _dead_claude_fallback(monkeypatch):
    monkeypatch.setattr(cf, "claude_inference",
                        AsyncMock(side_effect=RuntimeError("claude down")))


# ---------------------------------------------------------------------------
# Ordering: Claude-RT first
# ---------------------------------------------------------------------------


def test_injected_claude_wins_dw_never_called(monkeypatch):
    claude = MagicMock()
    claude.prompt_only = AsyncMock(return_value="claude says")
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("dw says"))
    out = _run(rtg.gate_completion(
        "p", caller_id="t", claude_provider=claude, dw_provider=dw))
    assert out == "claude says"
    dw.complete_sync.assert_not_called()          # time bought, tokens saved


def test_providerless_claude_fallback_used_when_no_injected(monkeypatch):
    monkeypatch.setattr(cf, "claude_inference", AsyncMock(return_value="cf says"))
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("dw says"))
    out = _run(rtg.gate_completion("p", caller_id="t", dw_provider=dw))
    assert out == "cf says"
    dw.complete_sync.assert_not_called()


def test_dw_rt_is_the_opportunistic_fallback(monkeypatch):
    """Both Claude tiers down → the gate degrades to DW-RT, not dead."""
    _dead_claude_fallback(monkeypatch)
    claude = MagicMock()
    claude.prompt_only = AsyncMock(side_effect=RuntimeError("529 overloaded"))
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("dw says"))
    out = _run(rtg.gate_completion(
        "p", caller_id="t", claude_provider=claude, dw_provider=dw))
    assert out == "dw says"
    kwargs = dw.complete_sync.await_args.kwargs
    assert "timeout_s" in kwargs                  # RT primitive, caller-timed


def test_master_off_restores_dw_first(monkeypatch):
    monkeypatch.setenv("JARVIS_GATE_CLAUDE_FIRST_ENABLED", "false")
    claude = MagicMock()
    claude.prompt_only = AsyncMock(return_value="claude says")
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("dw says"))
    out = _run(rtg.gate_completion(
        "p", caller_id="t", claude_provider=claude, dw_provider=dw))
    assert out == "dw says"                       # one-flip rollback
    claude.prompt_only.assert_not_called()


# ---------------------------------------------------------------------------
# Exhaustion + bounding
# ---------------------------------------------------------------------------


def test_full_exhaustion_raises_typed_error(monkeypatch):
    _dead_claude_fallback(monkeypatch)
    dw = MagicMock()
    dw.complete_sync = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(rtg.GateProviderExhaustedError):
        _run(rtg.gate_completion("p", caller_id="t", dw_provider=dw))


def test_no_providers_at_all_raises(monkeypatch):
    _dead_claude_fallback(monkeypatch)
    with pytest.raises(rtg.GateProviderExhaustedError):
        _run(rtg.gate_completion("p", caller_id="t"))


def test_hanging_tier_is_bounded(monkeypatch):
    """A hung provider (the batch-stall class) cannot wedge a gate."""
    _dead_claude_fallback(monkeypatch)

    async def _hang(*a, **k):
        await asyncio.sleep(3600)

    claude = MagicMock()
    claude.prompt_only = _hang
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("dw says"))
    out = _run(asyncio.wait_for(
        rtg.gate_completion("p", caller_id="t", claude_provider=claude,
                            dw_provider=dw, timeout_s=5.0),
        timeout=30,
    ))
    assert out == "dw says"


def test_empty_responses_cascade_not_return(monkeypatch):
    """An empty completion is a tier failure, never a returned ''."""
    monkeypatch.setattr(cf, "claude_inference", AsyncMock(return_value=""))
    claude = MagicMock()
    claude.prompt_only = AsyncMock(return_value="")
    dw = MagicMock()
    dw.complete_sync = AsyncMock(return_value=_SyncResult("real content"))
    out = _run(rtg.gate_completion(
        "p", caller_id="t", claude_provider=claude, dw_provider=dw))
    assert out == "real content"


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


def test_timeout_env_parsing(monkeypatch):
    monkeypatch.delenv("JARVIS_GATE_RT_TIMEOUT_S", raising=False)
    assert rtg.gate_rt_timeout_s() == 60.0
    monkeypatch.setenv("JARVIS_GATE_RT_TIMEOUT_S", "25")
    assert rtg.gate_rt_timeout_s() == 25.0
    monkeypatch.setenv("JARVIS_GATE_RT_TIMEOUT_S", "junk")
    assert rtg.gate_rt_timeout_s() == 60.0
    monkeypatch.setenv("JARVIS_GATE_RT_TIMEOUT_S", "1")
    assert rtg.gate_rt_timeout_s() == 5.0         # floor


def test_master_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_GATE_CLAUDE_FIRST_ENABLED", raising=False)
    assert rtg.claude_first_enabled() is True
