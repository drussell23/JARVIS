"""UMF Golden Contract Tests -- IDENTICAL across all 3 repos.

CI must verify file hash parity to detect drift.
DO NOT modify without updating copies in jarvis-prime and reactor-core.
"""
from __future__ import annotations

import pytest

from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    RejectReason,
    Stream,
    UMF_SCHEMA_VERSION,
    UmfMessage,
)


def _make_msg(**overrides):
    defaults = dict(
        stream=Stream.command,
        kind=Kind.command,
        source=MessageSource(repo="jarvis", component="a",
                             instance_id="i", session_id="s"),
        target=MessageTarget(repo="jarvis-prime", component="b"),
        payload={},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestUmfEnvelopeShape:

    def test_required_top_level_keys(self):
        msg = _make_msg()
        d = msg.to_dict()
        required = {
            "schema_version", "message_id", "idempotency_key",
            "stream", "kind", "source", "target", "payload",
            "observed_at_unix_ms",
        }
        assert required.issubset(set(d.keys()))

    def test_source_has_required_fields(self):
        msg = _make_msg()
        src = msg.to_dict()["source"]
        assert {"repo", "component", "instance_id", "session_id"} == set(src.keys())

    def test_schema_version_is_umf_v1(self):
        assert UMF_SCHEMA_VERSION == "umf.v1"


class TestUmfStreamAndKindContract:

    def test_stream_enum_values(self):
        expected = {"lifecycle", "command", "event", "heartbeat", "telemetry"}
        assert {s.value for s in Stream} == expected

    def test_kind_enum_values(self):
        expected = {"command", "event", "heartbeat", "ack", "nack"}
        assert {k.value for k in Kind} == expected


class TestUmfReasonCodeContract:

    def test_all_reason_codes_present(self):
        required = {
            "schema_mismatch", "sig_invalid", "capability_mismatch",
            "ttl_expired", "deadline_expired", "dedup_duplicate",
            "route_unavailable", "backpressure_drop", "circuit_open",
            "handler_timeout",
        }
        actual = {r.value for r in RejectReason}
        assert required == actual

    def test_reason_codes_are_lowercase_snake(self):
        for r in RejectReason:
            assert r.value == r.value.lower()
            assert " " not in r.value


class TestUmfSerializationContract:

    def test_roundtrip_preserves_all_fields(self):
        msg = _make_msg(
            stream=Stream.lifecycle,
            kind=Kind.heartbeat,
            payload={"state": "ready", "liveness": True},
        )
        d = msg.to_dict()
        restored = UmfMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.payload == {"state": "ready", "liveness": True}
