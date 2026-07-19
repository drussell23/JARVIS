"""Performance & Preference Ledger — API-era optimization substrate.

The final integration piece (operator authorization 2026-07-19). It is
"RLHF-by-self-modification" made durable: implicit outcomes accumulate,
noise is attenuated, and highly-rated strategies bias future
generation — WITHOUT touching model weights.

Mandate 1 — no DB engine, no synchronous disk loop: an in-memory,
thread-safe structured store that serializes ASYNCHRONOUSLY (debounced
off-loop task) to a bounded local ``.json``.

**Multi-Variable Attenuation (mandate 2 — the Environmental Noise
Guard):** a QoS frustration trigger alone is NOT a failure — typos,
network blips, and stray interrupts fire it too. A path is a
DEFINITIVE negative ONLY when frustration fires AND the user overrides
immediately. Conversely a downstream ``exit_code==0`` OR a post-
response idle beyond ``JARVIS_LEDGER_IDLE_SUCCESS_S`` (45s)
ATTENUATES the frustration and records an implicit SUCCESS — the human
moved on satisfied. Net score per path is the attenuated aggregate.

DRY (mandate 3): consumes the EXISTING ``UX_DEGRADATION_EVENT``
envelope evidence (``cause`` / ``dialogue_context``) — no second
telemetry wrapper.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.PreferenceLedger")


def _idle_success_s() -> float:
    try:
        return max(10.0, min(600.0, float(os.environ.get(
            "JARVIS_LEDGER_IDLE_SUCCESS_S", "45",
        ))))
    except (TypeError, ValueError):
        return 45.0


def _ledger_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_PREFERENCE_LEDGER", ".jarvis/preference_ledger.json",
    ))


def _max_paths() -> int:
    try:
        return max(16, int(os.environ.get("JARVIS_LEDGER_MAX_PATHS", "500")))
    except (TypeError, ValueError):
        return 500


class _PathRecord:
    __slots__ = ("key", "strategy", "successes", "frustrations",
                 "definitive_negatives", "attenuated", "score", "updated_at")

    def __init__(self, key: str, strategy: str) -> None:
        self.key = key
        self.strategy = strategy
        self.successes = 0
        self.frustrations = 0
        self.definitive_negatives = 0
        self.attenuated = 0
        self.score = 0.0
        self.updated_at = 0.0

    def recompute(self) -> None:
        # Net attenuated score: successes lift, definitive negatives
        # sink; a lone frustration that was later attenuated does NOT.
        total = self.successes + self.definitive_negatives
        if total <= 0:
            self.score = 0.0
            return
        self.score = round(
            (self.successes - self.definitive_negatives) / max(1, total), 4,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "strategy": self.strategy,
            "successes": self.successes, "frustrations": self.frustrations,
            "definitive_negatives": self.definitive_negatives,
            "attenuated": self.attenuated, "score": self.score,
            "updated_at": self.updated_at,
        }


class PreferenceLedger:
    """The in-memory attenuation store. ``clock`` injected for tests;
    NEVER raises on any public path."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 path: Optional[Path] = None) -> None:
        self._clock = clock
        self._path = path or _ledger_path()
        self._lock = threading.RLock()
        self._paths: Dict[str, _PathRecord] = {}
        #: a frustration awaiting resolution (attenuate or confirm).
        self._pending: Dict[str, float] = {}      # key → frustration ts
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self.stats: Dict[str, int] = {
            "frustrations_seen": 0, "definitive_negatives": 0,
            "attenuated": 0, "successes": 0,
        }

    def _rec(self, key: str, strategy: str) -> _PathRecord:
        r = self._paths.get(key)
        if r is None:
            if len(self._paths) >= _max_paths():
                # evict the least-recently-updated
                oldest = min(self._paths.values(), key=lambda x: x.updated_at)
                self._paths.pop(oldest.key, None)
            r = _PathRecord(key, strategy)
            self._paths[key] = r
        return r

    # ---- the attenuation state machine ----

    def record_frustration(self, envelope: Any) -> None:
        """A UX_DEGRADATION_EVENT fired. DRY: read the existing
        envelope evidence. This is PENDING — not yet a negative; it
        awaits an override (confirm) or a success/idle (attenuate).
        NEVER raises."""
        try:
            ev = getattr(envelope, "evidence", None) or {}
            ctx = ev.get("dialogue_context") or []
            key = _strategy_key(ctx)
            with self._lock:
                self.stats["frustrations_seen"] += 1
                self._rec(key, _strategy_label(ctx)).frustrations += 1
                self._pending[key] = self._clock()
                self._mark_dirty()
        except Exception:  # noqa: BLE001
            pass

    def confirm_override(self, envelope: Any = None) -> None:
        """The user OVERRODE immediately after frustration (SIGINT /
        lease seizure) → the most-recent pending frustration is a
        DEFINITIVE negative. NEVER raises."""
        try:
            with self._lock:
                if not self._pending:
                    return
                # newest pending frustration is the one being overridden
                key = max(self._pending, key=lambda k: self._pending[k])
                self._pending.pop(key, None)
                r = self._paths.get(key)
                if r is not None:
                    r.definitive_negatives += 1
                    r.updated_at = self._clock()
                    r.recompute()
                    self.stats["definitive_negatives"] += 1
                    self._mark_dirty()
        except Exception:  # noqa: BLE001
            pass

    def record_exit_code(self, exit_code: int, context: Any = None) -> None:
        """A downstream terminal command returned. exit 0 ATTENUATES
        any pending frustration and books an implicit SUCCESS (the work
        actually worked). NEVER raises."""
        try:
            if int(exit_code) != 0:
                return
            with self._lock:
                self._attenuate_and_succeed(context)
        except Exception:  # noqa: BLE001
            pass

    def note_idle(self, idle_seconds: float, context: Any = None) -> None:
        """Post-response idle beyond the threshold = the human moved on
        satisfied → attenuate + implicit success. NEVER raises."""
        try:
            if float(idle_seconds) < _idle_success_s():
                return
            with self._lock:
                self._attenuate_and_succeed(context)
        except Exception:  # noqa: BLE001
            pass

    def _attenuate_and_succeed(self, context: Any) -> None:
        ctx = context if isinstance(context, list) else []
        key = _strategy_key(ctx) if ctx else (
            max(self._pending, key=lambda k: self._pending[k])
            if self._pending else None
        )
        if key is None:
            return
        r = self._rec(key, _strategy_label(ctx))
        # If a frustration was pending on this path, attenuate it.
        if key in self._pending:
            self._pending.pop(key, None)
            r.attenuated += 1
            self.stats["attenuated"] += 1
        r.successes += 1
        r.updated_at = self._clock()
        r.recompute()
        self.stats["successes"] += 1
        self._mark_dirty()

    # ---- scaffolding bias (the read side) ----

    def top_strategies(self, n: int = 3, *, min_score: float = 0.3) -> List[str]:
        """Highly-rated verified strategies for prompt injection.
        NEVER raises."""
        try:
            with self._lock:
                ranked = sorted(
                    (r for r in self._paths.values()
                     if r.score >= min_score and r.successes > 0),
                    key=lambda r: (r.score, r.successes), reverse=True,
                )
                return [r.strategy for r in ranked[:max(1, n)]]
        except Exception:  # noqa: BLE001
            return []

    def format_for_prompt(self) -> str:
        """The Dynamic Scaffolding Bias block for CONTEXT_EXPANSION.
        Empty when nothing is proven yet. NEVER raises."""
        try:
            strategies = self.top_strategies()
            if not strategies:
                return ""
            lines = "\n".join(f"  - {s}" for s in strategies)
            return (
                "## Verified Operational Patterns (preference ledger)\n"
                "These strategies have empirically satisfied the operator "
                "— bias toward them:\n" + lines
            )
        except Exception:  # noqa: BLE001
            return ""

    # ---- async serialization (mandate 1) ----

    def _mark_dirty(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        try:
            await asyncio.sleep(2.0)             # debounce burst writes
            await asyncio.to_thread(self._write_sync)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Ledger] flush degraded", exc_info=True)

    def _write_sync(self) -> None:
        try:
            with self._lock:
                if not self._dirty:
                    return
                payload = {
                    "schema_version": "preference_ledger.1",
                    "paths": [r.to_dict() for r in self._paths.values()],
                    "stats": dict(self.stats),
                }
                self._dirty = False
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")))
            os.replace(tmp, self._path)         # atomic
        except Exception:  # noqa: BLE001
            logger.debug("[Ledger] write degraded", exc_info=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "paths": len(self._paths),
                "pending": len(self._pending),
                "stats": dict(self.stats),
            }


def _strategy_key(context: List[str]) -> str:
    import hashlib
    try:
        # The strategy IS the dialogue trajectory that produced the
        # outcome — keyed on its shape, not verbatim text.
        joined = " | ".join(str(c) for c in (context or [])[-4:])
        return hashlib.sha256(joined.encode()).hexdigest()[:16] or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _strategy_label(context: List[str]) -> str:
    try:
        for c in reversed(context or []):
            s = str(c)
            if s.lower().startswith(("daniel", "karen", "assistant")):
                return s[:120]
        return str(context[-1])[:120] if context else "interaction"
    except Exception:  # noqa: BLE001
        return "interaction"


# Process-wide singleton (orchestrator-root).
_DEFAULT: Optional[PreferenceLedger] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_ledger() -> PreferenceLedger:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = PreferenceLedger()
        return _DEFAULT


def reset_default_ledger() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


__all__ = [
    "PreferenceLedger",
    "get_default_ledger",
    "reset_default_ledger",
]
