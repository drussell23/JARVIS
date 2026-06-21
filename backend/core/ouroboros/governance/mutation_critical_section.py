"""Mutation critical-section guard (spec 5.4 / LR-B).

The operator-yield must never park an op while a critical state mutation
(write_file/edit_file/ChangeEngine.execute/git commit) is in flight, or the FSM
would rehydrate from a half-applied checkpoint. This is a per-op re-entrant
async section + a drain() the yield path awaits before parking. Pure stdlib +
asyncio. Never raises out of the public API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Dict

logger = logging.getLogger(__name__)

_counts: Dict[str, int] = {}

_TRUTHY = ("1", "true", "yes", "on")


def is_mutating(op_id: str) -> bool:
    """True iff op_id is inside >=1 active mutation section. Never raises."""
    try:
        return _counts.get(str(op_id), 0) > 0
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def mutation_section(op_id: str):
    """Re-entrant per-op critical section around a state mutation. Increments on
    enter, decrements on exit (even on exception). Never swallows the body's
    exception."""
    key = str(op_id)
    _counts[key] = _counts.get(key, 0) + 1
    try:
        yield
    finally:
        n = _counts.get(key, 0) - 1
        if n <= 0:
            _counts.pop(key, None)
        else:
            _counts[key] = n


@asynccontextmanager
async def maybe_mutation_section(op_id: str):
    """Gated wrapper around mutation_section. When JARVIS_OPERATOR_YIELD_ENABLED
    is on, enters a real mutation section; otherwise a cheap no-op so the
    instrumented call site is byte-identical in behavior with the feature off."""
    enabled = (os.environ.get("JARVIS_OPERATOR_YIELD_ENABLED", "false") or "").strip().lower() in _TRUTHY
    if enabled:
        async with mutation_section(op_id):
            yield
    else:
        yield


async def drain(op_id: str, timeout: float, poll_s: float = 0.02) -> bool:
    """Wait until op_id has no active mutation section, up to `timeout` seconds.
    Returns True if it drained (safe to park), False if it wedged past the
    timeout (caller must ABANDON the yield — never park a half-applied op).
    Never raises."""
    key = str(op_id)
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while is_mutating(key):
            if loop.time() >= deadline:
                logger.warning(
                    "[MutationSection] drain abandoned op=%s (wedged > %.2fs)", key, timeout
                )
                return False
            await asyncio.sleep(poll_s)
        return True
    except Exception:  # noqa: BLE001 — drain must not crash the yield path
        return True  # fail-open to "drained" only if our own bookkeeping errors
