"""Tell a hardware downgrade apart from a software crash.

The attach path has one fallback and two entirely different reasons to take
it, and until now they printed the same line:

    ⎿ cockpit fallback → legacy view (ValueError: ...)

*Hardware* — no TTY, a kill-switch, a terminal that will not report its cursor
position. Expected, correct, and nothing is wrong. It should be quiet.

*Software* — the cockpit raised. A layout error, a bad IPC payload, an import
that broke. Something IS wrong, and the evidence was being truncated to 80
characters and thrown away with the traceback.

Collapsing those into one message is how a crash hides: the operator reads a
routine downgrade notice, the parachute opens, the session continues, and the
bug is never reported. This module keeps the parachute and makes the two
causes distinguishable — silence for hardware, a loud banner plus a full
traceback on disk for software.

Deliberately NOT re-raising. The fallback exists so a cockpit bug cannot brick
attach, and that judgement is unchanged. Being loud is not the same as being
fatal.
"""
from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.MountBreaker")

__all__ = [
    "HARDWARE",
    "SOFTWARE",
    "classify_mount_failure",
    "crash_banner",
    "crash_log_path",
    "record_mount_crash",
]

#: The terminal cannot host the cockpit. Expected; stay quiet.
HARDWARE = "hardware"
#: The cockpit raised. Unexpected; be loud and keep the evidence.
SOFTWARE = "software"


def classify_mount_failure(
    exc: Optional[BaseException], reason: str = "",
) -> str:
    """``HARDWARE`` or ``SOFTWARE``.

    An exception is the only reliable evidence of a software fault, so the
    classification is driven by whether one was raised rather than by pattern
    matching the reason string. A reason is prose written for an operator; it
    changes freely and is the wrong thing to branch on.
    """
    return SOFTWARE if exc is not None else HARDWARE


def crash_log_path() -> Path:
    """Where the traceback goes. ``JARVIS_OV_CRASH_LOG`` overrides."""
    override = os.environ.get("JARVIS_OV_CRASH_LOG", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("JARVIS_REPO_PATH", ".")) / ".jarvis" / "logs" / "ov-crash.log"


def record_mount_crash(
    exc: BaseException, *, path: Optional[Path] = None,
) -> Optional[Path]:
    """Append the full traceback to the crash log. Returns the path, or None.

    APPENDS rather than truncates: a cockpit that fails intermittently is a
    much harder bug than one that fails always, and the previous occurrences
    are the evidence that distinguishes them.

    Returns None on any I/O failure — an unwritable log must not turn a
    survivable crash into a fatal one, which would defeat the parachute this
    exists to instrument.
    """
    target = path if path is not None else crash_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n{'=' * 72}\n"
                f"[{stamp}] cockpit mount failed — degraded to safe mode\n"
                f"{'=' * 72}\n{body}"
            )
        return target
    except Exception:  # noqa: BLE001 — never escalate a survivable crash
        logger.debug("[MountBreaker] could not write crash log", exc_info=True)
        return None


def crash_banner(exc: BaseException, path: Optional[Path]) -> str:
    """The line an operator must not be able to scroll past without noticing.

    Plain ASCII and no markup: this is printed at the moment the rich surface
    has just proven it does not work, so it must not depend on anything the
    failure might have taken with it.
    """
    name = type(exc).__name__
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(detail) > 120:
        detail = detail[:117] + "..."
    where = str(path) if path else "(crash log unwritable)"
    return (
        "\n"
        "!! FATAL MOUNT EXCEPTION - DEGRADING TO SAFE MODE\n"
        f"!! {name}: {detail}\n"
        f"!! full traceback: {where}\n"
    )


def announce(
    exc: Optional[BaseException],
    reason: str,
    *,
    emit: Any = print,
    path: Optional[Path] = None,
) -> Tuple[str, Optional[Path]]:
    """Classify, record, and say the right thing. Returns (kind, log path).

    The single seam both fallback causes pass through, so they cannot drift
    into two policies. Hardware downgrades get one quiet line; software
    crashes get the banner and a file. NEVER raises.
    """
    try:
        kind = classify_mount_failure(exc, reason)
        if kind == HARDWARE:
            try:
                emit(f"⎿ cockpit fallback → legacy view ({reason or 'unknown'})")
            except Exception:  # noqa: BLE001
                pass
            return kind, None
        assert exc is not None
        log_path = record_mount_crash(exc, path=path)
        try:
            emit(crash_banner(exc, log_path))
        except Exception:  # noqa: BLE001
            pass
        logger.error(
            "[MountBreaker] cockpit mount raised %s — degraded to safe mode",
            type(exc).__name__, exc_info=exc,
        )
        return kind, log_path
    except Exception:  # noqa: BLE001
        return HARDWARE, None
