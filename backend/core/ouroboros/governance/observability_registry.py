"""Slice 193 — Sovereign Telemetry Registry: structured, durable, unsuppressed
counters for the proactive transport-hedge (Manifesto §7).

The Slice 192 live verdict exposed a structural gap: the hedge's win/loss/
swallow telemetry is logger.info — invisible at the soak's WARNING console
threshold — and the Slice 190 economic counters are in-memory only, lost on
every restart. Promoting victory logs to WARNING would dilute the warning
channel; the correct answer is metrics, not noisier text.

This module is that answer:

  * :class:`ObservabilityRegistry` — thread-safe atomic counters backed by a
    fixed-slot memory-mapped file (``.jarvis/observability_registry.bin``).
    An increment is one lock-guarded ``struct.pack_into`` against the mmap —
    a page-cache write, no syscall I/O, no fsync — so the dispatch throat
    pays microseconds. Durability comes from the OS page cache plus a
    background daemon flusher thread (msync off the hot path, zero GIL
    contention during generation).
  * Fail-soft everywhere: a corrupt, missing-dir, or unwritable backing file
    degrades to an in-memory dict — counting still works, NOTHING raises
    into the dispatch throat. The corrupt file is left in place (evidence,
    not destruction).
  * :func:`record_hedge_dispatch` / :func:`record_hedge_outcome` — the two
    fire-and-forget helpers the doubleword_provider hedge block calls.
  * :func:`register_registry_routes` — GET ``/observability/registry``
    mounted on the EventChannelServer app, mirroring the metrics_observability
    pattern (403 master-off, 429 rate-limited, no-store, loopback-only via
    the caller's helpers).

Authority invariants: counts, never gates. No orchestrator / policy /
gate-family imports. The only I/O is the registry's own backing file.
"""
from __future__ import annotations

import logging
import mmap
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_OBSERVABILITY_REGISTRY_ENABLED"
_ENV_PATH = "JARVIS_OBSERVABILITY_REGISTRY_PATH"
_ENV_FLUSH_S = "JARVIS_OBSERVABILITY_REGISTRY_FLUSH_S"

_DEFAULT_PATH = ".jarvis/observability_registry.bin"
_DEFAULT_FLUSH_S = 5.0

REGISTRY_SCHEMA_VERSION = "1.0"

# Backing-file format (little-endian):
#   header  : magic b"JOR1" (4) + u32 version (4) + u32 used_slots (4) + pad (4)
#   slot[i] : name utf-8 NUL-padded (64) + u64 value (8)
_MAGIC = b"JOR1"
_FORMAT_VERSION = 1
_HEADER_SIZE = 16
_NAME_SIZE = 64
_SLOT_SIZE = _NAME_SIZE + 8
_MAX_SLOTS = 256
_FILE_SIZE = _HEADER_SIZE + _MAX_SLOTS * _SLOT_SIZE

# The long-window hedge metrics (Slice 193 charter + Slice 194 abandoned races).
HEDGE_CONCURRENCY_DISPATCHES = "hedge_concurrency_dispatches"
HEDGE_RT_VICTORIES = "hedge_rt_victories"
HEDGE_BATCH_VICTORIES = "hedge_batch_victories"
HEDGE_RUPTURES_SWALLOWED = "hedge_ruptures_swallowed"
# Slice 194 — a race where BOTH arms failed (no winner). Previously only
# derivable as dispatches − victories; now explicit and self-describing.
HEDGE_RACES_ABANDONED = "hedge_races_abandoned"
# Slice 197 — organism-health criteria for the M10 autonomous graduation
# contract. Previously log-line-only; charter counters make the graduation
# evaluation a pure read of the .bin (no log parsing).
PROVIDER_EXHAUSTIONS = "provider_exhaustions"
CONTROL_PLANE_STARVATION_EVENTS = "control_plane_starvation_events"

