"""Unit tests never reach a live inference tier.

Root cause of every failed L3 unit on 2026-09-07: the RT gate makes the local
model the PRIMARY tier when ``JARVIS_LOCAL_PRIME_ENABLED`` is true, the daemon
loads that flag from ``.env``, and every pytest it spawns for VALIDATE
inherits it — so a unit test that injected a fake provider was answered by
the 30B model on the busy GPU (or timed out behind the soak's own
generation). The autouse pin in ``tests/conftest.py`` is the contract; this
test is its tripwire.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.comms.karen_synth.speech_provider import DWSpeechProvider
from backend.core.ouroboros.governance.rt_gate import local_tier_enabled


def test_local_tier_is_pinned_off_for_the_suite() -> None:
    assert os.environ.get("JARVIS_LOCAL_PRIME_ENABLED") == "false"
    assert local_tier_enabled() is False


def test_a_test_can_still_opt_into_the_local_lane(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    assert local_tier_enabled() is True


class _Res:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeDW:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_sync(self, prompt, *, system_prompt, caller_id, max_tokens=512, **kw):
        self.calls += 1
        return _Res("from the injected provider")


@pytest.mark.asyncio
async def test_injected_provider_is_the_tier_that_answers(monkeypatch) -> None:
    """The gate's cloud tiers are unreachable in a unit test (no key), the
    local tier is pinned off — so the INJECTED provider answers. This is the
    exact shape that was red under the daemon's environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    dw = _FakeDW()
    out = [c async for c in DWSpeechProvider(dw, max_tokens=16).source(system_prompt="s", user_prompt="u")]
    assert out == ["from the injected provider"]
    assert dw.calls == 1
