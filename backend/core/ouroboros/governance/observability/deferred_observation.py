"""Slice 2.3 — DeferredObservation queue.

Per ``OUROBOROS_VENOM_PRD.md`` §24.3.3 / §24.10.2:

  > Self-paced wake-up scheduling: ``{observation_target, due_unix,
  > hypothesis, max_wait_s}`` queue. Low-priority worker walks the
  > queue and re-fires observations when due.

This module ships the **persistent async observation queue** that
makes "check back later" possible. It is the infrastructure layer
beneath ``PostMergeAuditor`` (Slice 2.1) and ``TrajectoryAuditor``
(Slice 2.2).

## How it works

1.  A producer (e.g. PostMergeAuditor) calls ``schedule()`` with an
    ``ObservationIntent`` describing *what* to observe, *when*, and
    *what outcome to expect* (the hypothesis).
2.  The queue persists the intent to a JSONL file (append-only).
3.  On each ``tick(now_unix, observer_fn)``, the queue walks pending
    intents. Any intent whose ``due_unix <= now_unix`` is fired —
    the ``observer_fn`` callback is invoked with the intent, and the
    result is recorded.
4.  Intents past their ``due_unix + max_wait_s`` deadline without
    being observed are auto-expired.

## Design constraints (load-bearing)

  * **Tick-driven, not timer-driven** — ``tick()`` is called by the
    orchestrator's heartbeat loop (same injection point as
    ``CuriosityScheduler.tick()``). No background thread. No
    ``asyncio.sleep()`` loop. The caller controls the clock.
  * **Content-addressed intent dedup** — two identical
    ``(origin, observation_target, hypothesis)`` tuples produce the
    same ``intent_id``. Scheduling a duplicate is a no-op.
  * **Bounded queue** — at most ``MAX_PENDING_OBSERVATIONS`` pending
    intents at any time. Beyond that, ``schedule()`` returns
    ``(False, "queue_full")``.
  * **JSONL persistence** — survives daemon restarts. Uses
    ``flock_exclusive`` from ``adaptation/_file_lock.py`` for
    cross-process safety.
  * **Stdlib + _file_lock + determinism_substrate import surface
    only.** Leaf module — no governance, no orchestrator imports.
  * **NEVER raises into the caller** — all public methods return
    structured results or empty lists on error.

## Default-off

``JARVIS_DEFERRED_OBSERVATION_ENABLED`` (default false until
Phase 2 graduation).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Hard caps (bounded sizes — defends against runaway producers)
# ---------------------------------------------------------------------------

MAX_PENDING_OBSERVATIONS: int = 100
MAX_INTENT_METADATA_KEYS: int = 32
MAX_HYPOTHESIS_CHARS: int = 500
MAX_TARGET_CHARS: int = 500
MAX_RESULT_CHARS: int = 2_000
MAX_LEDGER_FILE_BYTES: int = 8 * 1024 * 1024  # 8 MiB


# ---------------------------------------------------------------------------
# Master flag + path config
# ---------------------------------------------------------------------------


def is_deferred_observation_enabled() -> bool:
    """Master flag — ``JARVIS_DEFERRED_OBSERVATION_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_DEFERRED_OBSERVATION_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _observation_path() -> Path:
    raw = os.environ.get("JARVIS_DEFERRED_OBSERVATION_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "deferred_observations.jsonl"


# ---------------------------------------------------------------------------
# Intent status constants
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_EXPIRED = "expired"
STATUS_COMPLETED = "completed"

_TERMINAL_STATUSES = frozenset({STATUS_FIRED, STATUS_EXPIRED, STATUS_COMPLETED})


# ---------------------------------------------------------------------------
# ObservationIntent — one scheduled observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationIntent:
    """One deferred observation. Frozen — append-only history.

    Parameters
    ----------
    intent_id:
        Content-addressed dedup key: ``sha256(origin + target +
        hypothesis)[:16]``.
    origin:
        Who scheduled this (e.g. ``"post_merge_auditor"``).
    observation_target:
        What to observe (e.g. ``"commit:<sha>"``, ``"files:<glob>"``).
    hypothesis:
        What we expect (e.g. ``"no new test failures"``).
    due_unix:
        When this observation should fire (Unix epoch seconds).
    created_unix:
        When it was scheduled.
    max_wait_s:
        Hard deadline — skip if not observed by
        ``due_unix + max_wait_s``.
    status:
        ``"pending"`` | ``"fired"`` | ``"expired"`` | ``"completed"``.
    result:
        Outcome after observation. Empty while pending.
    metadata:
        Origin-specific context (commit_sha, op_id, etc.).
    """

    intent_id: str
    origin: str
    observation_target: str
    hypothesis: str
    due_unix: float
    created_unix: float
    max_wait_s: float = 3600.0  # default 1h grace window
    status: str = STATUS_PENDING
    result: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_due(self, now_unix: float) -> bool:
        """True iff this intent is pending and due for observation."""
        return self.status == STATUS_PENDING and now_unix >= self.due_unix

    def is_expired(self, now_unix: float) -> bool:
        """True iff this intent is pending and past its hard deadline."""
        return (
            self.status == STATUS_PENDING
            and now_unix > (self.due_unix + self.max_wait_s)
        )

    def is_terminal(self) -> bool:
        """True iff this intent has reached a final state."""
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "intent_id": self.intent_id,
            "origin": self.origin,
            "observation_target": self.observation_target,
            "hypothesis": self.hypothesis,
            "due_unix": self.due_unix,
            "created_unix": self.created_unix,
            "max_wait_s": self.max_wait_s,
            "status": self.status,
        }
        if self.result:
            d["result"] = self.result
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    def with_status(self, status: str, result: str = "") -> "ObservationIntent":
        """Return a copy with updated status and result."""
        return ObservationIntent(
            intent_id=self.intent_id,
            origin=self.origin,
            observation_target=self.observation_target,
            hypothesis=self.hypothesis,
            due_unix=self.due_unix,
            created_unix=self.created_unix,
            max_wait_s=self.max_wait_s,
            status=status,
            result=result[:MAX_RESULT_CHARS] if result else "",
            metadata=self.metadata,
        )


