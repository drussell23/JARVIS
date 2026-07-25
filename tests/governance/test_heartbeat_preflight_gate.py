"""Pre-Flight Auth Gate + Auto-Defrost — the dual-auth freeze cascade.

The incident (session bt-2026-07-24-212505, live): DoubleWord was HEALTHY and
serving real generations, but the heartbeat's per-call lease acquisition hit a
transient ``TimeoutError``. The old code swallowed it, dispatched an
under-authenticated request anyway, the Aegis daemon answered 401, and the
Safety-Law classifier read that 401 as a *configuration* error and set
``_frozen = True`` — permanently. A frozen heartbeat never reports degraded and
never emits a HEALTHY edge, so the AWE recovery reflex was disabled for the life
of the process and the queued canary could never launch.

Two independent defects, pinned separately:

  (1) a transient lease failure must fail CLOSED before the socket — no HTTP
      request, no 401, no freeze;
  (2) a heartbeat that IS frozen must self-heal when it observes proof that the
      auth path works, rather than requiring a daemon restart.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import provider_heartbeat as ph
from backend.core.ouroboros.governance.provider_heartbeat import (
    AegisConfigurationError,
    ProbeAuthUnavailable,
)


# ---------------------------------------------------------------------------
# (1) Pre-Flight Auth Gate
# ---------------------------------------------------------------------------


def test_classifier_has_a_third_transient_state():
    """The original taxonomy was binary (auth|outage). A pre-dispatch credential
    failure is neither."""
    assert ph._classify_probe_exception(ProbeAuthUnavailable("x")) == "transient"

    import urllib.error
    assert ph._classify_probe_exception(
        urllib.error.HTTPError("u", 401, "m", None, None)) == "auth"
    assert ph._classify_probe_exception(
        urllib.error.HTTPError("u", 503, "m", None, None)) == "outage"
    # Unknown stays conservatively an outage — unchanged.
    assert ph._classify_probe_exception(TimeoutError()) == "outage"


async def test_lease_timeout_never_dispatches_http(monkeypatch):
    """THE CORE ASSERTION: a mocked TimeoutError during lease fetch must prevent
    the outbound request entirely. No HTTP means no 401 means no freeze."""
    from backend.core.ouroboros.governance import aegis_provider_bridge as apb

    monkeypatch.setattr(ph, "_aegis_lease_required", lambda: True)
    monkeypatch.setattr(apb, "dw_aegis_base_url", lambda: "http://127.0.0.1:1/v1")

    async def _ok_auth():
        return {"Authorization": "Bearer session-token"}

    async def _lease_times_out(**_kw):
        raise TimeoutError("lease acquire timed out")

    monkeypatch.setattr(apb, "dw_session_auth_header", _ok_auth)
    monkeypatch.setattr(apb, "acquire_call_lease", _lease_times_out)

    dispatched = []

    def _tripwire(*a, **k):
        dispatched.append(a)
        raise AssertionError("HTTP dispatched despite unavailable credentials")

    monkeypatch.setattr(ph, "_http_post_json", _tripwire)

    with pytest.raises(ProbeAuthUnavailable):
        await ph._resolve_dw_probe_transport()

    assert dispatched == [], "the gate must fail closed BEFORE the socket"


async def test_lease_timeout_does_not_freeze_the_heartbeat(monkeypatch):
    """The consequence that actually mattered: the heartbeat stays thawed, so
    AWE edge detection survives a transient lease hiccup."""
    hb = ph.DWHeartbeat()
    monkeypatch.setattr(ph, "heartbeat_enabled", lambda: True)

    async def _probe_raises_transient():
        raise ProbeAuthUnavailable("lease unavailable pre-dispatch")

    hb._probe_fn = _probe_raises_transient

    assert hb.is_frozen() is False
    await hb.beat()

    assert hb.is_frozen() is False, "a transient skip must NEVER freeze"
    assert hb._transient_skips == 1
    # And it recorded no degrade verdict either — no information, not bad news.
    assert hb._healthy_streak == 0


async def test_genuine_401_still_freezes(monkeypatch):
    """The Safety Law must survive intact: a REAL auth error (credentials
    assembled fine, daemon rejected them) still freezes. If this ever goes green
    by not-freezing, the pre-flight gate has swallowed the safety property."""
    hb = ph.DWHeartbeat()
    monkeypatch.setattr(ph, "heartbeat_enabled", lambda: True)

    async def _probe_raises_auth():
        raise AegisConfigurationError("real 401")

    hb._probe_fn = _probe_raises_auth
    hb._dlq_emit_fn = lambda payload: None

    await hb.beat()
    assert hb.is_frozen() is True, "a genuine config error must still freeze"


async def test_aegis_disabled_still_dispatches_without_lease(monkeypatch):
    """Direct-DW mode carries no lease at all. The gate must not brick it —
    lease=None is legitimate when Aegis is off."""
    from backend.core.ouroboros.governance import aegis_provider_bridge as apb

    monkeypatch.setattr(ph, "_aegis_lease_required", lambda: False)
    monkeypatch.setattr(apb, "dw_aegis_base_url", lambda: "http://127.0.0.1:1/v1")

    async def _auth():
        return {}

    async def _no_lease(**_kw):
        return None

    monkeypatch.setattr(apb, "dw_session_auth_header", _auth)
    monkeypatch.setattr(apb, "acquire_call_lease", _no_lease)

    url, headers = await ph._resolve_dw_probe_transport()
    assert url.endswith("/chat/completions")
    assert isinstance(headers, dict)


# ---------------------------------------------------------------------------
# (2) Auto-Defrost
# ---------------------------------------------------------------------------


def test_successful_generation_defrosts_a_frozen_heartbeat():
    """THE SECOND CORE ASSERTION: observing proof that auth works flips
    _frozen back to False without a daemon restart."""
    hb = ph.DWHeartbeat()
    hb._frozen = True

    assert hb.is_frozen() is True
    assert hb.note_provider_success() is True
    assert hb.is_frozen() is False, "auto-defrost did not thaw the heartbeat"


def test_defrost_is_a_noop_when_not_frozen():
    hb = ph.DWHeartbeat()
    assert hb.is_frozen() is False
    assert hb.note_provider_success() is False
    assert hb._defrost_count == 0


def test_defrost_is_bounded_against_flap(monkeypatch):
    """A persistently-broken probe auth would otherwise become a freeze/defrost
    flap machine, emitting a CRITICAL + DLQ record every cycle forever. After
    the cap, the freeze becomes durable so a human sees it."""
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_MAX_DEFROSTS", "2")
    hb = ph.DWHeartbeat()

    for expected in (True, True, False):
        hb._frozen = True
        assert hb.note_provider_success() is expected

    assert hb.is_frozen() is True, "past the cap the freeze must stick"
    assert hb._defrost_count == 2


async def test_defrost_rides_the_real_broker():
    """End-to-end over the REAL StreamEventBroker — proves the event type is
    registered and the wiring is live, not a listener for a name nobody emits."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PROVIDER_GENERATION_SUCCEEDED,
        get_default_broker,
        publish_task_event,
        reset_default_broker,
    )

    reset_default_broker()
    hb = ph.DWHeartbeat()
    hb._frozen = True

    task = asyncio.ensure_future(hb.watch_provider_success(max_events=1))
    try:
        await asyncio.wait_for(hb._success_subscribed.wait(), timeout=2.0)
        published = publish_task_event(
            EVENT_TYPE_PROVIDER_GENERATION_SUCCEEDED, "doubleword",
            {"provider": "doubleword", "candidates": 1},
        )
        assert published is not None, "event type is not registered on the broker"

        async def _thawed():
            while hb.is_frozen():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_thawed(), timeout=3.0)
        assert hb.is_frozen() is False
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        reset_default_broker()


def test_generation_success_event_is_registered():
    """Guard against the silent-drop trap: an unregistered event_type is
    rejected by publish() and the listener would never fire."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PROVIDER_GENERATION_SUCCEEDED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_PROVIDER_GENERATION_SUCCEEDED in _VALID_EVENT_TYPES
