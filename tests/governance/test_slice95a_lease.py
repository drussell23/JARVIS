"""Slice 95a — Aegis session-lease enforcement for LLMMutationProvider.

TDD regression spine: ALL tests mock acquire_call_lease / merge_lease_header /
the async client. ZERO live LLM or Aegis calls.

Test cases:
  (a) Aegis ENABLED + lease acquired → messages.create called WITH lease header.
  (b) Aegis ENABLED + acquire_call_lease returns None → AegisLeaseError raised,
      messages.create NEVER called.
  (c) Aegis ENABLED + acquire_call_lease raises → AegisLeaseError raised,
      messages.create NEVER called.
  (d) Aegis DISABLED → lease None → messages.create called WITHOUT lease header
      (direct path unchanged).
  (e) Aegis ENABLED + lease ok + MODEL call raises → swallowed → []
      (genuine per-mutation errors don't abort the run).
"""
from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEED = "x = 1\n"
_FENCE_RESPONSE = "```python\ny = x + 1\n```"
_LEASE_TOKEN = "lease-tok-95a"
_AEGIS_BRIDGE_PATH = "backend.core.ouroboros.governance.aegis_provider_bridge"
_AEGIS_CLIENT_IS_ENABLED_PATH = (
    "backend.core.ouroboros.aegis.client.is_enabled"
)


def _make_mock_response(text: str = _FENCE_RESPONSE) -> MagicMock:
    """Build a minimal mock Anthropic response with content text + usage."""
    content_block = MagicMock()
    content_block.text = text
    usage = MagicMock()
    usage.input_tokens = 50
    usage.output_tokens = 20
    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


def _make_mock_client(response: MagicMock) -> MagicMock:
    """Build an injectable mock async Anthropic client."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _run(coro):
    """Run a coroutine in the default event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# (a) Aegis ENABLED + lease acquired → messages.create called WITH lease header
# ---------------------------------------------------------------------------

class TestAegisEnabledLeaseAttached:
    """When Aegis is enabled and a lease is successfully obtained, the lease
    token MUST appear in the extra_headers kwarg of messages.create."""

    def test_lease_header_attached_to_messages_create(self):
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )
        from backend.core.ouroboros.governance.aegis_provider_bridge import (
            LEASE_HEADER_NAME,
        )

        mock_response = _make_mock_response()
        mock_client = _make_mock_client(mock_response)
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(return_value=_LEASE_TOKEN),
             ), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                 wraps=lambda h, t: {**(h or {}), LEASE_HEADER_NAME: t} if t else dict(h or {}),
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            results = _run(provider.mutate(_SEED, n=1))

        # messages.create MUST have been called
        assert mock_client.messages.create.called, (
            "messages.create was not called — lease path blocked the call"
        )

        # Retrieve the kwargs from the actual call
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs is not None

        # extra_headers must be present and contain the lease token
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        extra_headers = kwargs.get("extra_headers", {})
        assert LEASE_HEADER_NAME in extra_headers, (
            f"Expected '{LEASE_HEADER_NAME}' in extra_headers; got: {extra_headers}"
        )
        assert extra_headers[LEASE_HEADER_NAME] == _LEASE_TOKEN, (
            f"Lease token mismatch: expected {_LEASE_TOKEN!r}, "
            f"got {extra_headers[LEASE_HEADER_NAME]!r}"
        )

        # Mutations should still be returned (non-empty)
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# (b) Aegis ENABLED + acquire_call_lease returns None → AegisLeaseError
# ---------------------------------------------------------------------------

