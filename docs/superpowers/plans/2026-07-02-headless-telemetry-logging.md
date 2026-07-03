# Asynchronous Headless Telemetry Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kill the observation-apparatus OOM class (67 GB Terminal scrollback killed soak bt-iso-1782987919 via SIGTERM) by making the telemetry stream non-blocking, size-rotated on disk, and reducing the live terminal to a rate-limited single-line heartbeat.

**Architecture:** A new `headless_telemetry.py` module provides (1) `NonBlockingQueueHandler` — a `logging.handlers.QueueHandler` whose records drain through a daemon-thread `QueueListener` into a `RotatingFileHandler` (env-driven 50 MB chunks), so no log call ever does disk I/O on the emitting thread/event loop; (2) `HeartbeatConsoleHandler` — a rate-limited, `\r`-rewriting single-line liveness surface on the real TTY. Integration extends the EXISTING `silent_boot.configure_silent_boot` organ (it already owns console-muting + file routing + the harness idempotency contract) — gated, fail-soft, byte-identical legacy fallback.

**Tech Stack:** Python 3.9+ stdlib only (`logging.handlers`, `queue.SimpleQueue`, `threading` via QueueListener). No new dependencies.

## Global Constraints

- Master switch `JARVIS_HEADLESS_TELEMETRY_ENABLED` default **true** (mirrors `JARVIS_SILENT_BOOT_ENABLED` default-true precedent for logging glue; hot-revert via `=false` restores the legacy blocking FileHandler path byte-identically).
- **NEVER raises. Boot is not blocked by logging glue** — same defensive contract as silent_boot; any failure in the non-blocking path falls back to the legacy `logging.FileHandler` path.
- **Auditor contract preserved:** the session log stays at `session_dir/debug.log` (CLAUDE.md: debug.log is the canonical truth source for "did the loop work"). Rotation chunks default to 50 MB (`JARVIS_TELEMETRY_LOG_MAX_BYTES`, default `52428800`) — a normal 1-hour session writes <1 MB and never rotates; rotation only guards pathological log storms.
- When no session dir is supplied, the rotating sink defaults to `JARVIS_TELEMETRY_LOG_DIR` (default `.ouroboros/logs/`) with filename `telemetry.log`.
- All knobs env-driven, no hardcoding: `JARVIS_TELEMETRY_LOG_MAX_BYTES` (50 MB), `JARVIS_TELEMETRY_LOG_BACKUPS` (10), `JARVIS_TELEMETRY_LOG_DIR` (`.ouroboros/logs`), `JARVIS_CONSOLE_HEARTBEAT_ENABLED` (true), `JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S` (30.0), `JARVIS_CONSOLE_HEARTBEAT_WIDTH` (120).
- Heartbeat writes ONLY to a real TTY: check `sys.__stdout__` and its `.isatty()` (the load-bearing presentation_restraint lesson — `sys.stdout.isatty()` lies under `patch_stdout(raw=True)`). Headless/CI/pipe → heartbeat is a silent no-op.
- The harness handler contract must keep working: `configure_silent_boot` return value exposes `.baseFilename` (harness.py:830 reads it) and `.close()`; the idempotency checks at silent_boot.py:216 and harness.py:2208-2212 currently require `isinstance(h, logging.FileHandler)` and MUST be loosened to marker + `hasattr(h, "baseFilename")` or the legacy fallback double-installs (the 2x-emission bug those gates exist to prevent).
- `python3 -m pytest` from repo root.

---

## File Structure

- **Create:** `backend/core/ouroboros/governance/headless_telemetry.py` — `NonBlockingQueueHandler`, `HeartbeatConsoleHandler`, env resolvers, `register_flags()`.
- **Modify:** `backend/core/ouroboros/governance/silent_boot.py` — `_configure_locked` builds the non-blocking pipeline when enabled (legacy path on flag-off or failure); loosen idempotency isinstance; install heartbeat handler.
- **Modify:** `backend/core/ouroboros/battle_test/harness.py:2208-2212` — loosen the `_sb_installed` isinstance gate to match.
- **Create:** `tests/governance/test_headless_telemetry.py`.

