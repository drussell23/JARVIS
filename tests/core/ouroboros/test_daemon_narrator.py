"""Tests for DaemonNarrator — rate-limited voice for Ouroboros autonomous events (TDD).

All tests are pure-asyncio with zero network, zero model calls, and zero I/O.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, call

import pytest

from backend.core.ouroboros.daemon_narrator import DaemonNarrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_say_fn() -> AsyncMock:
    """Return an async mock for say_fn that records all calls."""
    fn = AsyncMock(return_value=True)
    return fn


# ---------------------------------------------------------------------------
# Construction / state
# ---------------------------------------------------------------------------


class TestDaemonNarratorConstruction:
    def test_narrator_starts_enabled(self) -> None:
        narrator = DaemonNarrator(say_fn=_make_say_fn())
        assert narrator.enabled is True

    def test_narrator_can_start_disabled(self) -> None:
        narrator = DaemonNarrator(say_fn=_make_say_fn(), enabled=False)
        assert narrator.enabled is False

    def test_narrator_no_say_fn_allowed(self) -> None:
        narrator = DaemonNarrator(say_fn=None)
        assert narrator.enabled is True  # enabled flag is independent of say_fn

    def test_rate_limit_stored(self) -> None:
        narrator = DaemonNarrator(say_fn=_make_say_fn(), rate_limit_s=120.0)
        assert narrator.rate_limit_s == 120.0


# ---------------------------------------------------------------------------
# Basic speech — known events fire say_fn
# ---------------------------------------------------------------------------


class TestKnownEventsSpeech:
    @pytest.mark.asyncio
    async def test_epoch_start_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        await narrator.on_event("rem.epoch_start", {})
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "REM" in message

    @pytest.mark.asyncio
    async def test_epoch_complete_speaks_summary(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"findings_count": 3, "envelopes_submitted": 2}
        await narrator.on_event("rem.epoch_complete", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "3" in message
        assert "2" in message

    @pytest.mark.asyncio
    async def test_saga_complete_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"title": "WhatsApp Integration", "step_count": 5}
        await narrator.on_event("saga.complete", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "WhatsApp" in message

    @pytest.mark.asyncio
    async def test_synthesis_complete_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"hypothesis_count": 7}
        await narrator.on_event("synthesis.complete", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "7" in message

    @pytest.mark.asyncio
    async def test_saga_started_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"title": "Calendar Sync", "step_count": 4}
        await narrator.on_event("saga.started", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "Calendar Sync" in message

    @pytest.mark.asyncio
    async def test_saga_aborted_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"reason": "timeout"}
        await narrator.on_event("saga.aborted", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "timeout" in message

    @pytest.mark.asyncio
    async def test_governance_patch_applied_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"description": "fix null pointer"}
        await narrator.on_event("governance.patch_applied", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "fix null pointer" in message

    @pytest.mark.asyncio
    async def test_vital_warn_speaks(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        payload: Dict[str, Any] = {"warning_count": 2}
        await narrator.on_event("vital.warn", payload)
        say.assert_called_once()
        message = say.call_args[0][0]
        assert "2" in message


# ---------------------------------------------------------------------------
# say_fn call contract — keyword args
# ---------------------------------------------------------------------------


class TestSayFnCallContract:
    @pytest.mark.asyncio
    async def test_say_fn_called_with_source_kwarg(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        await narrator.on_event("rem.epoch_start", {})
        _, kwargs = say.call_args
        assert kwargs.get("source") == "ouroboros_narrator"

    @pytest.mark.asyncio
    async def test_say_fn_called_with_skip_dedup_kwarg(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        await narrator.on_event("rem.epoch_start", {})
        _, kwargs = say.call_args
        assert kwargs.get("skip_dedup") is True


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limiting_same_category_drops_second(self) -> None:
        """Two events of the same category within rate_limit_s → second dropped."""
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=60.0)
        await narrator.on_event("rem.epoch_start", {})
        await narrator.on_event("rem.epoch_complete", {"findings_count": 1, "envelopes_submitted": 0})
        # Both are "rem" category — second should be rate-limited
        say.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limiting_zero_allows_all(self) -> None:
        """rate_limit_s=0 disables rate limiting."""
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        await narrator.on_event("rem.epoch_start", {})
        await narrator.on_event("rem.epoch_start", {})
        assert say.call_count == 2

    @pytest.mark.asyncio
    async def test_different_categories_not_rate_limited(self) -> None:
        """Events in different categories are each allowed independently."""
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=60.0)
        await narrator.on_event("rem.epoch_start", {})
        await narrator.on_event("synthesis.complete", {"hypothesis_count": 5})
        await narrator.on_event("saga.complete", {"title": "Feat A", "step_count": 3})
        assert say.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_expires_allows_next(self) -> None:
        """After rate_limit_s elapses the category is unblocked."""
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.001)
        await narrator.on_event("rem.epoch_start", {})
        await asyncio.sleep(0.01)  # let the rate window expire
        await narrator.on_event("rem.epoch_complete", {"findings_count": 0, "envelopes_submitted": 0})
        assert say.call_count == 2


# ---------------------------------------------------------------------------
# Disabled narrator
# ---------------------------------------------------------------------------


class TestDisabledNarrator:
    @pytest.mark.asyncio
    async def test_disabled_narrator_silent(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0, enabled=False)
        await narrator.on_event("rem.epoch_start", {})
        await narrator.on_event("synthesis.complete", {"hypothesis_count": 3})
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_say_fn_silent(self) -> None:
        """No say_fn provided — on_event must not raise."""
        narrator = DaemonNarrator(say_fn=None, rate_limit_s=0.0)
        await narrator.on_event("rem.epoch_start", {})  # must not raise


# ---------------------------------------------------------------------------
# Unknown / malformed events
# ---------------------------------------------------------------------------


class TestUnknownEvents:
    @pytest.mark.asyncio
    async def test_unknown_event_ignored(self) -> None:
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        await narrator.on_event("totally.unknown", {"key": "value"})
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_payload_key_uses_raw_template(self) -> None:
        """If payload is missing a key referenced by the template, raw template used."""
        say = _make_say_fn()
        narrator = DaemonNarrator(say_fn=say, rate_limit_s=0.0)
        # rem.epoch_complete requires findings_count + envelopes_submitted
        await narrator.on_event("rem.epoch_complete", {})  # missing keys
        say.assert_called_once()
        # Should not raise; raw template used (contains brace placeholders)
        message = say.call_args[0][0]
        assert isinstance(message, str)
        assert len(message) > 0
