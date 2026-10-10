"""How many generations the serving engine says it runs at once.

Concurrency on the local lane was DERIVED: a VRAM estimate (the pool, sized
once at hydrate time, before the served model's size was even cached, so it
fell back to 1 and was never revisited), a node count ("one GPU = one lane"),
and a hardcoded ``Semaphore(1)`` on the J-Prime path. Each was a guess about
something the engine knows exactly.

J-Prime launches llama-server with ``--parallel N``: N slots, each with its own
pre-allocated KV for the full context. That allocation is physical, so the
engine cannot be over-subscribed the way an on-demand server was (2026-09-08:
four streams contending for one card all returned ``tokens=0``); a request
beyond N waits inside the engine. N is therefore the concurrency the lane can
serve, and J-Prime reports it per resident model in ``/api/ps`` (``parallel``).

This module is that reading, nothing else: a per-endpoint cache filled by an
async probe, and listeners told when a reading CHANGES, so the pool's lanes and
the client-side J-Prime limit follow the engine at runtime instead of being
fixed at boot. An engine that does not report slots (Ollama) reads as unknown,
and every consumer keeps its previous behaviour for an unknown.

Stdlib + aiohttp (already a dependency of the generator). NEVER raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.EngineSlots")

__all__ = ["add_listener", "known", "lanes_for", "record", "refresh", "remove_listener",
           "reset_for_tests", "slots"]

_lock = threading.Lock()
_SLOTS: Dict[str, Tuple[int, float]] = {}          # endpoint -> (slots, read_at)
_LISTENERS: List[Callable[[str, int], None]] = []


def _ttl_s() -> float:
    """``JARVIS_ENGINE_SLOTS_TTL_S``: how long a reading stands before the
    next probe re-reads it (default 60). An engine relaunched with a new
    slot count is picked up within one TTL."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_ENGINE_SLOTS_TTL_S", "") or 60.0))
    except ValueError:
        return 60.0


def _norm(endpoint: str) -> str:
    return str(endpoint or "").strip().rstrip("/")


def slots(endpoint: str) -> Optional[int]:
    """The last reading for *endpoint*, or None when unknown."""
    with _lock:
        held = _SLOTS.get(_norm(endpoint))
    return held[0] if held else None


def known() -> Dict[str, int]:
    """Every endpoint with a reading, and its slots."""
    with _lock:
        return {ep: n for ep, (n, _at) in _SLOTS.items()}


def lanes_for(endpoints: Iterable[str]) -> int:
    """Concurrent generations a set of serving endpoints can run: the sum of
    their slots, an unknown endpoint counting as one (the previous rule)."""
    total = 0
    for ep in endpoints:
        n = slots(ep)
        total += n if n else 1
    return max(1, total)


def add_listener(cb: Callable[[str, int], None]) -> None:
    with _lock:
        if cb not in _LISTENERS:
            _LISTENERS.append(cb)


def remove_listener(cb: Callable[[str, int], None]) -> None:
    with _lock:
        if cb in _LISTENERS:
            _LISTENERS.remove(cb)


def record(endpoint: str, n: int) -> bool:
    """Store a reading. Listeners run only when the value CHANGED. Returns
    whether it changed."""
    ep = _norm(endpoint)
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False
    if not ep or n < 1:
        return False
    with _lock:
        prev = _SLOTS.get(ep)
        _SLOTS[ep] = (n, time.monotonic())
        changed = prev is None or prev[0] != n
        listeners = list(_LISTENERS) if changed else []
    if changed:
        logger.info("[EngineSlots] %s serves %d concurrent generation(s)%s", ep, n,
                    "" if prev is None else f" (was {prev[0]})")
    for cb in listeners:
        try:
            cb(ep, n)
        except Exception:  # noqa: BLE001 — a listener never breaks the reading
            logger.debug("[EngineSlots] listener faulted", exc_info=True)
    return changed


def _parse(payload: Any) -> Optional[int]:
    """Slots of the PRIMARY resident model: the one holding the most VRAM
    (the same rule the residency reader uses). None when not reported."""
    best: Optional[Tuple[int, int]] = None
    for entry in (payload or {}).get("models", []) or []:
        if not isinstance(entry, dict) or "parallel" not in entry:
            continue
        try:
            vram, n = int(entry.get("size_vram", 0) or 0), int(entry["parallel"])
        except (TypeError, ValueError):
            continue
        if n >= 1 and (best is None or vram > best[0]):
            best = (vram, n)
    return best[1] if best else None


async def _fetch_ps(endpoint: str, timeout_s: float) -> Any:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as sess:
        async with sess.get(f"{_norm(endpoint)}/api/ps") as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)


async def refresh(endpoint: str, *, fetch: Optional[Callable[[str, float], Any]] = None,
                  force: bool = False, timeout_s: float = 3.0) -> Optional[int]:
    """Re-read *endpoint*'s slots unless a reading younger than the TTL
    stands. Returns the current reading. NEVER raises."""
    ep = _norm(endpoint)
    if not ep:
        return None
    with _lock:
        held = _SLOTS.get(ep)
    if held and not force and time.monotonic() - held[1] < _ttl_s():
        return held[0]
    try:
        payload = await (fetch or _fetch_ps)(ep, timeout_s)
        n = _parse(payload)
    except Exception:  # noqa: BLE001 — an unreachable engine is not a reading
        n = None
    if n is not None:
        record(ep, n)
        return n
    return held[0] if held else None


def reset_for_tests() -> None:
    with _lock:
        _SLOTS.clear()
        _LISTENERS.clear()