---

### Task L1: `headless_telemetry.py` — non-blocking rotating pipeline + heartbeat handler

**Files:**
- Create: `backend/core/ouroboros/governance/headless_telemetry.py`
- Test: `tests/governance/test_headless_telemetry.py`

**Interfaces:**
- Produces: `is_enabled() -> bool`; `resolve_telemetry_path(session_dir: Optional[Path], filename: str) -> Path`; `class NonBlockingQueueHandler(logging.handlers.QueueHandler)` with attrs `.baseFilename: str`, `.listener`, and `.close()` that stops the listener and closes the wrapped file handler; `def build_nonblocking_handler(log_path: Path, formatter: logging.Formatter, level: int) -> NonBlockingQueueHandler`; `class HeartbeatConsoleHandler(logging.Handler)`; `def register_flags(registry) -> int` (7 seeds incl. master).

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_headless_telemetry.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_headless_telemetry.py -v`
Expected: FAIL — `ModuleNotFoundError: ... headless_telemetry`

- [ ] **Step 3: Implement the module**

```python
# backend/core/ouroboros/governance/headless_telemetry.py
"""Asynchronous headless telemetry — non-blocking rotating log pipeline.

Root cause fixed: the observation apparatus. Session bt-iso-1782987919
died to SIGTERM after Terminal scrollback ballooned to 67 GB; separately,
the legacy logging.FileHandler does disk I/O on the emitting thread —
i.e., on the asyncio event loop. This module provides:

  * NonBlockingQueueHandler — QueueHandler -> daemon QueueListener ->
    RotatingFileHandler. Emitters enqueue (lock-free SimpleQueue.put)
    and return; disk I/O happens on the listener thread. Size-based
    rotation guards disk exhaustion.
  * HeartbeatConsoleHandler — the ONLY terminal surface: a rate-limited
    single updating line (carriage-return rewrite, no scrollback).

Consumed by silent_boot._configure_locked (the existing console-muting
organ). Same defensive contract: NEVER raises; boot is not blocked by
logging glue. Authority-free.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FLAG_MASTER = "JARVIS_HEADLESS_TELEMETRY_ENABLED"
_FLAG_MAX_BYTES = "JARVIS_TELEMETRY_LOG_MAX_BYTES"
_FLAG_BACKUPS = "JARVIS_TELEMETRY_LOG_BACKUPS"
_FLAG_LOG_DIR = "JARVIS_TELEMETRY_LOG_DIR"
_FLAG_HB_ENABLED = "JARVIS_CONSOLE_HEARTBEAT_ENABLED"
_FLAG_HB_INTERVAL = "JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S"
_FLAG_HB_WIDTH = "JARVIS_CONSOLE_HEARTBEAT_WIDTH"

_DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB chunks
_DEFAULT_BACKUPS = 10
_DEFAULT_LOG_DIR = os.path.join(".ouroboros", "logs")
_DEFAULT_HB_INTERVAL_S = 30.0
_DEFAULT_HB_WIDTH = 120


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return _env_bool(_FLAG_MASTER, True)


def resolve_telemetry_path(session_dir: Optional[Any], filename: str) -> Path:
    """Session dir wins (auditor contract: debug.log colocates with the
    session artifacts); otherwise the global env-driven telemetry dir."""
    if session_dir is not None:
        return Path(session_dir) / filename
    return Path(os.getenv(_FLAG_LOG_DIR, "") or _DEFAULT_LOG_DIR) / filename


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler bound to its own QueueListener + RotatingFileHandler.

    Exposes .baseFilename (harness contract) and .close() that tears the
    whole pipeline down (stop listener -> drain -> close file handler).
    """

    def __init__(
        self,
        record_queue: "queue.SimpleQueue",
        listener: logging.handlers.QueueListener,
        file_handler: logging.handlers.RotatingFileHandler,
    ) -> None:
        super().__init__(record_queue)
        self.listener = listener
        self._file_handler = file_handler
        self.baseFilename = file_handler.baseFilename

    def close(self) -> None:  # noqa: D102 — logging.Handler override
        try:
            self.listener.stop()  # joins the daemon thread, drains queue
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        try:
            self._file_handler.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


