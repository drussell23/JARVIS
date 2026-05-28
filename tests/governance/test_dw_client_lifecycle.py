from __future__ import annotations


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


def _make_provider_with_session(fake_session):
    """Bypass __init__ and wire up _state so the _session property works."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    from backend.core.ouroboros.governance._governance_state import (
        DoubleWordProviderState,
    )
    prov = DoublewordProvider.__new__(DoublewordProvider)
    state = DoubleWordProviderState()
    state.session = fake_session
    prov._state = state
    return prov


async def test_force_session_reset_closes_and_nulls():
    fake = _FakeSession()
    prov = _make_provider_with_session(fake)

    await prov.force_session_reset()

    assert fake.close_calls == 1
    assert prov._session is None  # next _get_session() rebuilds fresh connector


async def test_force_session_reset_idempotent_when_none():
    prov = _make_provider_with_session(None)
    await prov.force_session_reset()  # must NOT raise
    assert prov._session is None
