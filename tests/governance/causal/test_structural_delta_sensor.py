"""Domain-1 Staging-1 Task 1 -- StructuralDeltaSensor (Body publisher).

Proves the sensor computes a Staging-0 structural delta and publishes it as a
content-free ``causal.delta.<repo>`` TrinityEvent over the injected bus, that a
no-op change publishes nothing, that an over-bound change still publishes (as
``file_level_churn``), that construction refuses a non-trinity repo, and that a
causal-delta event passes the Body's ``_journal_local_origin_only`` filter (so
the Stage-3 DurableOutbound WAL journals it -- durability is INHERITED, not
re-coded). Also pins the Body bus-bridge allowlist to include ``causal.delta.*``.
"""
from __future__ import annotations

import inspect
import json

import pytest

from backend.core.trinity_event_bus import RepoType, TrinityEvent
from backend.core.ouroboros.governance.causal.structural_delta_sensor import (
    CAUSAL_DELTA_TOPIC_PREFIX,
    GitLineage,
    StructuralDeltaSensor,
)


# ---------------------------------------------------------------------------
# Fakes -- zero real bus, zero real durable emit-seq.
# ---------------------------------------------------------------------------

class _FakeBus:
    """Records publish()/publish_raw() calls without touching a real broker."""

    def __init__(self) -> None:
        self.published = []          # list[TrinityEvent] via publish()
        self.published_raw = []      # list[dict] via publish_raw()
        self.local_repo = RepoType.JARVIS

    async def publish(self, event, persist=True):
        self.published.append(event)
        return event.event_id

    async def publish_raw(self, topic, data, priority=None, target=None,
                          persist=True, correlation_id=None, causation_id=None):
        self.published_raw.append(
            {
                "topic": topic,
                "data": data,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
            }
        )
        return "raw-event-id"


class _FakeEmitSeq:
    def __init__(self, value: int = 7) -> None:
        self.value = value
        self.calls = []

    def next(self, repo: str) -> int:
        self.calls.append(repo)
        return self.value


_LINEAGE = GitLineage(head_sha="head123", parent_sha="parent456", merge_base="mb789")

_BEFORE = "def foo():\n    return 1\n"
# after adds a brand-new function whose BODY carries a distinctive token that
# must never leak into the published (content-free) envelope.
_AFTER = (
    "def foo():\n    return 1\n\n\n"
    "def bar():\n    MAGIC_TOKEN_XYZ = 42\n    return MAGIC_TOKEN_XYZ\n"
)


def _all_published_events(bus: _FakeBus):
    return list(bus.published) + list(bus.published_raw)


# ---------------------------------------------------------------------------
# (a) real before/after -> one content-free publish with correct provenance.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_real_change_publishes_content_free_causal_delta():
    bus = _FakeBus()
    seq = _FakeEmitSeq(value=7)
    sensor = StructuralDeltaSensor(bus, repo="jarvis", emit_seq=seq)

    event_id = await sensor.emit_file_change(
        "backend/x.py", _BEFORE, _AFTER, lineage=_LINEAGE
    )

    assert event_id is not None
    assert len(_all_published_events(bus)) == 1
    assert len(bus.published) == 1, "expected publish() with an explicit source"
    event: TrinityEvent = bus.published[0]

    assert event.topic == CAUSAL_DELTA_TOPIC_PREFIX + "jarvis" == "causal.delta.jarvis"
    assert event.source == RepoType.JARVIS
    assert event.correlation_id == "head123"
    assert event.causation_id == "parent456"

    # envelope == stamp_delta output shape: {"delta": ..., "lineage": ...}
    payload = event.payload
    assert set(payload.keys()) == {"delta", "lineage"}
    assert payload["lineage"]["repo"] == "jarvis"
    assert payload["lineage"]["head_sha"] == "head123"
    assert payload["lineage"]["parent_sha"] == "parent456"
    assert payload["lineage"]["merge_base"] == "mb789"
    assert payload["lineage"]["emit_seq"] == 7
    assert seq.calls == ["jarvis"]

    # bar() was added structurally...
    added_ids = [s["symbol_id"] for s in payload["delta"]["symbols_added"]]
    assert any(sid.endswith(":bar") for sid in added_ids)

    # ...but NO source content ever crosses the boundary (Mandate 1).
    assert "MAGIC_TOKEN_XYZ" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# (b) empty structural change -> nothing published, returns None.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_structural_change_publishes_nothing():
    bus = _FakeBus()
    sensor = StructuralDeltaSensor(bus, repo="jarvis", emit_seq=_FakeEmitSeq())

    event_id = await sensor.emit_file_change(
        "backend/x.py", _BEFORE, _BEFORE, lineage=_LINEAGE
    )

    assert event_id is None
    assert _all_published_events(bus) == []


