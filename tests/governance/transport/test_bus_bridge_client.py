from __future__ import annotations

from backend.core.ouroboros.governance.ide_observability_stream import StreamEventBroker
from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.bus_bridge_client import BusBridgeClient


def _cfg(**over):
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.5, reconnect_max_s=8.0, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=False, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="mac-test",
    )
    base.update(over)
    return TransportConfig(**base)


def test_backoff_grows_geometrically_and_caps():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c._next_backoff(0) == 0.5
    assert c._next_backoff(1) == 1.0
    assert c._next_backoff(2) == 2.0
    assert c._next_backoff(20) == 8.0  # capped at reconnect_max_s


def test_backoff_jitter_stays_in_band():
    c = BusBridgeClient(StreamEventBroker(), _cfg(reconnect_jitter=0.3))
    for attempt in range(6):
        base = min(0.5 * 2 ** attempt, 8.0)
        val = c._next_backoff(attempt)
        assert base * 0.7 <= val <= base * 1.3


def test_contiguous_advance_leaves_gaps_for_replay():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c.last_event_id is None
    c._advance_contiguous(format(1, "012x"))
    assert c.last_event_id == format(1, "012x")
    c._advance_contiguous(format(2, "012x"))
    assert c.last_event_id == format(2, "012x")
    # A gap (skip 3, receive 4) must NOT advance the high-water past 2.
    c._advance_contiguous(format(4, "012x"))
    assert c.last_event_id == format(2, "012x")  # replay will refetch 3,4
    # Filling the gap advances again.
    c._advance_contiguous(format(3, "012x"))
    assert c.last_event_id == format(3, "012x")


def test_degraded_defaults_false():
    c = BusBridgeClient(StreamEventBroker(), _cfg())
    assert c.degraded is False
