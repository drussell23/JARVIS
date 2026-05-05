"""BootTimer — lightweight phase-timing instrumentation for the
battle-test boot sequence.

The user-reported issue is "boot is slow." Without measurements,
optimization is guesswork. This module records named phase
boundaries with monotonic timestamps and emits a table on demand
(typically at end of boot under ``-v`` mode).

Design constraints
-------------------

* **Zero overhead when disabled** — the master flag
  :data:`MASTER_FLAG_ENV_VAR` defaults to ``true`` post graduation
  but the actual recording cost is ``time.monotonic()`` (single
  syscall) per phase boundary — negligible. The summary print is
  the only operator-visible output.
* **No hardcoded phase list** — phases are recorded by name, free-
  form. Calling ``timer.phase("preflight")`` is a context manager
  that records start + end automatically.
* **Single source of truth** — the singleton :func:`get_default_timer`
  is reusable across script + harness + REPL boot paths.
* **NEVER raises** — into the boot hot path. Defensive try/except
  on every public method.

Authority boundary
------------------

* §1 deterministic — pure timestamps + tabular emission; no LLM
* §7 fail-closed — recording errors degrade silently; invalid input
  yields no-op records
* §8 observable — :class:`PhaseRecord` projection ready for SSE
  publication via a future `boot_timed` event
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("Ouroboros.BootTiming")


BOOT_TIMING_SCHEMA_VERSION: str = "boot_timing.v1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_BOOT_TIMING_ENABLED"


def is_boot_timing_enabled() -> bool:
    """``JARVIS_BOOT_TIMING_ENABLED``. Default true — recording is
    cheap and the summary is only printed on explicit ``emit_summary``
    or ``-v`` mode. Operators set ``=false`` for absolute zero
    overhead. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class PhaseRecord:
    """One recorded phase boundary.

    Fields
    ------
    * ``name`` — operator-readable phase label (e.g. ``"preflight"``).
    * ``started_at`` — ``time.monotonic()`` at phase start.
    * ``ended_at`` — ``time.monotonic()`` at phase end. ``0.0``
      when the phase is still in flight.
    * ``elapsed_s`` — convenience: ``ended_at - started_at``, ``0.0``
      while in flight.
    * ``parent`` — name of the enclosing phase (when nested), or
      empty string for top-level. Lets the summary indent nested
      phases.
    """

    name: str
    started_at: float
    ended_at: float
    parent: str
    schema_version: str = BOOT_TIMING_SCHEMA_VERSION

    @property
    def elapsed_s(self) -> float:
        if self.ended_at <= 0.0:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    @property
    def is_in_flight(self) -> bool:
        return self.ended_at <= 0.0


# ===========================================================================
# BootTimer — the recorder
# ===========================================================================