# ---------------------------------------------------------------------------
# (c) over-bound / unparseable revision -> file_level_churn -> still publishes.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_level_churn_publishes():
    bus = _FakeBus()
    sensor = StructuralDeltaSensor(bus, repo="jarvis", emit_seq=_FakeEmitSeq())

    # before is unparseable -> parse_failure -> file_level_churn True even though
    # the per-symbol tuples collapse to empty.
    event_id = await sensor.emit_file_change(
        "backend/x.py", "this is not python (((", _AFTER, lineage=_LINEAGE
    )

    assert event_id is not None
    assert len(bus.published) == 1
    delta = bus.published[0].payload["delta"]
    assert delta["file_level_churn"] is True
    assert delta["symbols_added"] == []


# ---------------------------------------------------------------------------
# (d) unknown repo -> construction refuses (reflective RepoType validation).
# ---------------------------------------------------------------------------

def test_unknown_repo_refuses_construction():
    bus = _FakeBus()
    with pytest.raises(ValueError):
        StructuralDeltaSensor(bus, repo="not-a-trinity-repo")


def test_broadcast_repo_refuses_construction():
    # BROADCAST is a TARGET semantic, not a valid SOURCE identity -- a causal
    # delta must originate from ONE concrete repo.
    bus = _FakeBus()
    with pytest.raises(ValueError):
        StructuralDeltaSensor(bus, repo="broadcast")


# ---------------------------------------------------------------------------
# (e) durability inheritance: a causal-delta trinity event passes the Body's
#     _journal_local_origin_only filter, so DurableOutbound WILL journal it.
# ---------------------------------------------------------------------------

def test_causal_delta_event_passes_durable_journal_filter():
    from scripts.run_body_mode import _journal_local_origin_only

    # A locally-originated causal-delta event (no peer ``origin`` stamped yet --
    # the bridge stamps origin on the OUTBOUND payload; at publish time it is
    # absent -> fail-OPEN -> journaled).
    event = TrinityEvent(
        topic=CAUSAL_DELTA_TOPIC_PREFIX + "jarvis",
        source=RepoType.JARVIS,
        payload={"delta": {"file_level_churn": False}, "lineage": {"repo": "jarvis"}},
    )
    assert _journal_local_origin_only(event) is True


# ---------------------------------------------------------------------------
# Body bus-bridge allowlist pin: causal.delta.* is bridged onto the wire.
# ---------------------------------------------------------------------------

def test_body_bridge_allowlist_includes_causal_delta():
    from scripts.run_body_mode import BodyModeDriver

    src = inspect.getsource(BodyModeDriver._do_bridge)
    assert "causal.delta.*" in src


# ---------------------------------------------------------------------------
# Fail-soft: an exploding bus never raises out of emit_file_change.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_is_fail_soft_on_bus_error():
    class _BoomBus(_FakeBus):
        async def publish(self, event, persist=True):
            raise RuntimeError("bus down")

    sensor = StructuralDeltaSensor(_BoomBus(), repo="jarvis", emit_seq=_FakeEmitSeq())
    result = await sensor.emit_file_change(
        "backend/x.py", _BEFORE, _AFTER, lineage=_LINEAGE
    )
    assert result is None