_PREREGISTERED = (
    HEDGE_CONCURRENCY_DISPATCHES,
    HEDGE_RT_VICTORIES,
    HEDGE_BATCH_VICTORIES,
    HEDGE_RUPTURES_SWALLOWED,
    HEDGE_RACES_ABANDONED,
    PROVIDER_EXHAUSTIONS,
    CONTROL_PLANE_STARVATION_EVENTS,
)


def observability_registry_enabled() -> bool:
    """Master gate (default TRUE — authority-free observability, economic
    telemetry precedent). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _registry_path() -> Path:
    raw = os.environ.get(_ENV_PATH, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_PATH)


def _flush_interval_s() -> float:
    try:
        raw = os.environ.get(_ENV_FLUSH_S, "").strip()
        v = float(raw) if raw else _DEFAULT_FLUSH_S
        return v if v > 0 else _DEFAULT_FLUSH_S
    except Exception:  # noqa: BLE001
        return _DEFAULT_FLUSH_S


class ObservabilityRegistry:
    """Thread-safe atomic counter registry, mmap-backed with in-memory
    fail-soft fallback. All public methods NEVER raise."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path if path is not None else _registry_path()
        self._mm: Optional[mmap.mmap] = None
        self._file = None
        self._slots: Dict[str, int] = {}  # name -> slot index (mmap mode)
        self._memory: Dict[str, int] = {}  # fallback / disabled mode
        self.backend_kind = "memory"
        self._flusher_stop = threading.Event()
        self._flusher: Optional[threading.Thread] = None
        if observability_registry_enabled():
            self._open_backend()
            for name in _PREREGISTERED:
                self._ensure_counter(name)
            if self._mm is not None:
                self._start_flusher()

    # -- backend -----------------------------------------------------------

    def _open_backend(self) -> None:
        """Open or create the mmap backing file; degrade to memory on ANY
        failure (corrupt file is left in place as evidence)."""
        try:
            existing = self._path.exists()
            if existing:
                if self._path.stat().st_size != _FILE_SIZE:
                    raise ValueError("backing file size mismatch")
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "wb") as fh:
                    fh.write(_MAGIC)
                    fh.write(struct.pack("<II", _FORMAT_VERSION, 0))
                    fh.write(b"\x00" * (_FILE_SIZE - 12))
            self._file = open(self._path, "r+b")
            self._mm = mmap.mmap(self._file.fileno(), _FILE_SIZE)
            if self._mm[:4] != _MAGIC:
                raise ValueError("bad magic")
            version, used = struct.unpack_from("<II", self._mm, 4)
            if version != _FORMAT_VERSION or used > _MAX_SLOTS:
                raise ValueError(f"unsupported header version={version} used={used}")
            for i in range(used):
                off = _HEADER_SIZE + i * _SLOT_SIZE
                name = self._mm[off:off + _NAME_SIZE].rstrip(b"\x00").decode(
                    "utf-8", errors="replace",
                )
                if name:
                    self._slots[name] = i
            self.backend_kind = "mmap"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ObservabilityRegistry] mmap backend unavailable (%s) — "
                "fail-soft to in-memory counters: %s",
                self._path, exc,
            )
            self._teardown_mmap()
            self.backend_kind = "memory"

    def _teardown_mmap(self) -> None:
        try:
            if self._mm is not None:
                self._mm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._file is not None:
                self._file.close()
        except Exception:  # noqa: BLE001
            pass
        self._mm = None
        self._file = None
        self._slots = {}

    def _ensure_counter(self, name: str) -> None:
        """Allocate a slot for *name* if absent. Caller need not hold the
        lock (construction) — incr() re-enters under the lock."""
        if self._mm is None:
            self._memory.setdefault(name, 0)
            return
        if name in self._slots:
            return
        used = len(self._slots)
        if used >= _MAX_SLOTS:
            # Registry full — overflow counters live in memory (still counted,
            # still in snapshot, just not crash-durable).
            self._memory.setdefault(name, 0)
            return
        off = _HEADER_SIZE + used * _SLOT_SIZE
        encoded = name.encode("utf-8")[:_NAME_SIZE]
        self._mm[off:off + _NAME_SIZE] = encoded.ljust(_NAME_SIZE, b"\x00")
        struct.pack_into("<Q", self._mm, off + _NAME_SIZE, 0)
        self._slots[name] = used
        struct.pack_into("<I", self._mm, 8, used + 1)

    # -- background flusher (durability off the hot path) -------------------

    def _start_flusher(self) -> None:
        interval = _flush_interval_s()

        def _run() -> None:
            while not self._flusher_stop.wait(interval):
                self.flush()

        self._flusher = threading.Thread(
            target=_run, name="observability-registry-flusher", daemon=True,
        )
        self._flusher.start()

    def flush(self) -> None:
        """msync the mmap (durability point). Off the hot path. NEVER raises."""
        try:
            with self._lock:
                if self._mm is not None:
                    self._mm.flush()
        except Exception:  # noqa: BLE001
            pass

    # -- public API ----------------------------------------------------------

    def incr(self, name: str, n: int = 1) -> None:
        """Atomically increment counter *name* by *n*. Microsecond lock +
        page-cache write — no I/O syscalls. NEVER raises."""
        if not observability_registry_enabled():
            return
        try:
            with self._lock:
                self._ensure_counter(name)
                idx = self._slots.get(name)
                if self._mm is not None and idx is not None:
                    off = _HEADER_SIZE + idx * _SLOT_SIZE + _NAME_SIZE
                    (current,) = struct.unpack_from("<Q", self._mm, off)
                    struct.pack_into("<Q", self._mm, off, current + n)
                else:
                    self._memory[name] = self._memory.get(name, 0) + n
        except Exception:  # noqa: BLE001
            pass

    def get(self, name: str) -> int:
        """Current value of *name* (0 if unknown or disabled). NEVER raises."""
        if not observability_registry_enabled():
            return 0
        try:
            with self._lock:
                idx = self._slots.get(name)
                if self._mm is not None and idx is not None:
                    off = _HEADER_SIZE + idx * _SLOT_SIZE + _NAME_SIZE
                    (value,) = struct.unpack_from("<Q", self._mm, off)
                    return int(value)
                return int(self._memory.get(name, 0))
        except Exception:  # noqa: BLE001
            return 0

    def snapshot(self) -> Dict[str, int]:
        """All counters as a plain dict ({} when disabled). NEVER raises."""
        if not observability_registry_enabled():
            return {}
        try:
            with self._lock:
                out: Dict[str, int] = {}
                if self._mm is not None:
                    for name, idx in self._slots.items():
                        off = _HEADER_SIZE + idx * _SLOT_SIZE + _NAME_SIZE
                        (value,) = struct.unpack_from("<Q", self._mm, off)
                        out[name] = int(value)
                for name, value in self._memory.items():
                    out.setdefault(name, int(value))
                return out
        except Exception:  # noqa: BLE001
            return {}

    def close(self) -> None:
        """Stop the flusher, msync, release the mmap. NEVER raises."""
        try:
            self._flusher_stop.set()
            if self._flusher is not None:
                self._flusher.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        self.flush()
        with self._lock:
            self._teardown_mmap()


