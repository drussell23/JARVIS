"""Tests for CancellationToken — epoch-scoped cooperative cancellation."""
import asyncio
import pytest

from backend.core.ouroboros.cancellation_token import CancellationToken


class TestCancellationToken:
    def test_token_starts_uncancelled(self):
        token = CancellationToken(epoch_id=1)
        assert token.is_cancelled is False

    def test_cancel_sets_flag(self):
        token = CancellationToken(epoch_id=2)
        token.cancel()
        assert token.is_cancelled is True

    def test_cancel_is_idempotent(self):
        token = CancellationToken(epoch_id=3)
        token.cancel()
        token.cancel()  # Should not raise or change behaviour
        assert token.is_cancelled is True

    @pytest.mark.asyncio
    async def test_wait_for_cancellation(self):
        token = CancellationToken(epoch_id=4)

        async def cancel_soon():
            await asyncio.sleep(0.05)
            token.cancel()

        asyncio.get_event_loop().call_later(0.05, token.cancel)
        # wait() should return after the cancel fires; 1s timeout is ample
        await asyncio.wait_for(token.wait(), timeout=1.0)
        assert token.is_cancelled is True

    def test_epoch_id_is_readonly(self):
        token = CancellationToken(epoch_id=5)
        assert token.epoch_id == 5
        with pytest.raises(AttributeError):
            token.epoch_id = 99  # type: ignore[misc]
