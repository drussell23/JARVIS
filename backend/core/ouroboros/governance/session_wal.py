"""Session WAL — Slice 12G-3 continuous state checkpointing.

The harness's ``_generate_report`` writes ``summary.json`` at the
end of a clean shutdown. When the asyncio loop wedges and the
WallClockWatchdog Layer-3 RESOURCE-ZERO HARD KILL fires (or the
LoopDeadman Slice 12G-2 ``os._exit(75)`` path triggers), that
final write never lands and the session dir is left with only
``debug.log``. Empirical evidence: bt-2026-05-22-195721 — a
real SWE-Bench-Pro element-web run wedged 82 minutes and lost
its verdict artifact entirely.

The operator's explicit rejection of Slice 12C ("artifact
backstop / panic-save") is honored here: **this is NOT a
last-second band-aid**. Instead, the WAL writes the latest
session state to disk *continuously* — at every phase boundary,
every op terminal, every structural fault — so when SIGKILL
drops, the latest checkpoint is already at rest.

## Architecture

  * **Atomic write** — every checkpoint goes to a temp file in
    the session dir, then ``os.replace`` renames it on top of
    the canonical ``summary.json``. POSIX guarantees ``rename``
    is atomic; readers always see a coherent snapshot.
  * **Schema-pinned envelope** — the on-disk format matches the
    ``_generate_report`` shape byte-for-byte, with the addition
    of a ``checkpoint_iso`` timestamp + ``checkpoint_reason``
    string. Downstream parsers (LastSessionSummary, etc.) ignore
    unknown fields.
  * **Best-effort by construction** — checkpoint failure NEVER
    bubbles into the asyncio loop. The contract is: "write IF
    you can; the prior checkpoint stays valid otherwise".
  * **Bounded I/O cost** — checkpoint writes are ~1KB per call,
    one syscall per phase-boundary. No batching needed.
  * **Provenance** — the latest checkpoint carries
    ``checkpoint_reason`` (e.g. ``phase_change:GENERATE``,
    ``operation_terminal:RESOLVED``, ``structural_fault:stream_rupture``)
    so postmortem analysis sees exactly what triggered each
    checkpoint.

## Discipline

  * Operator rejection of Slice 12C is honored: this module
    does NOT install signal handlers, does NOT fire on atexit,
    does NOT race against SIGKILL. It writes continuously
    during normal operation; if the process dies, the latest
    checkpoint is already at rest.
  * Pure stdlib (``json``, ``os``, ``pathlib``, ``time``,
    ``threading``).
  * Atomic rename on POSIX uses ``os.replace`` (guaranteed
    atomic same-filesystem rename per POSIX.1-2008).
  * NEVER raises into the caller — every public method wraps
    in try/except.

## Env knobs

  * ``JARVIS_SESSION_WAL_ENABLED``           — master gate (default TRUE).
  * ``JARVIS_SESSION_WAL_MIN_INTERVAL_S``    — debounce floor between writes (default 0.5s).
  * ``JARVIS_SESSION_WAL_INCLUDE_DEBUG_LOG`` — include sample of recent debug log lines in checkpoint (default FALSE — bounded I/O).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


logger = logging.getLogger("Ouroboros.SessionWAL")


# ============================================================================
# Env-knob resolvers
# ============================================================================


def wal_enabled() -> bool:
    """``JARVIS_SESSION_WAL_ENABLED`` — default TRUE. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_SESSION_WAL_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def wal_min_interval_s() -> float:
    """Debounce floor between consecutive writes. Default 0.5s —
    prevents a phase-storm from hammering the disk while still
    granular enough that no significant state is lost."""
    try:
        raw = os.environ.get(
            "JARVIS_SESSION_WAL_MIN_INTERVAL_S", "",
        ).strip()
        v = float(raw) if raw else 0.5
        return max(0.0, min(60.0, v))
    except (TypeError, ValueError):
        return 0.5


# ============================================================================
# SessionWAL
# ============================================================================