def compute_intent_id(origin: str, target: str, hypothesis: str) -> str:
    """Content-addressed dedup key for an observation intent.

    Same ``(origin, target, hypothesis)`` always produces the same
    ``intent_id``. This prevents duplicate scheduling (e.g. crash
    recovery re-scheduling the same 24h observation).
    """
    raw = f"{origin}|{target}|{hypothesis}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_intent(obj: Dict[str, Any]) -> Optional[ObservationIntent]:
    """Parse one JSONL line into an ObservationIntent. Returns None
    on parse failure. NEVER raises."""
    try:
        return ObservationIntent(
            intent_id=str(obj.get("intent_id") or ""),
            origin=str(obj.get("origin") or ""),
            observation_target=str(obj.get("observation_target") or ""),
            hypothesis=str(obj.get("hypothesis") or ""),
            due_unix=float(obj.get("due_unix") or 0.0),
            created_unix=float(obj.get("created_unix") or 0.0),
            max_wait_s=float(obj.get("max_wait_s") or 3600.0),
            status=str(obj.get("status") or STATUS_PENDING),
            result=str(obj.get("result") or ""),
            metadata=obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {},
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ObservationResult — outcome of a fired observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationResult:
    """Outcome of a single observation firing.

    ``success`` indicates whether the observer callback ran without
    error. ``result_text`` carries the observer's structured output
    (truncated to ``MAX_RESULT_CHARS``).
    """

    intent: ObservationIntent
    success: bool
    result_text: str = ""
    error: str = ""
    fired_unix: float = 0.0


# ---------------------------------------------------------------------------
# DeferredObservationQueue
# ---------------------------------------------------------------------------


class DeferredObservationQueue:
    """Persistent observation queue with tick-driven evaluation.

    All injection points are optional; production wires real callables,
    tests inject fakes.

    Parameters
    ----------
    path:
        JSONL file path for persistence. Default: ``.jarvis/deferred_observations.jsonl``.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _observation_path()
        # In-memory cache of all intents (loaded lazily).
        self._intents: Optional[Dict[str, ObservationIntent]] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Lazy loading
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> Dict[str, ObservationIntent]:
        """Load intents from disk on first access. NEVER raises."""
        if self._intents is not None:
            return self._intents
        self._intents = {}
        if not self._path.exists():
            self._loaded = True
            return self._intents
        try:
            size = self._path.stat().st_size
        except OSError:
            self._loaded = True
            return self._intents
        if size > MAX_LEDGER_FILE_BYTES:
            logger.warning(
                "[DeferredObservation] %s exceeds MAX_LEDGER_FILE_BYTES=%d "
                "(was %d) — refusing to load",
                self._path, MAX_LEDGER_FILE_BYTES, size,
            )
            self._loaded = True
            return self._intents
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            self._loaded = True
            return self._intents
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            intent = _parse_intent(obj)
            if intent and intent.intent_id:
                self._intents[intent.intent_id] = intent
        self._loaded = True
        return self._intents

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> Tuple[bool, str]:
        """Write all intents to disk. NEVER raises."""
        intents = self._ensure_loaded()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (False, f"mkdir_failed:{exc}")
        try:
            with self._path.open("w", encoding="utf-8") as f:
                try:
                    from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                        flock_exclusive,
                    )
                    lock_ctx = flock_exclusive(f.fileno())
                except ImportError:
                    import contextlib
                    lock_ctx = contextlib.nullcontext(True)
                with lock_ctx:
                    for intent in intents.values():
                        line = json.dumps(
                            intent.to_dict(),
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        f.write(line)
                        f.write("\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except OSError as exc:
            return (False, f"write_failed:{exc}")
        return (True, "ok")

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def schedule(self, intent: ObservationIntent) -> Tuple[bool, str]:
        """Schedule a new deferred observation. Returns ``(ok, detail)``.

        Pre-checks:
          1. Master flag off → ``(False, "master_off")``
          2. Duplicate intent_id → ``(False, "duplicate")``
          3. Queue full → ``(False, "queue_full")``

        NEVER raises.
        """
        if not is_deferred_observation_enabled():
            return (False, "master_off")
        if not intent.intent_id:
            return (False, "empty_intent_id")
        if not intent.origin:
            return (False, "empty_origin")
        if not intent.observation_target:
            return (False, "empty_target")

        intents = self._ensure_loaded()

        # Content-addressed dedup.
        if intent.intent_id in intents:
            existing = intents[intent.intent_id]
            if not existing.is_terminal():
                return (False, "duplicate")
            # If the existing intent is terminal, allow re-scheduling
            # (new observation cycle for the same target).

        # Bounded queue — count pending only.
        pending_count = sum(
            1 for i in intents.values()
            if i.status == STATUS_PENDING
        )
        if pending_count >= MAX_PENDING_OBSERVATIONS:
            return (False, "queue_full")

        # Truncate fields to bounded sizes.
        safe_intent = ObservationIntent(
            intent_id=intent.intent_id,
            origin=intent.origin[:200],
            observation_target=intent.observation_target[:MAX_TARGET_CHARS],
            hypothesis=intent.hypothesis[:MAX_HYPOTHESIS_CHARS],
            due_unix=intent.due_unix,
            created_unix=intent.created_unix or time.time(),
            max_wait_s=max(0.0, intent.max_wait_s),
            status=STATUS_PENDING,
            result="",
            metadata=_truncate_metadata(intent.metadata),
        )

        intents[safe_intent.intent_id] = safe_intent
        ok, detail = self._persist()
        if not ok:
            # Rollback in-memory on persistence failure.
            intents.pop(safe_intent.intent_id, None)
            return (False, f"persist_failed:{detail}")

        logger.info(
            "[DeferredObservation] Scheduled intent=%s origin=%s "
            "target=%s due_in=%.0fs",
            safe_intent.intent_id[:8],
            safe_intent.origin,
            safe_intent.observation_target[:60],
            max(0.0, safe_intent.due_unix - time.time()),
        )
        return (True, "ok")

    # ------------------------------------------------------------------
    # Tick — the heartbeat entry point
    # ------------------------------------------------------------------

    def tick(
        self,
        now_unix: float,
        observer: Optional[Callable[[ObservationIntent], str]] = None,
    ) -> List[ObservationResult]:
        """Walk pending intents and fire those that are due.

        Parameters
        ----------
        now_unix:
            Current Unix epoch seconds. The caller controls the clock
            (deterministic testing).
        observer:
            Callback invoked for each due intent. Receives the
            ``ObservationIntent`` and returns a result string
            (truncated to ``MAX_RESULT_CHARS``). If ``None``, due
            intents are auto-expired.

        Returns
        -------
        List[ObservationResult]
            One result per intent that was fired or expired during
            this tick.

        NEVER raises.
        """
        if not is_deferred_observation_enabled():
            return []

        intents = self._ensure_loaded()
        results: List[ObservationResult] = []
        mutated = False

        for intent_id in list(intents.keys()):
            intent = intents[intent_id]
            if intent.status != STATUS_PENDING:
                continue

            # Check expiration first (hard deadline).
            if intent.is_expired(now_unix):
                updated = intent.with_status(STATUS_EXPIRED, "deadline_exceeded")
                intents[intent_id] = updated
                results.append(ObservationResult(
                    intent=updated,
                    success=False,
                    result_text="deadline_exceeded",
                    fired_unix=now_unix,
                ))
                mutated = True
                continue

            # Check if due.
            if not intent.is_due(now_unix):
                continue

            if observer is None:
                # No observer callback — auto-expire.
                updated = intent.with_status(STATUS_EXPIRED, "no_observer")
                intents[intent_id] = updated
                results.append(ObservationResult(
                    intent=updated,
                    success=False,
                    result_text="no_observer",
                    fired_unix=now_unix,
                ))
                mutated = True
                continue

            # Fire the observer.
            try:
                result_text = observer(intent)
                result_text = (result_text or "")[:MAX_RESULT_CHARS]
                updated = intent.with_status(STATUS_FIRED, result_text)
                intents[intent_id] = updated
                results.append(ObservationResult(
                    intent=updated,
                    success=True,
                    result_text=result_text,
                    fired_unix=now_unix,
                ))
            except Exception as exc:  # noqa: BLE001 — defensive
                error_str = f"{type(exc).__name__}:{str(exc)[:300]}"
                updated = intent.with_status(STATUS_FIRED, f"error:{error_str}")
                intents[intent_id] = updated
                results.append(ObservationResult(
                    intent=updated,
                    success=False,
                    result_text="",
                    error=error_str,
                    fired_unix=now_unix,
                ))
            mutated = True

        if mutated:
            self._persist()

        return results

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """Number of pending (not yet fired/expired) intents."""
        intents = self._ensure_loaded()
        return sum(1 for i in intents.values() if i.status == STATUS_PENDING)

    def read_all(self) -> List[ObservationIntent]:
        """Return all intents (all statuses). Sorted by due_unix ascending."""
        intents = self._ensure_loaded()
        return sorted(intents.values(), key=lambda i: i.due_unix)

    def read_pending(self) -> List[ObservationIntent]:
        """Return only pending intents. Sorted by due_unix ascending."""
        intents = self._ensure_loaded()
        return sorted(
            (i for i in intents.values() if i.status == STATUS_PENDING),
            key=lambda i: i.due_unix,
        )

    def expire_stale(self, now_unix: float) -> int:
        """Expire all pending intents past their hard deadline.

        Returns the number of intents expired. NEVER raises.
        """
        if not is_deferred_observation_enabled():
            return 0
        intents = self._ensure_loaded()
        expired_count = 0
        for intent_id in list(intents.keys()):
            intent = intents[intent_id]
            if intent.is_expired(now_unix):
                intents[intent_id] = intent.with_status(
                    STATUS_EXPIRED, "deadline_exceeded",
                )
                expired_count += 1
        if expired_count > 0:
            self._persist()
        return expired_count

    def get_intent(self, intent_id: str) -> Optional[ObservationIntent]:
        """Retrieve a single intent by ID. Returns None if not found."""
        intents = self._ensure_loaded()
        return intents.get(intent_id)

    def complete_intent(
        self, intent_id: str, result: str = "",
    ) -> Tuple[bool, str]:
        """Mark a fired intent as completed with a final result.

        This is for use cases where observation happens in two stages:
        ``tick()`` fires (status → ``"fired"``) and later the observer
        reports final outcome (status → ``"completed"``).
        """
        if not is_deferred_observation_enabled():
            return (False, "master_off")
        intents = self._ensure_loaded()
        intent = intents.get(intent_id)
        if intent is None:
            return (False, "not_found")
        if intent.status != STATUS_FIRED:
            return (False, f"wrong_status:{intent.status}")
        intents[intent_id] = intent.with_status(
            STATUS_COMPLETED, result[:MAX_RESULT_CHARS],
        )
        self._persist()
        return (True, "ok")

    # ------------------------------------------------------------------
    # Reset (test-only)
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all in-memory state. For tests."""
        self._intents = None
        self._loaded = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Truncate metadata dict to bounded size."""
    if not isinstance(meta, dict):
        return {}
    if len(meta) <= MAX_INTENT_METADATA_KEYS:
        return dict(meta)
    keys = list(meta.keys())[:MAX_INTENT_METADATA_KEYS]
    return {k: meta[k] for k in keys}


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_intent(
    *,
    origin: str,
    observation_target: str,
    hypothesis: str,
    due_unix: float,
    max_wait_s: float = 3600.0,
    metadata: Optional[Dict[str, Any]] = None,
    now_unix: Optional[float] = None,
) -> ObservationIntent:
    """Convenience factory for creating an ObservationIntent with
    auto-computed content-addressed ``intent_id``.

    This is the recommended way to create intents — callers don't
    need to compute the dedup key manually.
    """
    intent_id = compute_intent_id(origin, observation_target, hypothesis)
    return ObservationIntent(
        intent_id=intent_id,
        origin=origin,
        observation_target=observation_target,
        hypothesis=hypothesis,
        due_unix=due_unix,
        created_unix=now_unix or time.time(),
        max_wait_s=max_wait_s,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------


_DEFAULT_QUEUE: Optional[DeferredObservationQueue] = None


def get_default_queue() -> DeferredObservationQueue:
    global _DEFAULT_QUEUE
    if _DEFAULT_QUEUE is None:
        _DEFAULT_QUEUE = DeferredObservationQueue()
    return _DEFAULT_QUEUE


def reset_default_queue() -> None:
    global _DEFAULT_QUEUE
    _DEFAULT_QUEUE = None


__all__ = [
    "DeferredObservationQueue",
    "MAX_HYPOTHESIS_CHARS",
    "MAX_INTENT_METADATA_KEYS",
    "MAX_LEDGER_FILE_BYTES",
    "MAX_PENDING_OBSERVATIONS",
    "MAX_RESULT_CHARS",
    "MAX_TARGET_CHARS",
    "ObservationIntent",
    "ObservationResult",
    "STATUS_COMPLETED",
    "STATUS_EXPIRED",
    "STATUS_FIRED",
    "STATUS_PENDING",
    "compute_intent_id",
    "get_default_queue",
    "is_deferred_observation_enabled",
    "make_intent",
    "reset_default_queue",
]