class BootTimer:
    """Records named phases of a boot sequence.

    Thread-safe (uses an ``RLock``). Phases can be nested via the
    :meth:`phase` context manager. Marks (one-shot timestamps) are
    also supported via :meth:`mark`.
    """

    def __init__(self) -> None:
        self._records: List[PhaseRecord] = []
        self._stack: List[Tuple[str, float]] = []
        self._lock = threading.RLock()
        self._boot_start: float = time.monotonic()

    # ---- recording ----------------------------------------------------

    def mark(self, name: object) -> None:
        """One-shot timestamp record (zero-duration phase). Useful for
        recording milestones like "REPL prompt rendered" without
        tracking duration. NEVER raises."""
        if not is_boot_timing_enabled():
            return
        try:
            safe = str(name) if name is not None else ""
            if not safe:
                return
            now = time.monotonic()
            parent = self._stack[-1][0] if self._stack else ""
            with self._lock:
                self._records.append(PhaseRecord(
                    name=safe, started_at=now, ended_at=now, parent=parent,
                ))
        except Exception:  # noqa: BLE001
            logger.debug("[BootTimer] mark failed", exc_info=True)

    def begin(self, name: object) -> None:
        """Start a phase. Pairs with :meth:`end`. Prefer the
        :meth:`phase` context manager for automatic balance.
        NEVER raises."""
        if not is_boot_timing_enabled():
            return
        try:
            safe = str(name) if name is not None else ""
            if not safe:
                return
            now = time.monotonic()
            with self._lock:
                self._stack.append((safe, now))
        except Exception:  # noqa: BLE001
            logger.debug("[BootTimer] begin failed", exc_info=True)

    def end(self, name: object) -> None:
        """End the most-recently-started phase (matched by name).
        Mismatched names are tolerated — the actual top-of-stack is
        ended regardless. NEVER raises."""
        if not is_boot_timing_enabled():
            return
        try:
            now = time.monotonic()
            with self._lock:
                if not self._stack:
                    return
                top_name, started = self._stack.pop()
                parent = self._stack[-1][0] if self._stack else ""
                self._records.append(PhaseRecord(
                    name=top_name, started_at=started,
                    ended_at=now, parent=parent,
                ))
        except Exception:  # noqa: BLE001
            logger.debug("[BootTimer] end failed", exc_info=True)

    def phase(self, name: object) -> "_PhaseContext":
        """Context manager that calls ``begin(name)`` on entry and
        ``end(name)`` on exit (even on exception). Preferred over
        manual begin/end pairing.

        Usage::

            with timer.phase("harness_boot"):
                boot_harness()
        """
        return _PhaseContext(self, name)

    # ---- query --------------------------------------------------------

    def records(self) -> Tuple[PhaseRecord, ...]:
        """Snapshot of recorded phases (most recent first)."""
        with self._lock:
            return tuple(self._records)

    def total_elapsed_s(self) -> float:
        """Total time since timer construction."""
        return max(0.0, time.monotonic() - self._boot_start)

    # ---- emit ---------------------------------------------------------

    def emit_summary(
        self, *,
        console: object = None,
        sort_by: str = "elapsed",
        threshold_ms: float = 1.0,
    ) -> str:
        """Format + emit a summary table.

        Parameters
        ----------
        console :
            Rich Console instance (optional). When ``None``, returns
            the formatted string instead of emitting.
        sort_by :
            ``"elapsed"`` (default) — slowest phases first;
            ``"order"`` — recording order.
        threshold_ms :
            Phases shorter than this are filtered out (reduces noise
            from sub-millisecond phases). Default 1ms.

        Returns the formatted string. NEVER raises.
        """
        try:
            recs = list(self.records())
        except Exception:  # noqa: BLE001
            return ""

        # Filter + sort
        recs = [r for r in recs if r.elapsed_s * 1000 >= threshold_ms]
        if sort_by == "elapsed":
            recs.sort(key=lambda r: r.elapsed_s, reverse=True)

        lines: List[str] = []
        lines.append("")
        lines.append(f"  Boot timing  (total: {self.total_elapsed_s() * 1000:.1f}ms)")
        lines.append("  " + "─" * 52)
        for r in recs:
            indent = "    " if r.parent else "  "
            elapsed_ms = r.elapsed_s * 1000
            bar_chars = min(40, int(elapsed_ms / 25))  # 25ms per char
            bar = "█" * bar_chars
            lines.append(
                f"{indent}{r.name:<32s} {elapsed_ms:>7.1f}ms  {bar}"
            )
        lines.append("")
        text = "\n".join(lines)

        if console is not None:
            try:
                print_fn = getattr(console, "print", None)
                if callable(print_fn):
                    print_fn(text, highlight=False)
            except Exception:  # noqa: BLE001
                pass
        return text

    def reset(self) -> None:
        """Clear all records and restart timing. For test isolation."""
        with self._lock:
            self._records.clear()
            self._stack.clear()
            self._boot_start = time.monotonic()


class _PhaseContext:
    """Internal helper for :meth:`BootTimer.phase`."""

    __slots__ = ("_timer", "_name")

    def __init__(self, timer: BootTimer, name: object) -> None:
        self._timer = timer
        self._name = name

    def __enter__(self) -> "_PhaseContext":
        self._timer.begin(self._name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._timer.end(self._name)
        return False  # don't swallow exceptions


# ===========================================================================
# Module singleton
# ===========================================================================


_default_timer: Optional[BootTimer] = None
_singleton_lock = threading.Lock()


def get_default_timer() -> BootTimer:
    """Return the process-wide boot timer (constructed lazily on
    first access)."""
    global _default_timer
    with _singleton_lock:
        if _default_timer is None:
            _default_timer = BootTimer()
        return _default_timer


def reset_default_timer_for_tests() -> None:
    """Test isolation."""
    global _default_timer
    with _singleton_lock:
        _default_timer = None


__all__ = [
    "BOOT_TIMING_SCHEMA_VERSION",
    "BootTimer",
    "MASTER_FLAG_ENV_VAR",
    "PhaseRecord",
    "get_default_timer",
    "is_boot_timing_enabled",
    "reset_default_timer_for_tests",
]