# -- process-wide singleton ---------------------------------------------------

_singleton: Optional[ObservabilityRegistry] = None
_singleton_lock = threading.Lock()


def get_observability_registry() -> ObservabilityRegistry:
    """Process-wide singleton (double-checked lock). NEVER raises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ObservabilityRegistry()
    return _singleton


def _reset_singleton_for_tests() -> None:
    """Test seam — close and discard the singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
        _singleton = None


# -- dispatch-throat helpers (fire-and-forget, NEVER raise) -------------------

def record_hedge_dispatch() -> None:
    """One proactive hedge race launched (RT + batch in flight concurrently)."""
    try:
        get_observability_registry().incr(HEDGE_CONCURRENCY_DISPATCHES)
    except Exception:  # noqa: BLE001
        pass


def record_provider_exhaustion() -> None:
    """Slice 197 — one op died with all_providers_exhausted (every provider
    tier failed). The single strongest negative health signal."""
    try:
        get_observability_registry().incr(PROVIDER_EXHAUSTIONS)
    except Exception:  # noqa: BLE001
        pass


def record_control_plane_starvation() -> None:
    """Slice 197 — one ControlPlaneStarvation lag event (main asyncio loop
    failed to tick within threshold)."""
    try:
        get_observability_registry().incr(CONTROL_PLANE_STARVATION_EVENTS)
    except Exception:  # noqa: BLE001
        pass


