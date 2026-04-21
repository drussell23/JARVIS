"""Unit tests for jarvis_api.py.

Pure-Python, no Sublime dependency. Run via:

    python3 -m unittest extensions.sublime-jarvis.tests.test_jarvis_api

Or from the sublime-jarvis dir:

    python3 -m unittest tests.test_jarvis_api
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from typing import Any, Dict, List, Optional

# Allow running from either the repo root or the sublime-jarvis dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import jarvis_api  # noqa: E402
from jarvis_api import (  # noqa: E402
    ObservabilityClient,
    ObservabilityError,
    SchemaMismatchError,
    StreamConsumer,
    SCHEMA_VERSION,
    STATE_CLOSED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_ERROR,
    is_control_event,
    is_task_event,
    parse_sse_frame,
    _parse_endpoint,
    _validate_op_id,
)


# --- Fake HTTP connection factories ---------------------------------------


class _FakeResponse:
    def __init__(
        self, body: bytes, status: int = 200,
        chunks: Optional[List[bytes]] = None,
    ):
        self._body = body
        self.status = status
        self._chunks = chunks if chunks is not None else [body]
        self._chunk_idx = 0

    def read(self, n: Optional[int] = None) -> bytes:
        if n is None:
            return self._body
        if self._chunk_idx >= len(self._chunks):
            return b""
        out = self._chunks[self._chunk_idx]
        self._chunk_idx += 1
        return out


class _FakeConn:
    def __init__(
        self, response: _FakeResponse,
        expected_path: Optional[str] = None,
        expected_headers: Optional[Dict[str, str]] = None,
    ):
        self._response = response
        self.requests: List[Dict[str, Any]] = []
        self.closed = False
        self.expected_path = expected_path
        self.expected_headers = expected_headers

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.requests.append({
            "method": method, "path": path, "headers": dict(headers or {}),
        })

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        self.closed = True


def _inject_fake_conn(monkeypatch_target, fake_conn: _FakeConn) -> None:
    """Swap ``jarvis_api._new_connection`` to return our fake."""
    jarvis_api._new_connection = lambda endpoint, timeout=10.0: (
        fake_conn, "127.0.0.1", 8765,
    )


# ---------------------------------------------------------------------------
# Endpoint parsing + op_id validation
# ---------------------------------------------------------------------------


class EndpointParseTests(unittest.TestCase):

    def test_http_localhost_ok(self):
        host, port, is_https = _parse_endpoint("http://127.0.0.1:8765")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)
        self.assertFalse(is_https)

    def test_https_default_port(self):
        host, port, is_https = _parse_endpoint("https://localhost")
        self.assertEqual(port, 443)
        self.assertTrue(is_https)

    def test_rejects_non_loopback(self):
        with self.assertRaises(ObservabilityError):
            _parse_endpoint("http://203.0.113.7:8765")

    def test_rejects_bogus_scheme(self):
        with self.assertRaises(ObservabilityError):
            _parse_endpoint("ftp://127.0.0.1:8765")


class OpIdValidationTests(unittest.TestCase):

    def test_accepts_alnum_hyphen_underscore(self):
        for good in ["op-abc", "op_123", "OP-X-1", "a", "A" * 128]:
            _validate_op_id(good)  # no exception

    def test_rejects_space(self):
        with self.assertRaises(ObservabilityError):
            _validate_op_id("bad space")

    def test_rejects_empty(self):
        with self.assertRaises(ObservabilityError):
            _validate_op_id("")

    def test_rejects_over_128(self):
        with self.assertRaises(ObservabilityError):
            _validate_op_id("a" * 129)


# ---------------------------------------------------------------------------
# ObservabilityClient — GET paths
# ---------------------------------------------------------------------------


class ClientGetTests(unittest.TestCase):

    def setUp(self):
        self._orig_new_connection = jarvis_api._new_connection

    def tearDown(self):
        # Restore the original factory — do NOT reload, or the
        # ObservabilityError class identity changes and
        # assertRaises stops recognizing it.
        jarvis_api._new_connection = self._orig_new_connection

    def test_health_returns_body_on_200(self):
        body = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "enabled": True,
            "api_version": "1.0",
            "surface": "tasks",
            "now_mono": 0.0,
        }).encode("utf-8")
        fake = _FakeConn(_FakeResponse(body, 200))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        h = c.health()
        self.assertEqual(h["enabled"], True)
        self.assertEqual(h["surface"], "tasks")
        # One request made, GET, correct path, Accept header.
        self.assertEqual(len(fake.requests), 1)
        req = fake.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/observability/health")
        self.assertEqual(req["headers"].get("Accept"), "application/json")
        self.assertTrue(fake.closed)

    def test_task_list_returns_op_ids(self):
        body = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "op_ids": ["op-a", "op-b"],
            "count": 2,
        }).encode("utf-8")
        fake = _FakeConn(_FakeResponse(body, 200))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        out = c.task_list()
        self.assertEqual(list(out["op_ids"]), ["op-a", "op-b"])

    def test_task_detail_rejects_malformed_op_id_before_network(self):
        fake = _FakeConn(_FakeResponse(b"", 200))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        with self.assertRaises(ObservabilityError) as ctx:
            c.task_detail("bad space")
        self.assertEqual(ctx.exception.status, 400)
        # No network call should have been made.
        self.assertEqual(len(fake.requests), 0)

    def test_403_raises_observability_error(self):
        body = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "error": True,
            "reason_code": "ide_observability.disabled",
        }).encode("utf-8")
        fake = _FakeConn(_FakeResponse(body, 403))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        with self.assertRaises(ObservabilityError) as ctx:
            c.health()
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(
            ctx.exception.reason_code, "ide_observability.disabled",
        )

    def test_schema_mismatch_raises(self):
        body = json.dumps({
            "schema_version": "9.9",
            "op_ids": [], "count": 0,
        }).encode("utf-8")
        fake = _FakeConn(_FakeResponse(body, 200))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        with self.assertRaises(SchemaMismatchError) as ctx:
            c.task_list()
        self.assertEqual(ctx.exception.received, "9.9")

    def test_invalid_json_raises(self):
        fake = _FakeConn(_FakeResponse(b"not json", 200))
        _inject_fake_conn(None, fake)
        c = ObservabilityClient("http://127.0.0.1:8765")
        with self.assertRaises(ObservabilityError) as ctx:
            c.health()
        self.assertEqual(ctx.exception.reason_code, "client.invalid_json")


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


class SSEParserTests(unittest.TestCase):

    def _frame(self, event_id: str, event_type: str, op_id: str = "op-x") -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "event_type": event_type,
            "op_id": op_id,
            "timestamp": "t",
            "payload": {},
        }
        return (
            "id: " + event_id + "\n"
            "event: " + event_type + "\n"
            "data: " + json.dumps(payload)
        )

    def test_parse_well_formed_frame(self):
        raw = self._frame("e1", "task_created")
        out = parse_sse_frame(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["event_id"], "e1")
        self.assertEqual(out["event_type"], "task_created")

    def test_parse_returns_none_on_missing_data(self):
        out = parse_sse_frame("id: e1\nevent: task_created\n")
        self.assertIsNone(out)

    def test_parse_ignores_comment_lines(self):
        raw = ": keepalive\n" + self._frame("e1", "task_created")
        out = parse_sse_frame(raw)
        self.assertIsNotNone(out)

    def test_parse_rejects_schema_mismatch(self):
        payload = {
            "schema_version": "9.9",
            "event_id": "e1", "event_type": "task_created",
            "op_id": "op-x", "timestamp": "t", "payload": {},
        }
        raw = (
            "id: e1\nevent: task_created\n"
            "data: " + json.dumps(payload)
        )
        out = parse_sse_frame(raw)
        self.assertIsNone(out)

    def test_parse_rejects_unknown_event_type(self):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_id": "e1", "event_type": "bogus_type",
            "op_id": "op-x", "timestamp": "t", "payload": {},
        }
        raw = (
            "id: e1\nevent: bogus_type\n"
            "data: " + json.dumps(payload)
        )
        out = parse_sse_frame(raw)
        self.assertIsNone(out)

    def test_is_task_event_vs_control_event(self):
        for t in [
            "task_created", "task_started", "task_updated",
            "task_completed", "task_cancelled", "board_closed",
        ]:
            self.assertTrue(is_task_event({"event_type": t}))
            self.assertFalse(is_control_event({"event_type": t}))
        for t in ["heartbeat", "stream_lag", "replay_start", "replay_end"]:
            self.assertFalse(is_task_event({"event_type": t}))
            self.assertTrue(is_control_event({"event_type": t}))


# ---------------------------------------------------------------------------
# StreamConsumer lifecycle
# ---------------------------------------------------------------------------


class _FakeStreamConn:
    """Fake http-conn whose getresponse() returns a response whose
    .read() yields SSE chunks and then EOF."""

    def __init__(self, chunks: List[bytes], status: int = 200) -> None:
        self._resp = _FakeResponse(b"".join(chunks), status, chunks=chunks + [b""])
        self.requests: List[Dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, path: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.requests.append({
            "method": method, "path": path, "headers": dict(headers or {}),
        })

    def getresponse(self) -> _FakeResponse:
        return self._resp

    def close(self) -> None:
        self.closed = True


def _sse_frame_bytes(event_id: str, event_type: str, op_id: str = "op-x") -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "op_id": op_id,
        "timestamp": "t",
        "payload": {},
    }
    txt = (
        "id: " + event_id + "\n"
        "event: " + event_type + "\n"
        "data: " + json.dumps(payload) + "\n\n"
    )
    return txt.encode("utf-8")


class StreamConsumerTests(unittest.TestCase):

    def test_stream_consumer_receives_events(self):
        frames = [
            _sse_frame_bytes("e1", "task_created"),
            _sse_frame_bytes("e2", "task_started"),
        ]
        fake = _FakeStreamConn(frames)
        consumer = StreamConsumer(
            endpoint="http://127.0.0.1:8765",
            auto_reconnect=False,
            connect_fn=lambda: fake,
            sleep_fn=lambda _s: None,
        )
        received: List[Dict[str, Any]] = []
        consumer.on_event(lambda ev: received.append(ev))
        consumer.start()
        # Wait for the loop to finish (auto_reconnect=False → single pass).
        deadline = time.monotonic() + 2.0
        while consumer.get_state() not in (STATE_DISCONNECTED, STATE_CLOSED, STATE_ERROR) and time.monotonic() < deadline:
            time.sleep(0.01)
        consumer.stop(join_timeout=1.0)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0]["event_id"], "e1")
        self.assertEqual(received[1]["event_id"], "e2")

    def test_stream_consumer_transitions_through_connected(self):
        frames = [_sse_frame_bytes("e1", "task_created")]
        fake = _FakeStreamConn(frames)
        states: List[str] = []
        consumer = StreamConsumer(
            endpoint="http://127.0.0.1:8765",
            auto_reconnect=False,
            connect_fn=lambda: fake,
            sleep_fn=lambda _s: None,
            logger=lambda _m: None,
        )
        consumer.on_state(lambda s: states.append(s))
        consumer.start()
        deadline = time.monotonic() + 2.0
        while STATE_CONNECTED not in states and time.monotonic() < deadline:
            time.sleep(0.01)
        consumer.stop(join_timeout=1.0)
        self.assertIn(STATE_CONNECTED, states)

    def test_stream_consumer_error_on_403(self):
        fake = _FakeStreamConn([b""], status=403)
        consumer = StreamConsumer(
            endpoint="http://127.0.0.1:8765",
            auto_reconnect=False,
            connect_fn=lambda: fake,
            sleep_fn=lambda _s: None,
        )
        states: List[str] = []
        consumer.on_state(lambda s: states.append(s))
        consumer.start()
        deadline = time.monotonic() + 2.0
        while STATE_ERROR not in states and time.monotonic() < deadline:
            time.sleep(0.01)
        consumer.stop(join_timeout=1.0)
        self.assertIn(STATE_ERROR, states)

    def test_stop_is_idempotent(self):
        frames = [_sse_frame_bytes("e1", "task_created")]
        fake = _FakeStreamConn(frames)
        consumer = StreamConsumer(
            endpoint="http://127.0.0.1:8765",
            auto_reconnect=False,
            connect_fn=lambda: fake,
            sleep_fn=lambda _s: None,
        )
        consumer.start()
        consumer.stop(join_timeout=1.0)
        consumer.stop(join_timeout=1.0)  # must not raise


# ---------------------------------------------------------------------------
# Observability plugin rendering (pure text)
# ---------------------------------------------------------------------------


class RenderTests(unittest.TestCase):

    def setUp(self):
        # Plugin module imports sublime conditionally; on bare CPython
        # it falls back to the non-sublime path. Import here so tests
        # exercise the pure rendering path.
        # (stdin redirect prevents Sublime import warnings.)
        import importlib
        # Remove cached sublime stubs if any.
        for mod in ["jarvis_observability"]:
            sys.modules.pop(mod, None)
        self.plugin_mod = importlib.import_module("jarvis_observability")

    def test_render_op_detail_shows_open_and_tasks(self):
        text = self.plugin_mod.render_op_detail_text({
            "op_id": "op-abc",
            "closed": False,
            "active_task_id": "task-op-abc-0001",
            "board_size": 1,
            "tasks": [{
                "task_id": "task-op-abc-0001",
                "state": "in_progress",
                "title": "the thing",
                "body": "",
                "sequence": 1,
                "cancel_reason": "",
            }],
        })
        self.assertIn("op-abc", text)
        self.assertIn("LIVE", text)
        self.assertIn("in_progress", text)
        self.assertIn("the thing", text)

    def test_render_closed_shows_closed_tag(self):
        text = self.plugin_mod.render_op_detail_text({
            "op_id": "op-closed",
            "closed": True,
            "active_task_id": None,
            "board_size": 0,
            "tasks": [],
        })
        self.assertIn("CLOSED", text)

    def test_render_cancel_reason(self):
        text = self.plugin_mod.render_op_detail_text({
            "op_id": "op-c",
            "closed": False,
            "active_task_id": None,
            "board_size": 1,
            "tasks": [{
                "task_id": "task-c-1",
                "state": "cancelled",
                "title": "gone",
                "body": "",
                "sequence": 1,
                "cancel_reason": "user abort",
            }],
        })
        self.assertIn("reason: user abort", text)


if __name__ == "__main__":
    unittest.main()
