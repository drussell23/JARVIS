"""Cinematic Boot Mux — structural TTY isolation for the ov boot.

Operator mandate 2026-07-18: early-boot housekeeping (BootExorcism,
CrossProcessJSONL stale-lock sweeps, intake_router cleanups, third-party
import chatter) bleeds onto the terminal BEFORE any presentation layer
exists, ruining the product boot. Silencing individual loggers is
whack-a-mole; the root fix is a MUX: from the first instruction of a
COCKPIT boot, ``sys.stdout``/``sys.stderr`` are tee streams that write
ONLY to an in-memory buffer + ``.ouroboros/boot.log`` — the TTY stays
perfectly black until the AwakeningConductor takes the stage.

Design invariants:

  * **Mode-flip, never object-swap**: the mux NEVER restores the
    original stream objects. Any console/handler that bound
    ``sys.stdout`` while muxed keeps working after release, because
    release flips the SAME stream into passthrough mode. No dangling
    writer ever points at a dead buffer.
  * **Dead-Man's Switch**: a fatal boot exception (or nonzero
    ``SystemExit``) flushes the hidden buffer to the REAL stderr —
    forensics are never lost to the cinematic ambition.
  * **DRY handoff**: release happens exactly where the presentation
    plane asserts control — the awakening ignition (and the
    single-flight collision surface, which IS a presentation moment) —
    so the PresentationRouter-conformed surfaces inherit a clean TTY.
  * Everything NEVER raises; a mux fault degrades to the noisy-but-
    functional legacy boot.

Master ``JARVIS_BOOT_MUX_ENABLED`` (default on; cockpit-only by call
placement — SOAK/headless never engages it).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

_TRUTHY = ("1", "true", "yes", "on")


def boot_mux_enabled() -> bool:
    """Master gate — default ON. NEVER raises."""
    return os.environ.get(
        "JARVIS_BOOT_MUX_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def _boot_log_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_BOOT_MUX_LOG", ".ouroboros/boot.log",
    ))


class _TeeStream:
    """A stand-in for stdout/stderr with two modes.

    ``capturing=True``: writes go to the shared buffer + boot.log —
    NOTHING reaches the real stream. ``capturing=False`` (released):
    writes pass straight through to the real stream. The object
    identity never changes, so late-bound writers survive the flip.
    """

    def __init__(self, real: Any, mux: "BootMux") -> None:
        self._real = real
        self._mux = mux

    # -- file-like protocol (defensive everywhere) --

    def write(self, data: str) -> int:
        try:
            if self._mux.capturing:
                self._mux._record(data)
                return len(data)
            return self._real.write(data)
        except Exception:  # noqa: BLE001
            try:
                return self._real.write(data)
            except Exception:  # noqa: BLE001
                return 0

    def flush(self) -> None:
        try:
            if not self._mux.capturing:
                self._real.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        # The REAL answer — Rich/prompt_toolkit probe this to pick
        # rendering tiers; the mux must be invisible to that decision.
        try:
            return bool(self._real.isatty())
        except Exception:  # noqa: BLE001
            return False

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._real, "errors", "replace")

    @property
    def closed(self) -> bool:
        return bool(getattr(self._real, "closed", False))

    def writable(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class BootMux:
    """The boot-time stream multiplexer. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.capturing = False
        self._engaged = False
        self._buffer: List[str] = []
        self._log_fh: Optional[Any] = None
        self._real_stdout: Optional[Any] = None
        self._real_stderr: Optional[Any] = None

    # -- capture side --

    def _record(self, data: str) -> None:
        with self._lock:
            self._buffer.append(data)
            if self._log_fh is not None:
                try:
                    self._log_fh.write(data)
                    self._log_fh.flush()
                except Exception:  # noqa: BLE001
                    self._log_fh = None

    # -- lifecycle --

    def engage(self) -> bool:
        """Silence the TTY: replace ``sys.stdout``/``sys.stderr`` with
        capturing tees. Idempotent. NEVER raises."""
        try:
            with self._lock:
                if self._engaged or not boot_mux_enabled():
                    return self._engaged
                try:
                    path = _boot_log_path()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._log_fh = open(  # noqa: SIM115 — held for boot lifetime
                        path, "a", encoding="utf-8", errors="replace",
                    )
                    self._log_fh.write(
                        "\n===== boot mux engaged (pid %d) =====\n" % os.getpid()
                    )
                except Exception:  # noqa: BLE001
                    self._log_fh = None
                self._real_stdout = sys.stdout
                self._real_stderr = sys.stderr
                sys.stdout = _TeeStream(self._real_stdout, self)  # type: ignore[assignment]
                sys.stderr = _TeeStream(self._real_stderr, self)  # type: ignore[assignment]
                self.capturing = True
                self._engaged = True
                return True
        except Exception:  # noqa: BLE001
            self.capturing = False
            return False

    def release(self, *, flush_to_tty: bool = False) -> None:
        """Hand the TTY to the presentation plane (mode-flip: the tee
        objects stay installed, now passing through). ``flush_to_tty``
        is the DEAD-MAN'S SWITCH: dump the hidden buffer to the REAL
        stderr first so a fatal boot never eats its own forensics.
        Idempotent. NEVER raises."""
        try:
            with self._lock:
                if not self._engaged:
                    return
                if flush_to_tty and self._buffer:
                    real = self._real_stderr or sys.__stderr__
                    if real is None:
                        self.capturing = False
                        return
                    try:
                        real.write(
                            "\n─── boot mux dead-man flush "
                            "(fatal during silent boot) ───\n"
                        )
                        real.write("".join(self._buffer))
                        real.flush()
                    except Exception:  # noqa: BLE001
                        pass
                self.capturing = False
        except Exception:  # noqa: BLE001
            self.capturing = False

    @property
    def engaged(self) -> bool:
        return self._engaged

    def buffered_chars(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buffer)


_MUX: Optional[BootMux] = None
_MUX_LOCK = threading.Lock()


def get_boot_mux() -> BootMux:
    global _MUX
    with _MUX_LOCK:
        if _MUX is None:
            _MUX = BootMux()
        return _MUX


def engage_boot_mux() -> bool:
    """Cockpit boot entry — call before ANY chatty import. NEVER raises."""
    return get_boot_mux().engage()


def release_boot_mux(*, flush_to_tty: bool = False) -> None:
    """The presentation handoff (awakening ignition / collision
    surface) or the dead-man flush (fatal path). NEVER raises."""
    get_boot_mux().release(flush_to_tty=flush_to_tty)


def reset_boot_mux_for_tests() -> None:
    global _MUX
    with _MUX_LOCK:
        _MUX = None


__all__ = [
    "BootMux",
    "boot_mux_enabled",
    "engage_boot_mux",
    "get_boot_mux",
    "release_boot_mux",
    "reset_boot_mux_for_tests",
]