def build_nonblocking_handler(
    log_path: Any, formatter: logging.Formatter, level: int,
) -> NonBlockingQueueHandler:
    """Assemble queue -> listener -> rotating sink. Raises on failure —
    the CALLER (silent_boot) owns the fail-soft fallback decision."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        str(path),
        maxBytes=_env_int(_FLAG_MAX_BYTES, _DEFAULT_MAX_BYTES),
        backupCount=_env_int(_FLAG_BACKUPS, _DEFAULT_BACKUPS),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    record_queue: "queue.SimpleQueue" = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(
        record_queue, file_handler, respect_handler_level=True,
    )
    listener.start()  # daemon thread — never blocks interpreter exit
    handler = NonBlockingQueueHandler(record_queue, listener, file_handler)
    handler.setLevel(level)
    return handler


class HeartbeatConsoleHandler(logging.Handler):
    """Single updating line on the real TTY, at most once per interval.

    Eliminates scrollback bloat structurally: carriage-return rewrite,
    never a newline. No TTY -> silent no-op. Never raises.
    """

    def __init__(self, stream: Optional[Any] = None) -> None:
        super().__init__(level=logging.NOTSET)
        self._stream = stream if stream is not None else sys.__stdout__
        self._last_emit = 0.0
        self._count = 0

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._count += 1
            if not _env_bool(_FLAG_HB_ENABLED, True):
                return
            stream = self._stream
            if stream is None or not getattr(stream, "isatty", lambda: False)():
                return
            now = self._now()
            interval = _env_float(_FLAG_HB_INTERVAL, _DEFAULT_HB_INTERVAL_S)
            if self._last_emit and (now - self._last_emit) < interval:
                return
            self._last_emit = now
            width = _env_int(_FLAG_HB_WIDTH, _DEFAULT_HB_WIDTH)
            line = "\r⏳ %s · %d records · last: %s %s" % (
                time.strftime("%H:%M:%S"),
                self._count,
                record.levelname,
                record.name,
            )
            stream.write(line[:width].ljust(width))
            stream.flush()
        except Exception:  # noqa: BLE001 — a heartbeat must never wound
            pass


def register_flags(registry: Any) -> int:
    """FlagRegistry seed hook (auto-discovered convention)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception:  # noqa: BLE001
        return 0
    src = "backend/core/ouroboros/governance/headless_telemetry.py"
    seeds = [
        FlagSpec(name=_FLAG_MASTER, type=FlagType.BOOL, default=True,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Non-blocking rotating telemetry pipeline "
                             "(QueueListener + RotatingFileHandler). "
                             "=false restores legacy blocking FileHandler.",
                 example="JARVIS_HEADLESS_TELEMETRY_ENABLED=false"),
        FlagSpec(name=_FLAG_MAX_BYTES, type=FlagType.INT,
                 default=_DEFAULT_MAX_BYTES,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Rotation chunk size for the telemetry sink.",
                 example="JARVIS_TELEMETRY_LOG_MAX_BYTES=52428800"),
        FlagSpec(name=_FLAG_BACKUPS, type=FlagType.INT, default=_DEFAULT_BACKUPS,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Rotated chunk retention count (disk-exhaustion guard).",
                 example="JARVIS_TELEMETRY_LOG_BACKUPS=10"),
        FlagSpec(name=_FLAG_LOG_DIR, type=FlagType.STR, default=_DEFAULT_LOG_DIR,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Global telemetry dir when no session dir exists.",
                 example="JARVIS_TELEMETRY_LOG_DIR=.ouroboros/logs"),
        FlagSpec(name=_FLAG_HB_ENABLED, type=FlagType.BOOL, default=True,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Single-line console heartbeat (TTY only).",
                 example="JARVIS_CONSOLE_HEARTBEAT_ENABLED=false"),
        FlagSpec(name=_FLAG_HB_INTERVAL, type=FlagType.FLOAT,
                 default=_DEFAULT_HB_INTERVAL_S,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Minimum seconds between heartbeat line rewrites.",
                 example="JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S=30"),
        FlagSpec(name=_FLAG_HB_WIDTH, type=FlagType.INT, default=_DEFAULT_HB_WIDTH,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Heartbeat line clamp width (columns).",
                 example="JARVIS_CONSOLE_HEARTBEAT_WIDTH=120"),
    ]
    registry.bulk_register(seeds)
    return len(seeds)
