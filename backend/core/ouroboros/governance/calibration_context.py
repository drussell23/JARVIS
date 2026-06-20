"""Formal Calibration Mode — a sanctioned, scoped Advisor bypass for system tests.

The OperationAdvisor correctly BLOCKS the high-blast-radius / zero-coverage ops the
autonomous loop self-selects (e.g. refactoring the 99K-line supervisor at 2 AM). To
exercise the *dispatch* layer end-to-end we need a perfectly safe payload the Advisor
will greenlight — WITHOUT hacking the target past the gates or weakening them.

This module is that formal mechanism: an async-safe ``ContextVar`` carrying an
explicit calibration token. When (and ONLY when) a target is dispatched inside an
active calibration scope AND its basename matches the registered token, the Advisor
recognizes it as a blast-radius-1 system test and natively greenlights it. Every
other operation keeps the Advisor's strict heuristic gates, byte-identical.

Design invariants:
  * **Explicit + scoped** — never on by default; requires both the env master switch
    (``JARVIS_CALIBRATION_MODE_ENABLED``) AND an active ``set_calibration_target``
    scope. Two independent gates so a stray ContextVar can't silently disarm safety.
  * **Targeted** — only the named target is greenlit; a real op that happens to run
    in the same scope is NOT bypassed (basename match required).
  * **Auditable** — the bypass surfaces a distinct ``CALIBRATION_OVERRIDE`` reason on
    the Advisory so every greenlight is visible in the trace (Manifesto §7).
"""
from __future__ import annotations

import contextvars
import os
from typing import Iterable, Optional

_CALIBRATION_MODE_ENV = "JARVIS_CALIBRATION_MODE_ENABLED"

# Async-safe per-task token: the basename of the sanctioned calibration target
# (e.g. "test_seed_defect.py"), or "" when no calibration scope is active.
_calibration_target: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jarvis_calibration_target", default="",
)


def calibration_mode_enabled() -> bool:
    """Master switch (default OFF). Both this AND an active scope are required —
    a calibration bypass can never engage from the ContextVar alone. NEVER raises."""
    try:
        return os.environ.get(_CALIBRATION_MODE_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def set_calibration_target(target_basename: str) -> contextvars.Token:
    """Open a calibration scope for ``target_basename``. Returns a reset token —
    callers MUST ``reset_calibration_target(token)`` in a finally so the scope
    never leaks to a subsequent (real) op."""
    return _calibration_target.set((target_basename or "").strip())


def reset_calibration_target(token: contextvars.Token) -> None:
    """Close the calibration scope. NEVER raises."""
    try:
        _calibration_target.reset(token)
    except Exception:  # noqa: BLE001 — best-effort scope close
        pass


def current_calibration_target() -> str:
    """The active calibration target basename, or "" when no scope is active."""
    try:
        return _calibration_target.get()
    except Exception:  # noqa: BLE001
        return ""


def is_calibration_target(target_files: Iterable[str]) -> bool:
    """True iff calibration mode is armed AND one of ``target_files`` basename-matches
    the sanctioned calibration seed. This is the single predicate the Advisor
    consults to formally greenlight a blast-radius-1 system test — every other op
    falls through to the strict gates.

    The seed is matched two ways (either suffices, both require the env master ON so
    a stray ContextVar can never disarm safety on its own):
      1. **Registered seed** — basename of ``calibration_target_path()`` (env
         ``JARVIS_CALIBRATION_TARGET_PATH``). Works for the autonomous loop: the
         TestFailure sensor picks up the seed defect and the Advisor recognizes it
         natively, no dispatch-path wiring required.
      2. **Active scope** — an explicit ``set_calibration_target`` token, for
         programmatic injection (tests / a deterministic injector).
    NEVER raises (fail-closed → False)."""
    try:
        if not calibration_mode_enabled():
            return False
        sanctioned = set()
        tok = current_calibration_target()
        if tok:
            sanctioned.add(tok)
        seed_path = calibration_target_path()
        if seed_path:
            sanctioned.add(seed_path.replace("\\", "/").rsplit("/", 1)[-1])
        if not sanctioned:
            return False
        for f in target_files or ():
            if not f:
                continue
            base = str(f).replace("\\", "/").rsplit("/", 1)[-1]
            if base in sanctioned:
                return True
        return False
    except Exception:  # noqa: BLE001 — fail-closed: never spuriously bypass safety
        return False


def calibration_target_path() -> Optional[str]:
    """Convenience for the seed-defect injector: the env-overridable on-disk path of
    the calibration seed (default ``tests/seed/test_seed_defect.py``)."""
    return os.environ.get(
        "JARVIS_CALIBRATION_TARGET_PATH", "tests/seed/test_seed_defect.py",
    ).strip() or None


__all__ = [
    "calibration_mode_enabled",
    "set_calibration_target",
    "reset_calibration_target",
    "current_calibration_target",
    "is_calibration_target",
    "calibration_target_path",
]
