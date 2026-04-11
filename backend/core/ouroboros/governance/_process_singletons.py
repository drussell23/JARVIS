"""Process-wide async primitives that must survive module hot-reload.

This module is intentionally QUARANTINED from `ModuleHotReloader` — its
objects back long-lived locks, semaphores, and registries that other
reloadable modules share across reload boundaries.

Why this exists:
  Reloading a module re-executes its top-level code. Any
  `asyncio.Semaphore`, `asyncio.Lock`, or `threading.Lock` defined at
  module level becomes a NEW object, orphaning any tasks waiting on the
  old one. To safely hot-reload e.g. `patch_benchmarker.py`, the
  benchmark concurrency semaphore must live OUTSIDE that module — here.

How to add a new shared primitive:
  1. Define a new `get_*` function below following the pattern.
  2. Reference it via deferred import inside the consumer module
     (`from ._process_singletons import get_semaphore`).
  3. Confirm this module name is in `module_hot_reloader.DEFAULT_QUARANTINE`.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Dict

_lock = threading.Lock()
_semaphores: Dict[str, asyncio.Semaphore] = {}


def get_semaphore(key: str, value: int) -> asyncio.Semaphore:
    """Return a process-wide asyncio.Semaphore by key, creating on first call.

    Subsequent calls with the same key return the SAME semaphore instance
    regardless of how many times the calling module has been reloaded.
    The `value` argument is honored only on first creation; later calls
    return the existing semaphore unchanged.
    """
    with _lock:
        sem = _semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(value)
            _semaphores[key] = sem
        return sem


def reset_for_test() -> None:
    """Test-only: clear all process-wide singletons."""
    with _lock:
        _semaphores.clear()