class SessionWAL:
    """Atomic continuous checkpointing for ``summary.json``.

    Lifecycle:
      * ``SessionWAL(session_dir)`` — instantiates against a
        session directory; reads the prior checkpoint if one
        exists (resume scenarios are read-only here).
      * ``checkpoint(state, reason)`` — atomic write of the
        current state with a provenance label. Debounced by
        ``wal_min_interval_s``.
      * ``force_checkpoint(state, reason)`` — bypass the
        debounce. Used at phase boundaries that MUST land
        (e.g. operation_terminal events).
      * No explicit close — the file always reflects the latest
        successful write.
    """

    __slots__ = (
        "_session_dir",
        "_summary_path",
        "_lock",
        "_last_write_at",
        "_checkpoint_count",
        "_last_reason",
        "_min_interval_s",
    )

    def __init__(self, session_dir: Path) -> None:
        self._session_dir: Path = Path(session_dir)
        self._summary_path: Path = self._session_dir / "summary.json"
        self._lock: threading.Lock = threading.Lock()
        self._last_write_at: float = 0.0
        self._checkpoint_count: int = 0
        self._last_reason: str = ""
        self._min_interval_s: float = wal_min_interval_s()

    # ---- introspection ----

    @property
    def summary_path(self) -> Path:
        return self._summary_path

    @property
    def checkpoint_count(self) -> int:
        return self._checkpoint_count

    @property
    def last_reason(self) -> str:
        return self._last_reason

    # ---- public API ----

    def checkpoint(
        self,
        state: Dict[str, Any],
        reason: str = "",
    ) -> bool:
        """Write ``state`` atomically to ``summary.json``,
        debounced by ``wal_min_interval_s``. Returns True on
        successful write, False on skip/error. NEVER raises."""
        if not wal_enabled():
            return False
        try:
            now = time.monotonic()
            with self._lock:
                if (
                    self._last_write_at > 0
                    and now - self._last_write_at < self._min_interval_s
                ):
                    return False  # debounced
                ok = self._atomic_write(state, reason)
                if ok:
                    self._last_write_at = now
                    self._checkpoint_count += 1
                    self._last_reason = reason
                return ok
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[SessionWAL] checkpoint swallowed exc",
                exc_info=True,
            )
            return False

    def force_checkpoint(
        self,
        state: Dict[str, Any],
        reason: str = "",
    ) -> bool:
        """Bypass the debounce — write immediately. Used at
        critical transitions (operation_terminal, structural
        fault, etc.) that MUST land. NEVER raises."""
        if not wal_enabled():
            return False
        try:
            with self._lock:
                ok = self._atomic_write(state, reason)
                if ok:
                    self._last_write_at = time.monotonic()
                    self._checkpoint_count += 1
                    self._last_reason = reason
                return ok
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SessionWAL] force_checkpoint swallowed exc",
                exc_info=True,
            )
            return False

    # ---- internals ----

    def _atomic_write(
        self,
        state: Dict[str, Any],
        reason: str,
    ) -> bool:
        """Write ``state`` to ``summary.json`` via temp +
        ``os.replace``. POSIX guarantees same-filesystem rename
        is atomic; readers always see a coherent snapshot.
        NEVER raises."""
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            payload = dict(state)  # shallow copy — caller keeps theirs
            payload.setdefault("schema_version", 2)
            payload["checkpoint_iso"] = _utc_iso_now()
            payload["checkpoint_reason"] = str(reason)[:128]
            payload["checkpoint_seq"] = self._checkpoint_count + 1
            tmp_path = self._summary_path.with_suffix(
                ".json.wal-tmp",
            )
            # Encode + write to temp.
            data = json.dumps(
                payload, indent=2, sort_keys=True,
                default=_json_fallback,
            )
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data)
                f.write("\n")
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
            # POSIX atomic rename — replace summary.json.
            os.replace(tmp_path, self._summary_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[SessionWAL] atomic_write failed reason=%r: %s",
                reason, exc, exc_info=True,
            )
            return False


# ============================================================================
# Helpers
# ============================================================================


def _utc_iso_now() -> str:
    """ISO-8601 wall-clock timestamp. Independent of the
    asyncio loop (uses ``time.time()`` directly)."""
    from datetime import datetime, timezone
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="microseconds")
    )


def _json_fallback(obj: Any) -> Any:
    """``json.dumps(default=)`` fallback — coerces non-serializable
    fields to their string repr instead of raising."""
    try:
        # Enum-like
        if hasattr(obj, "value"):
            return obj.value
        # Path / pathlike
        if hasattr(obj, "__fspath__"):
            return str(obj)
        # Set
        if isinstance(obj, (set, frozenset)):
            return sorted(str(x) for x in obj)
    except Exception:  # noqa: BLE001
        pass
    return str(obj)


# ============================================================================
# Process-singleton accessor (one WAL per session)
# ============================================================================


_default_wal: Optional[SessionWAL] = None
_default_lock: threading.Lock = threading.Lock()


def install_default_wal(session_dir: Path) -> SessionWAL:
    """Install the process-singleton WAL for the current session.
    Idempotent — subsequent calls return the existing instance.
    NEVER raises."""
    global _default_wal
    with _default_lock:
        if _default_wal is not None:
            return _default_wal
        _default_wal = SessionWAL(session_dir)
        return _default_wal


def get_default_wal() -> Optional[SessionWAL]:
    """Returns the installed WAL or None when not installed.
    Callers MUST handle None — the WAL is opt-in by design."""
    return _default_wal


def reset_default_wal() -> None:
    """For tests."""
    global _default_wal
    with _default_lock:
        _default_wal = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "SessionWAL",
    "wal_enabled",
    "wal_min_interval_s",
    "install_default_wal",
    "get_default_wal",
    "reset_default_wal",
]