```

Implementer notes: (a) if `Category.OBSERVABILITY` does not exist in flag_registry.py, use the closest existing category (check the `Category` enum at flag_registry.py:148) and keep the test asserting only name/default; (b) Python 3.9 floor — `queue.SimpleQueue` and `QueueListener` are both 3.7+, fine.

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/governance/test_headless_telemetry.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/headless_telemetry.py tests/governance/test_headless_telemetry.py
git commit -m "feat(telemetry): non-blocking rotating log pipeline + single-line console heartbeat (OOM root-cause, Task L1)"
```

---

### Task L2: Integrate into silent_boot + harness idempotency gates

**Files:**
- Modify: `backend/core/ouroboros/governance/silent_boot.py` (`_configure_locked` at line 202: handler build at 239-257, idempotency isinstance at 215-219; `register_flags` may reference the new module's flags — do NOT duplicate seeds, headless_telemetry owns its own)
- Modify: `backend/core/ouroboros/battle_test/harness.py:2208-2212` (`_sb_installed` gate)
- Test: `tests/governance/test_headless_telemetry.py` (append integration tests)

**Interfaces:**
- Consumes: `build_nonblocking_handler`, `HeartbeatConsoleHandler`, `is_enabled` from Task L1; existing `_HANDLER_MARKER`, `log_filename()`, `terminal_level()` in silent_boot.
- Produces: `configure_silent_boot(...)` unchanged signature but return type widens to `Optional[logging.Handler]` (still exposes `.baseFilename`/`.close()`); heartbeat handler installed on root, marked with `_HANDLER_MARKER`.

- [ ] **Step 1: Write the failing integration tests**

```python
# append to tests/governance/test_headless_telemetry.py
import backend.core.ouroboros.governance.silent_boot as sb


def _cleanup_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, sb._HANDLER_MARKER, False):
            root.removeHandler(h)
            h.close()


def test_silent_boot_installs_nonblocking_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "true")
    try:
        handler = sb.configure_silent_boot(tmp_path / "sess")
        assert isinstance(handler, NonBlockingQueueHandler)
        assert handler.baseFilename.endswith("debug.log")
        # Idempotency: second call returns the SAME handler, no double-install
        again = sb.configure_silent_boot(tmp_path / "sess")
        assert again is handler
        marked = [
            h for h in logging.getLogger().handlers
            if getattr(h, sb._HANDLER_MARKER, False)
            and hasattr(h, "baseFilename")
        ]
        assert len(marked) == 1
    finally:
        _cleanup_root()


def test_silent_boot_flag_off_uses_legacy_filehandler(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "true")
    try:
        handler = sb.configure_silent_boot(tmp_path / "sess")
        assert type(handler) is logging.FileHandler  # byte-identical legacy
    finally:
        _cleanup_root()


def test_silent_boot_falls_back_when_pipeline_build_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "true")
    import backend.core.ouroboros.governance.headless_telemetry as ht

    def _boom(*a, **k):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(ht, "build_nonblocking_handler", _boom)
    try:
        handler = sb.configure_silent_boot(tmp_path / "sess")
        assert type(handler) is logging.FileHandler  # fail-soft fallback
    finally:
        _cleanup_root()


def test_silent_boot_installs_heartbeat_on_root(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HEADLESS_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SILENT_BOOT_ENABLED", "true")
    try:
        sb.configure_silent_boot(tmp_path / "sess")
        hbs = [
            h for h in logging.getLogger().handlers
            if isinstance(h, HeartbeatConsoleHandler)
        ]
        assert len(hbs) == 1
        assert getattr(hbs[0], sb._HANDLER_MARKER, False)
    finally:
        _cleanup_root()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_headless_telemetry.py -v -k silent_boot`
Expected: FAIL (legacy FileHandler returned where NonBlockingQueueHandler expected; no heartbeat installed)

- [ ] **Step 3: Modify `silent_boot._configure_locked`**

Replace the idempotency check (silent_boot.py:215-219):

```python
    for h in root.handlers:
        if getattr(h, _HANDLER_MARKER, False) and hasattr(h, "baseFilename"):
            return h
```

Replace the file-handler install block (silent_boot.py:239-257):

```python
    # Install the session sink at DEBUG level — full fidelity in the
    # file. Preferred: non-blocking rotating pipeline (headless
    # telemetry). Fallback: legacy blocking FileHandler — byte-identical
    # to the pre-telemetry behavior — on flag-off or ANY build failure.
    _formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = None
    try:
        from backend.core.ouroboros.governance import headless_telemetry as _ht
        if _ht.is_enabled():
            file_handler = _ht.build_nonblocking_handler(
                log_path, _formatter, logging.DEBUG,
            )
    except Exception:  # noqa: BLE001 — fail-soft to legacy
        logger.debug(
            "[silent_boot] non-blocking pipeline failed; legacy fallback",
            exc_info=True,
        )
        file_handler = None
    if file_handler is None:
        try:
            file_handler = logging.FileHandler(
                str(log_path), encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(_formatter)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[silent_boot] file handler install failed",
                exc_info=True,
            )
            return None
    # Mark so future re-calls skip re-install.
    setattr(file_handler, _HANDLER_MARKER, True)
    root.addHandler(file_handler)
```

After the terminal-handler block (silent_boot.py:281-298), append the heartbeat install:

```python
    # Heartbeat — the ONLY liveness surface on the terminal. Single
    # updating line, rate-limited, TTY-only. Eliminates the scrollback
    # bloat that OOM-killed bt-iso-1782987919 (67 GB Terminal).
    try:
        from backend.core.ouroboros.governance.headless_telemetry import (
            HeartbeatConsoleHandler as _HB,
            is_enabled as _ht_enabled,
        )
        if _ht_enabled():
            _hb = _HB()
            setattr(_hb, _HANDLER_MARKER, True)
            root.addHandler(_hb)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[silent_boot] heartbeat handler install failed",
            exc_info=True,
        )
```

Also update `configure_silent_boot`'s return annotation to `Optional[logging.Handler]` and its docstring's FileHandler references.

- [ ] **Step 4: Loosen the harness gate** (harness.py:2208-2212):

```python
                    _sb_installed = any(
                        getattr(_h, _SB_MARKER, False)
                        and hasattr(_h, "baseFilename")
                        for _h in _root.handlers
                    )
```

- [ ] **Step 5: Run the full telemetry suite + silent_boot regression**

Run: `python3 -m pytest tests/governance/test_headless_telemetry.py -v && python3 -m pytest tests/governance -k silent_boot -v`
Expected: 13 PASS in the new file; all existing silent_boot tests remain green (if any assert `isinstance(handler, logging.FileHandler)` on the default path, they now need `JARVIS_HEADLESS_TELEMETRY_ENABLED=false` set or the assertion widened to `hasattr(handler, "baseFilename")` — widening is correct, the contract IS baseFilename+close, update those tests accordingly)

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/silent_boot.py backend/core/ouroboros/governance/headless_telemetry.py backend/core/ouroboros/battle_test/harness.py tests/governance/test_headless_telemetry.py
git commit -m "feat(telemetry): silent_boot builds non-blocking rotating pipeline + heartbeat; loosen handler-marker gates (Task L2)"
```

---

## Verification Bar (post-plan)

- `python3 -m pytest tests/governance/test_headless_telemetry.py -v` — 13/13.
- Existing silent_boot invariant validators (`register_shipped_invariants`, silent_boot.py:537) still pass — the AST checks for no-rich-import / no-authority-imports apply to silent_boot itself and must not be broken by the new import.
- Next soak inherits this automatically (silent_boot is already first-thing-in-`run()`); the console monitor idiom should ALSO change operationally: monitors tail the rotated file on demand instead of streaming to Terminal scrollback.
