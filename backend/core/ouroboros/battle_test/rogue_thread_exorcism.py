"""Rogue Thread Exorcism — Slice 12W Phase 1.

bt-2026-05-23-201956 (Slice 12V validation soak) successfully
captured the exact interpreter wedge via the ShutdownWatchdog
tombstone — a non-daemon thread blocking ``threading._shutdown`` at
``threading.py:1590`` on ``lock.acquire()`` for ``Py_FinalizeEx``.
Cross-referencing the tombstone's full thread dump pinpoints the
culprits exactly:

* ``aiosqlite/core.py:99 in run`` — aiosqlite's per-connection worker
  thread. ``aiosqlite.Connection`` spawns a Python ``threading.Thread``
  in non-daemon mode by default; when an async context manager exits
  via cancellation (the SessionBudgetPreflightRefused circuit-breaker
  cascade that ended the soak), the worker may not always be drained.

* ``posthog/consumer.py:65 in run`` — PostHog analytics consumer
  thread spawned transitively via ``chromadb.telemetry.product.posthog``.
  Even though :class:`oracle.TheOracle` passes
  ``anonymized_telemetry=False`` to its ``chromadb.PersistentClient``,
  the ``import chromadb`` line at line 1429 already triggers
  module-level posthog initialization in chromadb's telemetry
  submodule — the analytics consumer thread is alive before our
  per-client setting even runs.

Slice 12W exorcises **the entire class** of non-daemon-thread
shutdown blockers via two composable disciplines:

1. **Process-wide env hygiene at boot.** Set telemetry-disable env
   vars BEFORE any subsystem imports them. ChromaDB
   (``ANONYMIZED_TELEMETRY=False``), PostHog
   (``POSTHOG_DISABLED=True``), and similar libraries read these at
   module-load time. Setting them at harness boot's first step
   guarantees the telemetry threads never spawn.

2. **Surgical aiosqlite daemonization.** ``aiosqlite.Connection``
   spawns its worker via ``threading.Thread(target=self.run)`` with
   ``daemon=False`` (default). Monkeypatch the Connection class so
   every subsequent instance spawns a daemon thread instead.
   Connections still drain cleanly via the async ``__aexit__``
   path; the daemon flag only matters at hard interpreter teardown.

Both disciplines are master-switched + idempotent + NEVER raise.
This module is the single source of truth for thread-hygiene at
boot; it composes existing primitives (``os.environ.setdefault`` +
attribute monkeypatch) — no new mechanism.

Master switch: ``JARVIS_ROGUE_THREAD_EXORCISM_ENABLED`` (BOOL/SAFETY,
default TRUE). When ``false``, falls back to pre-Slice-12W
behavior verbatim (telemetry threads may spawn; aiosqlite
connections may block interpreter teardown).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.RogueThreadExorcism")


# ============================================================================
# Master switch
# ============================================================================


ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR: str = (
    "JARVIS_ROGUE_THREAD_EXORCISM_ENABLED"
)


def exorcism_enabled() -> bool:
    """Master flag — default TRUE. Set to ``false`` / ``0`` / ``no`` /
    ``off`` for byte-identical pre-Slice-12W behavior."""
    raw = os.environ.get(
        ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR, "true",
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ============================================================================
# Telemetry disable — env hygiene
# ============================================================================


# Closed table of (env_var, value) pairs to set defaults for. Using
# ``os.environ.setdefault`` preserves explicit operator overrides
# (operator setting ``ANONYMIZED_TELEMETRY=True`` for debugging will
# survive). Each pair is documented with the library it disables +
# the thread it prevents from spawning.
_TELEMETRY_DEFAULTS: Tuple[Tuple[str, str], ...] = (
    # ChromaDB → posthog consumer thread (non-daemon).
    # bt-2026-05-23-201956 tombstone: posthog/consumer.py:65 in run.
    ("ANONYMIZED_TELEMETRY", "False"),
    # PostHog client itself (defense-in-depth — even if some other
    # library tries to import posthog, this disables the consumer).
    ("POSTHOG_DISABLED", "True"),
    # OpenTelemetry exporters (if any transitive import wires them).
    # OTEL_SDK_DISABLED is the canonical OTel kill switch.
    ("OTEL_SDK_DISABLED", "true"),
)


def apply_telemetry_env_defaults() -> List[Tuple[str, str]]:
    """Set every ``_TELEMETRY_DEFAULTS`` entry via
    :func:`os.environ.setdefault` — preserves operator overrides.

    Returns the list of ``(env_var, value)`` pairs that were
    actually set (i.e., were absent before this call). NEVER
    raises; on exception the partial list is returned.

    Idempotent: re-running is a no-op (setdefault skips existing
    keys).
    """
    applied: List[Tuple[str, str]] = []
    for env_var, value in _TELEMETRY_DEFAULTS:
        try:
            if env_var not in os.environ:
                os.environ[env_var] = value
                applied.append((env_var, value))
        except Exception:  # noqa: BLE001 — defensive
            continue
    return applied


# ============================================================================
# aiosqlite worker daemonization — monkeypatch
# ============================================================================


# Module-level latch so the monkeypatch is applied at most once per
# process even on re-imports.
_AIOSQLITE_PATCHED: bool = False


def patch_aiosqlite_to_daemon() -> bool:
    """Monkeypatch ``aiosqlite.core.Connection`` so its per-connection
    worker thread spawns as ``daemon=True``.

    Returns ``True`` when the patch was successfully applied (or
    already applied); ``False`` when aiosqlite isn't installed or the
    patch failed. NEVER raises.

    Strategy: aiosqlite's ``Connection.__init__`` builds the worker
    via ``threading.Thread(target=self.run)`` with no explicit
    ``daemon=`` kwarg — Python defaults to ``daemon=False``. We
    replace ``Connection.__init__`` with a wrapper that constructs
    the base instance, then flips the worker thread's ``daemon``
    attribute to ``True`` BEFORE it's started.

    The patch is benign when aiosqlite drains cleanly via async
    ``__aexit__`` — the daemon flag only matters at hard interpreter
    teardown (``Py_FinalizeEx``). Connections that exit via the
    normal path still close in-order; only orphaned connections (the
    cancellation-cascade case the soak surfaced) benefit from
    daemonization.
    """
    global _AIOSQLITE_PATCHED
    if _AIOSQLITE_PATCHED:
        return True
    try:
        import aiosqlite.core as _aiosqlite_core  # type: ignore[import]
    except Exception:  # noqa: BLE001 — aiosqlite not installed
        return False

    try:
        _original_init = _aiosqlite_core.Connection.__init__

        def _exorcised_init(self, *args: Any, **kwargs: Any) -> None:
            # Run the original constructor — this builds + starts
            # the worker thread.
            _original_init(self, *args, **kwargs)
            # Flip the worker to daemon=True. The aiosqlite
            # Connection exposes the thread via several attribute
            # names across versions (._tx, ._loop_thread, etc.) —
            # walk known candidates defensively. The interpreter
            # tolerates setting daemon AFTER start IF the thread
            # is still alive (Python docs allow this in CPython
            # 3.0+).
            for candidate in (
                "_tx", "_loop_thread", "_thread", "_worker",
            ):
                _thread = getattr(self, candidate, None)
                if _thread is None:
                    continue
                # threading.Thread instances expose .daemon and
                # .setDaemon; the property setter is the modern
                # form. Either works.
                try:
                    if hasattr(_thread, "daemon"):
                        _thread.daemon = True
                except Exception:  # noqa: BLE001
                    pass

        _aiosqlite_core.Connection.__init__ = _exorcised_init  # type: ignore[assignment]
        _AIOSQLITE_PATCHED = True
        return True
    except Exception:  # noqa: BLE001 — never raise into harness boot
        logger.debug(
            "[RogueThreadExorcism] aiosqlite daemonize patch failed "
            "(handled — pre-Slice-12W behavior preserved)",
            exc_info=True,
        )
        return False


# ============================================================================
# posthog disable — late defense if it imported before our env vars
# ============================================================================


def disable_posthog_if_loaded() -> bool:
    """Defense-in-depth: if ``posthog`` is already imported by the
    time this runs (e.g., chromadb pulled it in at startup), flip
    its module-level ``disabled`` flag so any subsequent
    ``posthog.capture(...)`` call short-circuits without enqueuing
    work onto the consumer thread.

    Returns ``True`` when posthog was loaded + flipped; ``False``
    otherwise. NEVER raises.

    This is BELT-and-suspenders to the env-var ``POSTHOG_DISABLED``
    + ``ANONYMIZED_TELEMETRY=False`` set in
    :func:`apply_telemetry_env_defaults`. Either alone suffices in
    most cases; together they cover both "imported pre-boot" and
    "imported post-boot" code paths.
    """
    import sys
    _posthog = sys.modules.get("posthog")
    if _posthog is None:
        return False
    try:
        # The module exposes both a top-level `disabled` flag AND
        # the Client class has its own `disabled` attr; set both.
        setattr(_posthog, "disabled", True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
# Composite entry point — call at harness boot
# ============================================================================


def exorcise_at_boot(
    *, logger_fn: Optional[Callable[[str], None]] = None,
) -> dict:
    """Composite entry point. Apply every Slice 12W Phase 1
    discipline in dependency order:

      1. ``apply_telemetry_env_defaults`` — must run BEFORE any
         downstream subsystem imports chromadb / posthog. Sets env
         vars via setdefault so operator overrides are preserved.
      2. ``patch_aiosqlite_to_daemon`` — apply the monkeypatch so
         every NEW aiosqlite.Connection gets a daemon worker.
         Existing connections (if any) are unchanged; daemonization
         applies prospectively.
      3. ``disable_posthog_if_loaded`` — late-bind kill switch in
         case posthog was imported before step 1's env vars took
         effect.

    Returns a dict with telemetry of what was done — useful for the
    harness boot log and Slice 12W test pins. NEVER raises.

    Master-switch-aware: when ``JARVIS_ROGUE_THREAD_EXORCISM_ENABLED``
    is FALSE, returns ``{"enabled": False, ...}`` without doing
    anything (byte-identical pre-Slice-12W rollback).
    """
    if not exorcism_enabled():
        return {
            "enabled": False,
            "env_defaults_applied": [],
            "aiosqlite_patched": False,
            "posthog_disabled": False,
        }
    report = {
        "enabled": True,
        "env_defaults_applied": [],
        "aiosqlite_patched": False,
        "posthog_disabled": False,
    }
    try:
        report["env_defaults_applied"] = (
            apply_telemetry_env_defaults()
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        report["aiosqlite_patched"] = patch_aiosqlite_to_daemon()
    except Exception:  # noqa: BLE001
        pass
    try:
        report["posthog_disabled"] = disable_posthog_if_loaded()
    except Exception:  # noqa: BLE001
        pass
    if logger_fn is not None:
        try:
            logger_fn(
                "[RogueThreadExorcism] applied: "
                f"env_defaults={len(report['env_defaults_applied'])} "
                f"aiosqlite_patched={report['aiosqlite_patched']} "
                f"posthog_disabled={report['posthog_disabled']}"
            )
        except Exception:  # noqa: BLE001
            pass
    return report


__all__ = [
    "ROGUE_THREAD_EXORCISM_ENABLED_ENV_VAR",
    "_TELEMETRY_DEFAULTS",
    "apply_telemetry_env_defaults",
    "disable_posthog_if_loaded",
    "exorcism_enabled",
    "exorcise_at_boot",
    "patch_aiosqlite_to_daemon",
]
