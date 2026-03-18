"""backend/core/objc_safe_preloader.py — Nuance 12: ObjC import deadlock prevention.

Problem
-------
Importing PyObjC-backed modules (``Quartz``, ``Foundation``, ``AppKit``, etc.)
triggers tens of thousands of ObjC runtime class registrations.  If a signal
fires while one of these registrations holds the ObjC global runtime lock, and
the signal handler itself calls into ObjC-using code (e.g., CoreAudio via
``sounddevice``), the signal handler blocks forever — a classic lock-order
inversion deadlock on macOS.

With parallel component initialisation, any component that imports an ObjC
module for the first time is a probabilistic deadlock risk on every startup.

Fix
---
Pre-import all ObjC modules **synchronously** on the **main thread** before
parallel init starts and before signal handlers are registered.  After
``preload_objc_modules()`` returns, the ObjC runtime has completed all class
registrations for those modules.  Subsequent re-imports are no-ops (Python
module cache hit — no ObjC work, no lock held).

Safety requirements
-------------------
1. Must be called from the **main thread** (verified at call time).
2. Must be called **before** ``asyncio.create_task()`` / ``asyncio.gather()``
   begins any parallel component init.
3. Must be called **before** signal handlers that call ObjC-using code are
   registered.
4. Idempotent — second call is a no-op (guarded by a ``threading.Lock``).

Configuration
-------------
The list of modules to preload is read from:
  ``JARVIS_OBJC_PRELOAD_MODULES``  (comma-separated, e.g. ``Quartz,Foundation``)

Defaults to ``["Quartz", "Foundation", "AppKit"]`` when the env var is absent.
Timeout per module is read from ``JARVIS_OBJC_PRELOAD_TIMEOUT_S`` (default 10 s).
"""
from __future__ import annotations

import importlib
import logging
import os
import platform
import signal
import sys
import threading
from typing import Dict, List, Optional

__all__ = [
    "preload_objc_modules",
    "is_preloaded",
    "ObjcPreloadResult",
]

logger = logging.getLogger(__name__)

_PRELOAD_LOCK = threading.Lock()
_PRELOADED: bool = False

_DEFAULT_MODULES: List[str] = ["Quartz", "Foundation", "AppKit"]
_ENV_MODULES_KEY = "JARVIS_OBJC_PRELOAD_MODULES"
_ENV_TIMEOUT_KEY = "JARVIS_OBJC_PRELOAD_TIMEOUT_S"


def _resolve_modules() -> List[str]:
    raw = os.getenv(_ENV_MODULES_KEY)
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(_DEFAULT_MODULES)


def _resolve_timeout() -> float:
    raw = os.getenv(_ENV_TIMEOUT_KEY)
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return 10.0


class ObjcPreloadResult:
    """Result of a ``preload_objc_modules()`` call."""

    def __init__(self, results: Dict[str, bool], skipped: bool) -> None:
        self.results = results          # {module_name: import_succeeded}
        self.skipped = skipped          # True if this call was a no-op (already preloaded)

    @property
    def all_succeeded(self) -> bool:
        return all(self.results.values())

    @property
    def failed_modules(self) -> List[str]:
        return [m for m, ok in self.results.items() if not ok]

    def __repr__(self) -> str:
        if self.skipped:
            return "ObjcPreloadResult(skipped=True)"
        ok = sum(self.results.values())
        total = len(self.results)
        return f"ObjcPreloadResult({ok}/{total} ok, failed={self.failed_modules})"


def _assert_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "preload_objc_modules() must be called from the main thread "
            "before parallel init starts.  "
            f"Current thread: {threading.current_thread().name!r}"
        )


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def preload_objc_modules(
    modules: Optional[List[str]] = None,
    timeout_s: Optional[float] = None,
) -> ObjcPreloadResult:
    """Pre-import ObjC modules synchronously on the main thread.

    Parameters
    ----------
    modules:
        List of module names to pre-import.  ``None`` → read from
        ``JARVIS_OBJC_PRELOAD_MODULES`` env or use ``["Quartz","Foundation","AppKit"]``.
    timeout_s:
        Seconds to allow each import.  ``None`` → ``JARVIS_OBJC_PRELOAD_TIMEOUT_S``
        or 10 s.  If a module takes longer than this, a ``SIGALRM`` fires (macOS
        only) and the import is abandoned.

    Returns
    -------
    ObjcPreloadResult
        Per-module success/failure mapping.

    Raises
    ------
    RuntimeError
        If called from a non-main thread.
    """
    global _PRELOADED

    _assert_main_thread()

    if not _is_macos():
        logger.debug("[ObjcPreloader] not macOS — skipping ObjC preload")
        return ObjcPreloadResult({}, skipped=True)

    with _PRELOAD_LOCK:
        if _PRELOADED:
            return ObjcPreloadResult({}, skipped=True)

        target_modules = modules if modules is not None else _resolve_modules()
        timeout = timeout_s if timeout_s is not None else _resolve_timeout()
        results: Dict[str, bool] = {}

        logger.info(
            "[ObjcPreloader] pre-importing %d ObjC module(s) on main thread: %s",
            len(target_modules), target_modules,
        )

        for mod_name in target_modules:
            if mod_name in sys.modules:
                logger.debug("[ObjcPreloader] '%s' already imported — skip", mod_name)
                results[mod_name] = True
                continue

            # Use SIGALRM to enforce per-module timeout (macOS only).
            timed_out = False
            original_handler = None

            if hasattr(signal, "SIGALRM"):
                def _alarm_handler(*_args):  # noqa: ANN001
                    nonlocal timed_out
                    timed_out = True
                original_handler = signal.signal(signal.SIGALRM, _alarm_handler)
                signal.alarm(max(1, int(timeout)))

            try:
                importlib.import_module(mod_name)
                results[mod_name] = True
                logger.info("[ObjcPreloader] '%s' imported successfully", mod_name)
            except ImportError as exc:
                results[mod_name] = False
                logger.debug(
                    "[ObjcPreloader] '%s' not available (ImportError: %s)", mod_name, exc
                )
            except Exception as exc:
                results[mod_name] = False
                logger.warning("[ObjcPreloader] '%s' import raised: %s", mod_name, exc)
            finally:
                if hasattr(signal, "SIGALRM"):
                    signal.alarm(0)  # cancel pending alarm
                    if original_handler is not None:
                        signal.signal(signal.SIGALRM, original_handler)

            if timed_out:
                results[mod_name] = False
                logger.error(
                    "[ObjcPreloader] '%s' import timed out after %.0fs",
                    mod_name, timeout,
                )

        _PRELOADED = True
        result = ObjcPreloadResult(results, skipped=False)
        logger.info("[ObjcPreloader] complete: %s", result)
        return result


def is_preloaded() -> bool:
    """Return ``True`` if ``preload_objc_modules()`` has been called."""
    return _PRELOADED