class TestAegisEnabledLeaseNone:
    """When Aegis is enabled but acquire_call_lease returns None (unexpected
    for the enabled path), AegisLeaseError MUST be raised and messages.create
    MUST NOT be called."""

    def test_aegis_enabled_lease_none_raises_and_no_create(self):
        from backend.core.ouroboros.governance.self_immunization import (
            AegisLeaseError,
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_response = _make_mock_response()
        mock_client = _make_mock_client(mock_response)
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(return_value=None),  # None despite Aegis enabled
             ), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                 wraps=lambda h, t: dict(h or {}),
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            with pytest.raises(AegisLeaseError):
                _run(provider.mutate(_SEED, n=1))

        # messages.create MUST NOT have been called — no unleased call allowed
        assert not mock_client.messages.create.called, (
            "messages.create was called despite missing lease (Aegis enabled + "
            "acquire_call_lease → None). ZERO-LEAK invariant violated."
        )

    def test_aegis_enabled_lease_none_not_swallowed_to_empty_list(self):
        """AegisLeaseError must NOT be swallowed by the per-mutation except
        block into []. It must propagate to the caller."""
        from backend.core.ouroboros.governance.self_immunization import (
            AegisLeaseError,
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_client = _make_mock_client(_make_mock_response())
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(return_value=None),
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            # Must raise, not return []
            raised = False
            try:
                _run(provider.mutate(_SEED, n=1))
            except AegisLeaseError:
                raised = True

            assert raised, (
                "AegisLeaseError was swallowed into [] — the ZERO-LEAK "
                "invariant requires it to propagate."
            )


# ---------------------------------------------------------------------------
# (c) Aegis ENABLED + acquire_call_lease raises → AegisLeaseError, no create
# ---------------------------------------------------------------------------

class TestAegisEnabledLeaseAcquireRaises:
    """When Aegis is enabled and acquire_call_lease itself raises (daemon
    unreachable, cap exceeded), AegisLeaseError MUST be raised and
    messages.create MUST NOT be called."""

    def test_acquire_raises_propagates_as_aegis_lease_error(self):
        from backend.core.ouroboros.governance.self_immunization import (
            AegisLeaseError,
            LLMMutationProvider,
            MutationBudgetGuard,
        )
        from backend.core.ouroboros.aegis.client import AegisClientError

        mock_client = _make_mock_client(_make_mock_response())
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(
                     side_effect=AegisClientError("daemon unreachable")
                 ),
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            with pytest.raises(AegisLeaseError):
                _run(provider.mutate(_SEED, n=1))

        assert not mock_client.messages.create.called, (
            "messages.create was called after acquire_call_lease raised — "
            "ZERO-LEAK invariant violated."
        )

    def test_acquire_generic_exception_propagates_as_aegis_lease_error(self):
        """Any exception from acquire_call_lease when Aegis is enabled must
        surface as AegisLeaseError (clean abort, no silent 401)."""
        from backend.core.ouroboros.governance.self_immunization import (
            AegisLeaseError,
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_client = _make_mock_client(_make_mock_response())
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(side_effect=OSError("network unavailable")),
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            with pytest.raises(AegisLeaseError):
                _run(provider.mutate(_SEED, n=1))

        assert not mock_client.messages.create.called


# ---------------------------------------------------------------------------
# (d) Aegis DISABLED → lease None → messages.create called WITHOUT lease header
# ---------------------------------------------------------------------------

class TestAegisDisabledDirectPath:
    """When Aegis is disabled, the direct path must work unchanged:
    acquire_call_lease returns None, merge_lease_header yields no extra header,
    and messages.create is called without an X-JARVIS-Lease header."""

    def test_disabled_direct_path_no_lease_header(self):
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )
        from backend.core.ouroboros.governance.aegis_provider_bridge import (
            LEASE_HEADER_NAME,
        )

        mock_response = _make_mock_response()
        mock_client = _make_mock_client(mock_response)
        guard = MutationBudgetGuard(budget_usd=1.0)

        # is_enabled() returns False — Aegis disabled
        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=False):
            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)
            results = _run(provider.mutate(_SEED, n=1))

        # messages.create MUST still be called (direct path intact)
        assert mock_client.messages.create.called, (
            "messages.create was NOT called on the direct (Aegis-disabled) path"
        )

        # Lease header must NOT appear in extra_headers
        call_kwargs = mock_client.messages.create.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        extra_headers = kwargs.get("extra_headers", {})
        assert LEASE_HEADER_NAME not in extra_headers, (
            f"Unexpected lease header on direct path: {extra_headers}"
        )

        # Results should be returned normally
        assert isinstance(results, list)

    def test_disabled_does_not_call_acquire_lease(self):
        """On the disabled path, acquire_call_lease should not be called at
        all (or if it is, it returns None — no AegisLeaseError raised)."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_client = _make_mock_client(_make_mock_response())
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=False):
            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)
            # Must not raise — disabled path is always safe
            results = _run(provider.mutate(_SEED, n=1))

        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# (e) Aegis ENABLED + lease ok + MODEL call raises → swallowed → []
# ---------------------------------------------------------------------------

class TestAegisEnabledModelError:
    """When Aegis is enabled and a lease is acquired correctly, but the
    actual model call (messages.create) raises, the error should be swallowed
    by the per-mutation except block and [] returned.

    Lease failures are fatal (abort). Genuine model/generation errors are not.
    """

    def test_model_error_swallowed_returns_empty_list(self):
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        # Model call raises — simulate network error / auth error from model
        mock_client.messages.create = AsyncMock(
            side_effect=RuntimeError("model unavailable")
        )
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(return_value=_LEASE_TOKEN),
             ), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                 return_value={"X-JARVIS-Lease": _LEASE_TOKEN},
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

            # Must not raise — model errors are swallowed to []
            results = _run(provider.mutate(_SEED, n=1))

        assert results == [], (
            f"Expected [] on model error, got {results!r}"
        )

    def test_model_timeout_swallowed_returns_empty_list(self):
        """asyncio.TimeoutError from messages.create → swallowed → []."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        mock_client = MagicMock()
        mock_client.messages = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        guard = MutationBudgetGuard(budget_usd=1.0)

        with patch(_AEGIS_CLIENT_IS_ENABLED_PATH, return_value=True), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.acquire_call_lease",
                 new=AsyncMock(return_value=_LEASE_TOKEN),
             ), \
             patch(
                 f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                 return_value={"X-JARVIS-Lease": _LEASE_TOKEN},
             ):

            provider = LLMMutationProvider(client=mock_client, budget_guard=guard)
            results = _run(provider.mutate(_SEED, n=1))

        assert results == []


# ---------------------------------------------------------------------------
# AegisLeaseError importable from self_immunization
# ---------------------------------------------------------------------------

class TestAegisLeaseErrorExport:
    """AegisLeaseError must be importable from self_immunization (defined or
    re-exported there so callers don't need two imports)."""

    def test_aegis_lease_error_importable(self):
        from backend.core.ouroboros.governance.self_immunization import (  # noqa: F401
            AegisLeaseError,
        )
        assert issubclass(AegisLeaseError, RuntimeError), (
            "AegisLeaseError must be a RuntimeError subclass"
        )

    def test_aegis_lease_error_message_is_clear(self):
        from backend.core.ouroboros.governance.self_immunization import (
            AegisLeaseError,
        )
        err = AegisLeaseError("test message")
        assert "test message" in str(err)
