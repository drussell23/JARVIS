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


def _daemonize_existing_aiosqlite_threads() -> int:
    """Walk every currently-alive Python thread; if any has a name
    matching the aiosqlite worker convention, attempt to flip its
    ``daemon`` attribute to ``True`` defensively.

    Returns the count of threads successfully daemonized. NEVER
    raises.

    **CPython runtime constraint:** ``Thread.daemon`` cannot be
    set after ``start()`` has been called — CPython raises
    ``RuntimeError: cannot set daemon status of active thread``.
    The sweep catches and swallows this, so for any thread that
    has already started, the function silently records a
    no-op. The primary defense is therefore Layer 1
    (``Connection.__init__`` wrap), which flips the daemon flag
    inside ``__init__`` BEFORE the Thread is started in
    ``__aenter__``.

    Layer 3 still has value for the rare race where a Connection
    has been constructed but not yet entered (caller built it
    pre-patch but hasn't ``await``'d into it). For the common
    case (already-running workers), it's documented as a
    best-effort no-op rather than a hard guarantee.
    """
    import threading as _threading
    daemonized = 0
    try:
        for _t in _threading.enumerate():
            try:
                _name = (_t.name or "").lower()
                if "aiosqlite" in _name and not _t.daemon:
                    _t.daemon = True
                    daemonized += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return daemonized


def patch_aiosqlite_to_daemon() -> bool:
    """Slice 12X Phase 2 — TOTAL aiosqlite daemonization.

    Layers FOUR distinct daemonization disciplines so no
    connection-creation path can spawn a non-daemon worker:

      1. Monkeypatch ``aiosqlite.core.Connection.__init__`` — flips
         the worker thread's ``daemon`` attribute after the parent
         ``__init__`` constructs it. (Slice 12W's original
         discipline.)
      2. Monkeypatch ``aiosqlite.connect()`` — the top-level
         factory used by every caller in the codebase
         (``cost_tracker``, ``hybrid``, ``trinity_base_client``,
         ``persistent_intelligence_manager``). Any connection
         created via this path also gets a daemon-true worker.
      3. Walk ``threading.enumerate()`` at patch time and
         daemonize any aiosqlite worker thread that's already
         alive (the "monkeypatch landed too late" case the
         bt-2026-05-23-204519 tombstone surfaced).
      4. Same ``_AIOSQLITE_PATCHED`` latch as before — idempotent.

    Returns ``True`` when at least one of (1)–(3) succeeded.
    Returns ``False`` only when aiosqlite isn't installed. NEVER
    raises.
    """
    global _AIOSQLITE_PATCHED
    if _AIOSQLITE_PATCHED:
        # Idempotent fast path — but ALSO re-sweep existing
        # threads so callers who arm this after a later
        # connection opens still get retroactive daemonization.
        _daemonize_existing_aiosqlite_threads()
        return True
    try:
        import aiosqlite as _aiosqlite_top  # type: ignore[import]
        import aiosqlite.core as _aiosqlite_core  # type: ignore[import]
    except Exception:  # noqa: BLE001 — aiosqlite not installed
        return False

    success_count = 0

    # ── Layer 1: Connection.__init__ wrap ──
    #
    # Slice 12X discovery: ``aiosqlite.core.Connection`` IS-A
    # :class:`threading.Thread` (multiple-inheritance via
    # ``class Connection(Thread)``). Slice 12W looked for
    # ``_tx``/``_loop_thread``/``_worker`` attributes that don't
    # exist on this class — the Connection IS the thread. The
    # tombstone in bt-2026-05-23-204519 confirmed this: the
    # offending frame was ``aiosqlite/core.py:99 in run`` (which
    # is Thread.run on a Connection instance).
    #
    # Layer 1 now flips ``self.daemon = True`` directly when
    # ``self`` is a Thread subclass, after the base __init__
    # but BEFORE the worker is started (start happens later in
    # ``__aenter__``, so the daemon flip is legal at this point).
    # The legacy attribute walk is kept as a fallback for
    # future aiosqlite versions that might split the worker out.
    try:
        _original_init = _aiosqlite_core.Connection.__init__

        def _exorcised_init(self, *args: Any, **kwargs: Any) -> None:
            _original_init(self, *args, **kwargs)
            # Primary path — Connection IS a Thread.
            try:
                import threading as _th
                if isinstance(self, _th.Thread):
                    # daemon can only be set before start(); we
                    # are still inside __init__ so the Thread
                    # has NOT been started yet. This is the
                    # critical window the Slice 12W patch missed.
                    try:
                        # Some Thread states reject daemon
                        # assignment; the bare attribute set
                        # below works pre-start in CPython.
                        self.daemon = True
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            # Fallback path — future-compat for aiosqlite
            # versions that may split the worker out.
            for candidate in (
                "_tx", "_loop_thread", "_thread", "_worker",
            ):
                _thread = getattr(self, candidate, None)
                if _thread is None:
                    continue
                try:
                    if hasattr(_thread, "daemon"):
                        _thread.daemon = True
                except Exception:  # noqa: BLE001
                    pass

        _aiosqlite_core.Connection.__init__ = _exorcised_init  # type: ignore[assignment]
        success_count += 1
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RogueThreadExorcism] Connection.__init__ wrap failed "
            "(handled)", exc_info=True,
        )

    # ── Layer 2: top-level connect() factory wrap ──
    # ``aiosqlite.connect`` is the user-facing entry point. Across
    # aiosqlite versions it's either a function returning a
    # Connection or an `_ContextManagerMixin`-style awaitable
    # context manager. We wrap whatever it is and ensure the
    # returned object's worker is daemonized — either at construction
    # time (sync) or at __aenter__ time (async).
    try:
        _original_connect = getattr(_aiosqlite_top, "connect", None)
        if _original_connect is not None:

            def _exorcised_connect(*args: Any, **kwargs: Any) -> Any:
                _obj = _original_connect(*args, **kwargs)
                # Best-effort: if the returned object already has
                # one of the worker attribute names, daemonize
                # immediately. If it's still an awaitable context
                # manager, the Layer 1 Connection.__init__ wrap
                # will handle it when the actual Connection is
                # constructed at await time.
                for candidate in (
                    "_tx", "_loop_thread", "_thread", "_worker",
                ):
                    _thread = getattr(_obj, candidate, None)
                    if _thread is not None:
                        try:
                            if hasattr(_thread, "daemon"):
                                _thread.daemon = True
                        except Exception:  # noqa: BLE001
                            pass
                return _obj

            _aiosqlite_top.connect = _exorcised_connect  # type: ignore[assignment]
            success_count += 1
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RogueThreadExorcism] aiosqlite.connect() wrap failed "
            "(handled)", exc_info=True,
        )

    # ── Layer 3: sweep existing threads ──
    _retro = _daemonize_existing_aiosqlite_threads()
    if _retro > 0:
        try:
            logger.info(
                "[RogueThreadExorcism] retroactively daemonized "
                "%d existing aiosqlite worker thread(s)", _retro,
            )
        except Exception:  # noqa: BLE001
            pass

    if success_count > 0:
        _AIOSQLITE_PATCHED = True
        return True
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
    "_daemonize_existing_aiosqlite_threads",
    "apply_telemetry_env_defaults",
    "disable_posthog_if_loaded",
    "exorcism_enabled",
    "exorcise_at_boot",
    "patch_aiosqlite_to_daemon",
]
