from __future__ import annotations

import logging
import time
from pathlib import Path

from backend.core.ouroboros.governance.headless_telemetry import (
    HeartbeatConsoleHandler,
    NonBlockingQueueHandler,
    build_nonblocking_handler,
    is_enabled,
    resolve_telemetry_path,
)

_FMT = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")


def test_master_switch_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", raising=False)
    assert is_enabled() is True
    monkeypatch.setenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", "false")
    assert is_enabled() is False


def test_resolve_path_prefers_session_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_TELEMETRY_LOG_DIR", raising=False)
    p = resolve_telemetry_path(tmp_path / "sess", "debug.log")
    assert p == tmp_path / "sess" / "debug.log"
    # No session dir -> env dir (default .ouroboros/logs) + telemetry.log
    monkeypatch.setenv("JARVIS_TELEMETRY_LOG_DIR", str(tmp_path / "global"))
    p2 = resolve_telemetry_path(None, "telemetry.log")
    assert p2 == tmp_path / "global" / "telemetry.log"


def test_records_drain_to_file_off_thread(tmp_path):
    log_path = tmp_path / "debug.log"
    handler = build_nonblocking_handler(log_path, _FMT, logging.DEBUG)
    try:
        lg = logging.getLogger("ht.test.drain")
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(handler)
        for i in range(50):
            lg.info("record %d", i)
        deadline = time.time() + 5
        while time.time() < deadline:
            if log_path.exists() and "record 49" in log_path.read_text():
                break
            time.sleep(0.02)
        text = log_path.read_text()
        assert "record 0" in text and "record 49" in text
        assert handler.baseFilename == str(log_path)
    finally:
        lg.removeHandler(handler)
        handler.close()


def test_rotation_fires_at_max_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TELEMETRY_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("JARVIS_TELEMETRY_LOG_BACKUPS", "2")
    log_path = tmp_path / "debug.log"
    handler = build_nonblocking_handler(log_path, _FMT, logging.DEBUG)
    try:
        lg = logging.getLogger("ht.test.rotate")
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(handler)
        for i in range(200):
            lg.info("x" * 64)
        deadline = time.time() + 5
        while time.time() < deadline and not (tmp_path / "debug.log.1").exists():
            time.sleep(0.02)
        assert (tmp_path / "debug.log.1").exists(), "rotation never fired"
        assert log_path.stat().st_size <= 4096
    finally:
        lg.removeHandler(handler)
        handler.close()


def test_emit_is_nonblocking_even_when_sink_wedges(tmp_path):
    """The emitting thread must never wait on disk I/O — a wedged sink
    cannot stall the caller (the event-loop-starvation failure class)."""
    log_path = tmp_path / "debug.log"
    handler = build_nonblocking_handler(log_path, _FMT, logging.DEBUG)
    try:
        # Wedge the downstream file handler: make its emit block forever.
        import threading
        gate = threading.Event()
        inner = handler.listener.handlers[0]
        orig_emit = inner.emit

        def _wedged_emit(record):
            gate.wait(timeout=30)
            orig_emit(record)

        inner.emit = _wedged_emit
        lg = logging.getLogger("ht.test.wedge")
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(handler)
        t0 = time.monotonic()
        for i in range(100):
            lg.info("must not block %d", i)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"emit blocked the caller for {elapsed:.2f}s"
        gate.set()
    finally:
        lg.removeHandler(handler)
        handler.close()


class _FakeTTY:
    def __init__(self):
        self.chunks: list = []

    def write(self, s):
        self.chunks.append(s)

    def flush(self):
        pass

    def isatty(self):
        return True


def _record(msg="hello", level=logging.INFO, name="ht.hb"):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_heartbeat_single_line_and_rate_limited(monkeypatch):
    monkeypatch.setenv("JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S", "10")
    fake = _FakeTTY()
    hb = HeartbeatConsoleHandler(stream=fake)
    now = [1000.0]
    monkeypatch.setattr(hb, "_now", lambda: now[0])
    hb.emit(_record("first"))
    for _ in range(50):
        hb.emit(_record("suppressed"))
    assert len(fake.chunks) == 1, "heartbeat must rate-limit within interval"
    assert fake.chunks[0].startswith("\r") and "\n" not in fake.chunks[0]
    now[0] += 11.0
    hb.emit(_record("second"))
    assert len(fake.chunks) == 2
    assert "52" in fake.chunks[1]  # cumulative record count surfaces liveness


def test_heartbeat_noop_without_tty(monkeypatch):
    class _Pipe(_FakeTTY):
        def isatty(self):
            return False

    pipe = _Pipe()
    hb = HeartbeatConsoleHandler(stream=pipe)
    hb.emit(_record())
    assert pipe.chunks == []


def test_heartbeat_never_raises_on_broken_stream():
    class _Broken:
        def write(self, s):
            raise OSError("gone")

        def flush(self):
            raise OSError("gone")

        def isatty(self):
            return True

    hb = HeartbeatConsoleHandler(stream=_Broken())
    hb.emit(_record())  # must not raise


def test_register_flags_seeds_all_knobs():
    from backend.core.ouroboros.governance.flag_registry import FlagRegistry
    from backend.core.ouroboros.governance import headless_telemetry as ht

    registry = FlagRegistry()
    n = ht.register_flags(registry)
    assert n == 7
    spec = registry.get_spec("JARVIS_HEADLESS_TELEMETRY_ENABLED")
    assert spec is not None and spec.default is True
