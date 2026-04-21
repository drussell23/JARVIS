"""HTTP client + SSE consumer for the JARVIS observability surface.

Pure-Python implementation targeting Sublime Text 4's embedded
Python 3.8 interpreter — **no third-party dependencies**. Uses the
stdlib ``http.client`` for both GET and long-polling SSE, so the
same module works in Sublime, plain CPython, and any environment
where urllib-level HTTP is available.

## Authority posture

- **Read-only client.** Consumes the Slice 1 GET routes and the
  Slice 2 SSE stream. The module never issues a non-GET request.
- **Schema validation.** Every JSON payload must carry
  ``schema_version == "1.0"``; mismatches raise
  :class:`SchemaMismatchError` so the caller can surface an
  operator-visible warning instead of rendering broken shapes.
- **Bounded everything.** Network reads are chunked; the SSE parser
  tracks ``last_event_id`` for resume but never retains more than
  one parsed frame at a time.

## Why handroll the SSE parser?

Sublime Text's bundled Python does not include ``aiohttp`` or
``requests``. A ~50-line state machine over ``http.client`` is more
portable than taking on a networking dep (which we can't ship).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Callable, Dict, List, Optional, Tuple


SCHEMA_VERSION = "1.0"

# The same 10-type vocabulary locked by Slice 2.
TASK_EVENT_TYPES = frozenset({
    "task_created",
    "task_started",
    "task_updated",
    "task_completed",
    "task_cancelled",
    "board_closed",
})
CONTROL_EVENT_TYPES = frozenset({
    "heartbeat",
    "stream_lag",
    "replay_start",
    "replay_end",
})
ALL_EVENT_TYPES = TASK_EVENT_TYPES | CONTROL_EVENT_TYPES


# --- Exceptions ------------------------------------------------------------


class ObservabilityError(Exception):
    """Any HTTP-level failure talking to the JARVIS server."""

    def __init__(self, message: str, status: int = -1, reason_code: str = ""):
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code


class SchemaMismatchError(Exception):
    """Server returned a payload with an unexpected schema_version."""

    def __init__(self, received: str):
        super().__init__(
            "schema_version mismatch: expected %s, got %r"
            % (SCHEMA_VERSION, received)
        )
        self.received = received


# --- Endpoint parsing ------------------------------------------------------


def _parse_endpoint(endpoint: str) -> Tuple[str, int, bool]:
    """Split ``http://host:port`` into (host, port, is_https).

    Raises :class:`ObservabilityError` on an unparseable or non-
    loopback-looking endpoint. Loopback enforcement is defensive —
    the server-side ``assert_loopback_only`` already gates binds,
    but the plugin should refuse to even try to talk to a non-local
    JARVIS.
    """
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise ObservabilityError(
            "endpoint scheme must be http or https: %s" % endpoint,
            status=-1, reason_code="client.bad_scheme",
        )
    host = parsed.hostname or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ObservabilityError(
            "endpoint host must be loopback (127.0.0.1/::1/localhost): %s"
            % host,
            status=-1, reason_code="client.non_loopback",
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port, parsed.scheme == "https"


def _new_connection(
    endpoint: str, timeout: float = 10.0,
) -> Tuple[Any, str, int]:
    host, port, is_https = _parse_endpoint(endpoint)
    conn_cls = HTTPSConnection if is_https else HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    return conn, host, port


# --- GET client ------------------------------------------------------------


class ObservabilityClient:
    """Synchronous GET wrapper — callers that want async behavior
    use :class:`StreamConsumer` or a worker thread."""

    def __init__(self, endpoint: str, timeout: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def health(self) -> Dict[str, Any]:
        return self._get("/observability/health")

    def task_list(self) -> Dict[str, Any]:
        return self._get("/observability/tasks")

    def task_detail(self, op_id: str) -> Dict[str, Any]:
        _validate_op_id(op_id)
        return self._get(
            "/observability/tasks/" + urllib.parse.quote(op_id, safe=""),
        )

    def _get(self, path: str) -> Dict[str, Any]:
        conn, _, _ = _new_connection(self.endpoint, self.timeout)
        try:
            conn.request("GET", path, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
        except OSError as exc:
            raise ObservabilityError(
                "fetch failed: %s" % exc, status=-1,
                reason_code="client.network_error",
            )
        finally:
            conn.close()
        if resp.status != 200:
            reason = _extract_reason(body)
            raise ObservabilityError(
                "%s returned %d" % (path, resp.status),
                status=resp.status, reason_code=reason,
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ObservabilityError(
                "invalid JSON from %s" % path,
                status=resp.status, reason_code="client.invalid_json",
            )
        if not _is_supported_schema(payload):
            raise SchemaMismatchError(str(payload.get("schema_version", "")))
        return payload


def _validate_op_id(op_id: str) -> None:
    if not isinstance(op_id, str) or not op_id:
        raise ObservabilityError(
            "op_id must be a non-empty string",
            status=400, reason_code="client.malformed_op_id",
        )
    if len(op_id) > 128:
        raise ObservabilityError(
            "op_id too long", status=400, reason_code="client.malformed_op_id",
        )
    for ch in op_id:
        if not (ch.isalnum() or ch in ("_", "-")):
            raise ObservabilityError(
                "op_id has forbidden character %r" % ch,
                status=400, reason_code="client.malformed_op_id",
            )


def _is_supported_schema(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
    )


def _extract_reason(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(parsed, dict):
        rc = parsed.get("reason_code")
        if isinstance(rc, str):
            return rc
    return ""


# --- SSE parser + consumer -------------------------------------------------


StreamListener = Callable[[Dict[str, Any]], None]
StateListener = Callable[[str], None]


# Matches the VS Code extension's state machine for cross-client parity.
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_CLOSED = "closed"


_BASE_BACKOFF_S = 0.5


class StreamConsumer:
    """Threaded SSE consumer for ``/observability/stream``.

    One background thread per consumer. `start()` is non-blocking;
    `stop()` is blocking (joins the thread). Listeners are invoked
    on the consumer thread — callers that need to touch Sublime's
    main thread must dispatch via ``sublime.set_timeout``.
    """

    def __init__(
        self,
        endpoint: str,
        op_id_filter: Optional[str] = None,
        auto_reconnect: bool = True,
        reconnect_max_backoff_s: float = 30.0,
        timeout_s: float = 30.0,
        logger: Optional[Callable[[str], None]] = None,
        # Test injection: override the connection factory.
        connect_fn: Optional[Callable[[], Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.op_id_filter = op_id_filter
        self.auto_reconnect = auto_reconnect
        self.reconnect_max_backoff_s = reconnect_max_backoff_s
        self.timeout_s = timeout_s
        self._logger = logger or (lambda _m: None)
        self._connect_fn = connect_fn
        self._sleep_fn = sleep_fn or time.sleep
        self._listeners: List[StreamListener] = []
        self._state_listeners: List[StateListener] = []
        self._state = STATE_DISCONNECTED
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_event_id: Optional[str] = None
        self._consecutive_failures = 0

    # --- listener registration --------------------------------------------

    def on_event(self, listener: StreamListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsub

    def on_state(self, listener: StateListener) -> Callable[[], None]:
        with self._lock:
            self._state_listeners.append(listener)

        def unsub() -> None:
            with self._lock:
                if listener in self._state_listeners:
                    self._state_listeners.remove(listener)
        return unsub

    def get_state(self) -> str:
        return self._state

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="jarvis-stream",
                daemon=True,
            )
            self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._transition(STATE_CLOSED)
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(join_timeout)

    # --- internals --------------------------------------------------------

    def _transition(self, new_state: str) -> None:
        if self._state == new_state:
            return
        self._state = new_state
        with self._lock:
            listeners = list(self._state_listeners)
        for l in listeners:
            try:
                l(new_state)
            except Exception as exc:  # noqa: BLE001
                self._logger("[stream] state listener threw: %s" % exc)

    def _dispatch(self, event: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for l in listeners:
            try:
                l(event)
            except Exception as exc:  # noqa: BLE001
                self._logger(
                    "[stream] listener threw for %s: %s"
                    % (event.get("event_type"), exc)
                )

    def _compute_backoff(self) -> float:
        raw = _BASE_BACKOFF_S * (2 ** max(0, self._consecutive_failures - 1))
        capped = min(raw, self.reconnect_max_backoff_s)
        # Full-jitter: [0, capped).
        import random
        return random.random() * capped

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._consecutive_failures == 0:
                    self._transition(STATE_CONNECTING)
                else:
                    self._transition(STATE_RECONNECTING)
                self._connect_and_stream()
                self._consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    return
                self._consecutive_failures += 1
                self._logger(
                    "[stream] dropped: %s (failures=%d)"
                    % (exc, self._consecutive_failures)
                )
                self._transition(STATE_ERROR)
                if not self.auto_reconnect:
                    return
            if self._stop_event.is_set() or not self.auto_reconnect:
                return
            backoff = self._compute_backoff()
            self._sleep_with_cancel(backoff)

    def _sleep_with_cancel(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_event.is_set():
                return
            self._sleep_fn(min(0.25, end - time.monotonic()))

    def _connect_and_stream(self) -> None:
        path = "/observability/stream"
        if self.op_id_filter:
            _validate_op_id(self.op_id_filter)
            path += "?op_id=" + urllib.parse.quote(self.op_id_filter, safe="")
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-store",
        }
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id
        if self._connect_fn is not None:
            conn = self._connect_fn()
        else:
            conn, _, _ = _new_connection(self.endpoint, self.timeout_s)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise ObservabilityError(
                    "stream returned %d" % resp.status,
                    status=resp.status,
                    reason_code=_extract_reason(resp.read()),
                )
            self._transition(STATE_CONNECTED)
            self._consume_body(resp)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _consume_body(self, resp: Any) -> None:
        """Read SSE frames until EOF or stop_event. Mirrors the VS
        Code extension parser: accumulate into a text buffer, split
        on the ``\\n\\n`` delimiter, parse each frame."""
        buffer = ""
        # ``http.client.HTTPResponse.read(n)`` blocks until ``n``
        # bytes have arrived OR the connection closes. For a
        # long-lived SSE stream that behavior stalls the loop. Use
        # ``read1(n)`` (available in CPython 3.5+ and Sublime's
        # 3.8 interpreter) which returns as soon as ANY chunk is
        # available.
        read = getattr(resp, "read1", resp.read)
        while not self._stop_event.is_set():
            chunk = read(4096)
            if not chunk:
                return
            buffer += chunk.decode("utf-8", errors="replace")
            while True:
                sep_idx = buffer.find("\n\n")
                if sep_idx < 0:
                    break
                raw_event = buffer[:sep_idx]
                buffer = buffer[sep_idx + 2:]
                parsed = parse_sse_frame(raw_event)
                if parsed is None:
                    continue
                self._last_event_id = parsed.get("event_id", self._last_event_id)
                self._dispatch(parsed)


def parse_sse_frame(raw_event: str) -> Optional[Dict[str, Any]]:
    """Parse a single SSE text block into a StreamEvent dict.

    Returns ``None`` on malformed / schema-mismatched frames so the
    caller can drop silently without unwinding the read loop.
    Exported for unit testing.
    """
    event_id: Optional[str] = None
    event_type: Optional[str] = None
    data_parts: List[str] = []
    for line in raw_event.split("\n"):
        if line == "" or line.startswith(":"):
            continue
        colon = line.find(":")
        if colon < 0:
            continue
        field = line[:colon].strip()
        value = line[colon + 1:]
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_parts.append(value)
    if event_id is None or event_type is None or not data_parts:
        return None
    try:
        parsed = json.loads("\n".join(data_parts))
    except json.JSONDecodeError:
        return None
    if not _is_supported_schema(parsed):
        return None
    if event_type not in ALL_EVENT_TYPES:
        return None
    return parsed  # type: ignore[no-any-return]


def is_task_event(event: Dict[str, Any]) -> bool:
    return event.get("event_type") in TASK_EVENT_TYPES


def is_control_event(event: Dict[str, Any]) -> bool:
    return event.get("event_type") in CONTROL_EVENT_TYPES
