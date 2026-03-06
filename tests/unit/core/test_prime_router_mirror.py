"""Tests for PrimeRouter mirror mode.

Disease 10 Wiring, Task 6.
"""
from __future__ import annotations

import pytest

from backend.core.prime_router import PrimeRouter, MirrorModeError


class TestMirrorMode:

    def test_mirror_mode_default_off(self):
        router = PrimeRouter()
        assert router.mirror_mode is False

    def test_set_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        assert router.mirror_mode is True

    async def test_promote_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            await router.promote_gcp_endpoint("10.0.0.1", 8000)

    async def test_demote_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            await router.demote_gcp_endpoint()

    def test_decide_route_blocked_in_mirror_mode(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        with pytest.raises(MirrorModeError):
            router._decide_route()

    def test_mirror_mode_can_be_disabled(self):
        router = PrimeRouter()
        router.set_mirror_mode(True)
        router.set_mirror_mode(False)
        # Should not raise
        router._decide_route()

    def test_mirror_decisions_counter(self):
        router = PrimeRouter()
        assert router.mirror_decisions_issued == 0