def record_hedge_abandoned() -> None:
    """Slice 194 — one hedge race died with NO winner (both arms failed)."""
    try:
        get_observability_registry().incr(HEDGE_RACES_ABANDONED)
    except Exception:  # noqa: BLE001
        pass


def record_hedge_outcome(winner: str, rupture_swallowed: bool) -> None:
    """One hedge race settled: which transport won, and whether an RT rupture
    was made invisible by batch winning."""
    try:
        reg = get_observability_registry()
        if str(winner).strip().lower() == "rt":
            reg.incr(HEDGE_RT_VICTORIES)
        else:
            reg.incr(HEDGE_BATCH_VICTORIES)
        if rupture_swallowed:
            reg.incr(HEDGE_RUPTURES_SWALLOWED)
    except Exception:  # noqa: BLE001
        pass


# -- GET /observability/registry ----------------------------------------------

def register_registry_routes(
    app,
    rate_limit_check: Optional[Callable] = None,
    cors_headers: Optional[Callable] = None,
) -> None:
    """Mount GET /observability/registry on a caller-supplied aiohttp app.

    Mirrors the metrics_observability pattern: the caller (EventChannelServer)
    supplies the rate-limit + CORS helpers from a dedicated
    IDEObservabilityRouter instance so the operator-visible surface stays
    uniform; loopback enforcement happens at the caller before mounting.
    """
    from aiohttp import web  # lazy — module import stays aiohttp-free

    def _headers(request) -> Dict[str, str]:
        out = {"Cache-Control": "no-store"}
        if cors_headers is not None:
            try:
                out.update(cors_headers(request) or {})
            except Exception:  # noqa: BLE001
                pass
        return out

    async def _handle_registry(request):
        if not observability_registry_enabled():
            return web.json_response(
                {"error": True, "reason_code": "ide_observability.disabled"},
                status=403, headers=_headers(request),
            )
        if rate_limit_check is not None:
            try:
                allowed = rate_limit_check(request)
            except Exception:  # noqa: BLE001
                allowed = True
            if not allowed:
                return web.json_response(
                    {"error": True, "reason_code": "ide_observability.rate_limited"},
                    status=429, headers=_headers(request),
                )
        reg = get_observability_registry()
        # Slice 204 — surface the Chronos non-volatile uptime ledger alongside
        # the counters so the operator can see operational continuity in one
        # GET (best-effort; absent/disabled → omitted).
        chronos = None
        try:
            from backend.core.ouroboros.governance.chronos_ledger import (
                chronos_enabled as _chr_on, get_chronos_ledger as _chr_get,
            )
            if _chr_on():
                chronos = _chr_get().snapshot()
        except Exception:  # noqa: BLE001
            chronos = None
        return web.json_response(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "backend": reg.backend_kind,
                "counters": reg.snapshot(),
                "chronos": chronos,
                "generated_at_unix": time.time(),
            },
            status=200, headers=_headers(request),
        )

    app.router.add_get("/observability/registry", _handle_registry)
