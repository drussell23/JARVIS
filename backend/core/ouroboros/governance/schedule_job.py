"""
ScheduledJob + JobRegistry — Slice 2 of the Scheduled Wake-ups arc.
===================================================================

Named-handler primitive. Separates *what to fire* (the handler
registry) from *when to fire it* (the :class:`ScheduleExpression`
wrapper + runtime clock). The existing :mod:`scheduled_agents`
module only fires a single dispatch path (GLS goal submission); this
slice opens the door for operators / the orchestrator to register
arbitrary async callbacks keyed by a short name.

Why a registry
--------------

* §1 Authority — handlers are authored by the operator or the
  orchestrator, NEVER by the model. :meth:`JobRegistry.register`
  requires a ``PinSource``-style source tag and refuses the ``model``
  source. Schedules carry handler NAMES, not callables; the runner
  resolves names at fire time through the registry. The model can ASK
  for a job to be added via a REPL command, but the adding happens
  through operator / orchestrator code.
* §7 Fail-closed — missing-handler at fire time returns a structured
  ``fire_result`` of shape ``{"status": "no_handler", ...}`` and the
  runner keeps going. Handler-raise is caught and reported.
* §8 Observable — every register / unregister / fire emits an INFO log
  line; Slice 4 bridges the fire events to SSE.

Shape
-----

* :class:`ScheduledJob` — a frozen value type: ``{job_id, handler_name,
  expression, payload, enabled, created_at, last_run, next_run,
  run_count, max_runs}``. One-shot jobs set ``max_runs=1``.
* :class:`JobRegistry` — per-process store with:
    - handler dict (name → async callable)
    - job dict (id → :class:`ScheduledJob`)
    - bounded size caps, thread-safe, listener hooks for SSE bridging.

The runner itself lives in Slice 4.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Awaitable, Callable, Dict, FrozenSet, List, Mapping, Optional,
)

from backend.core.ouroboros.governance.schedule_expression import (
    ScheduleExpression,
    ScheduleExpressionError,
)

logger = logging.getLogger("Ouroboros.ScheduleJob")


SCHEDULED_JOB_SCHEMA_VERSION: str = "schedule_job.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JobRegistryError(Exception):
    """Raised on illegal registry operations."""


class HandlerAuthorityError(JobRegistryError):
    """Raised when a non-authoritative source tries to register a handler."""


# ---------------------------------------------------------------------------
# Handler authority (§1 boundary)
# ---------------------------------------------------------------------------


class HandlerSource(str, enum.Enum):
    OPERATOR = "operator"
    ORCHESTRATOR = "orchestrator"
    # ``MODEL`` is deliberately absent — writers validate + refuse.


_AUTHORITATIVE_SOURCES: FrozenSet[HandlerSource] = frozenset({
    HandlerSource.OPERATOR, HandlerSource.ORCHESTRATOR,
})


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------


# Handlers receive (job, payload) and may do anything; they must be
# async. Return value is opaque — the registry captures it and logs
# the type for audit.
Handler = Callable[["ScheduledJob", Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class HandlerRegistration:
    """A named handler registration record."""

    name: str
    source: str              # HandlerSource.value
    registered_at_iso: str
    description: str = ""
    # callable is stored on the registry, NOT on this frozen record
    # (callables aren't hashable; freezing them here breaks equality).


# ---------------------------------------------------------------------------
# ScheduledJob — frozen value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledJob:
    """One scheduled firing plan.

    ``handler_name`` must resolve through the :class:`JobRegistry` at
    fire time. ``expression`` is the :class:`ScheduleExpression`
    (canonical cron + original phrase). ``payload`` is arbitrary
    JSON-serialisable data passed to the handler at fire time.
    """

    job_id: str
    handler_name: str
    expression: ScheduleExpression
    payload: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_at_ts: float = 0.0
    created_at_iso: str = ""
    last_run_ts: Optional[float] = None
    next_run_ts: Optional[float] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    description: str = ""
    schema_version: str = SCHEDULED_JOB_SCHEMA_VERSION

    # --- convenience helpers --------------------------------------------

    def with_run_recorded(
        self, *, fired_ts: float, next_run_ts: Optional[float],
    ) -> "ScheduledJob":
        """Return a new :class:`ScheduledJob` with run counters advanced."""
        return ScheduledJob(
            **{
                **self.__dict__,
                "last_run_ts": fired_ts,
                "next_run_ts": next_run_ts,
                "run_count": self.run_count + 1,
            }
        )

    def with_enabled(self, enabled: bool) -> "ScheduledJob":
        return ScheduledJob(**{**self.__dict__, "enabled": enabled})

    @property
    def exhausted(self) -> bool:
        """True when ``max_runs`` is set and reached."""
        return self.max_runs is not None and self.run_count >= self.max_runs


# ---------------------------------------------------------------------------
# Id helpers + projection
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _make_job_id(
    handler_name: str, canonical_cron: str, payload: Mapping[str, Any],
) -> str:
    """Deterministic-ish id from handler + cron + payload hash + uuid tail.

    The uuid tail makes re-adding the same schedule produce a different
    job id so operators can have multiple concurrent instances. Fully
    deterministic ids (for idempotent re-submission) are a future-slice
    concern.
    """
    seed = f"{handler_name}\0{canonical_cron}\0{sorted(payload.items())}"
    prefix = hashlib.sha256(seed.encode()).hexdigest()[:8]
    tail = uuid.uuid4().hex[:4]
    return f"job-{prefix}-{tail}"


# ---------------------------------------------------------------------------
# JobRegistry
# ---------------------------------------------------------------------------


class JobRegistry:
    """Per-process registry of named handlers + scheduled jobs.

    Thread-safe. Bounded (``max_handlers`` / ``max_jobs``). Listener
    hooks give Slice 4 a clean bridge to SSE.
    """

    def __init__(
        self,
        *,
        max_handlers: int = 128,
        max_jobs: int = 1024,
    ) -> None:
        self._lock = threading.Lock()
        self._handlers: Dict[str, Handler] = {}
        self._handler_meta: Dict[str, HandlerRegistration] = {}
        self._jobs: Dict[str, ScheduledJob] = {}
        self._max_handlers = max(1, max_handlers)
        self._max_jobs = max(1, max_jobs)
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- handler lifecycle ----------------------------------------------

    def register_handler(
        self,
        name: str,
        handler: Handler,
        *,
        source: HandlerSource,
        description: str = "",
    ) -> HandlerRegistration:
        """Register an async handler under ``name``.

        Raises :class:`HandlerAuthorityError` if ``source`` is not
        authoritative; :class:`JobRegistryError` on empty name,
        non-callable handler, or duplicate name.
        """
        if source not in _AUTHORITATIVE_SOURCES:
            raise HandlerAuthorityError(
                f"handler source {source!r} not authoritative"
            )
        if not isinstance(name, str) or not name.strip():
            raise JobRegistryError("handler name must be non-empty string")
        if not callable(handler):
            raise JobRegistryError("handler must be callable")
        with self._lock:
            if name in self._handlers:
                raise JobRegistryError(f"handler already registered: {name}")
            if len(self._handlers) >= self._max_handlers:
                raise JobRegistryError(
                    f"handler cap {self._max_handlers} reached"
                )
            self._handlers[name] = handler
            reg = HandlerRegistration(
                name=name,
                source=source.value,
                registered_at_iso=_utc_iso_now(),
                description=(description or "").strip()[:500],
            )
            self._handler_meta[name] = reg
        logger.info(
            "[ScheduleJob] handler_registered name=%s source=%s",
            name, source.value,
        )
        self._fire("handler_registered", {"name": name, **reg.__dict__})
        return reg

    def unregister_handler(self, name: str) -> bool:
        with self._lock:
            if name not in self._handlers:
                return False
            self._handlers.pop(name, None)
            self._handler_meta.pop(name, None)
        logger.info("[ScheduleJob] handler_unregistered name=%s", name)
        self._fire("handler_unregistered", {"name": name})
        return True

    def list_handlers(self) -> List[HandlerRegistration]:
        with self._lock:
            return list(self._handler_meta.values())

    def get_handler(self, name: str) -> Optional[Handler]:
        with self._lock:
            return self._handlers.get(name)

    def has_handler(self, name: str) -> bool:
        with self._lock:
            return name in self._handlers

    # --- job lifecycle ---------------------------------------------------

    def add_job(
        self,
        *,
        handler_name: str,
        expression: ScheduleExpression,
        payload: Optional[Mapping[str, Any]] = None,
        description: str = "",
        max_runs: Optional[int] = None,
        now_ts: Optional[float] = None,
    ) -> ScheduledJob:
        """Register a scheduled job.

        Raises :class:`JobRegistryError` if:
          * ``handler_name`` is not currently registered
          * the cap ``max_jobs`` is reached
        """
        if not isinstance(expression, ScheduleExpression):
            raise JobRegistryError(
                "expression must be a ScheduleExpression instance",
            )
        if not isinstance(handler_name, str) or not handler_name.strip():
            raise JobRegistryError("handler_name must be non-empty string")
        with self._lock:
            if handler_name not in self._handlers:
                raise JobRegistryError(
                    f"unknown handler: {handler_name!r} — register first",
                )
            if len(self._jobs) >= self._max_jobs:
                raise JobRegistryError(
                    f"job cap {self._max_jobs} reached",
                )
        payload_dict: Dict[str, Any] = dict(payload or {})
        now = now_ts if now_ts is not None else time.time()
        try:
            next_run = expression.next_fire_time(after=now)
        except ScheduleExpressionError as exc:
            raise JobRegistryError(
                f"expression has no upcoming fire: {exc}"
            ) from exc
        job_id = _make_job_id(
            handler_name, expression.canonical_cron, payload_dict,
        )
        job = ScheduledJob(
            job_id=job_id,
            handler_name=handler_name,
            expression=expression,
            payload=payload_dict,
            enabled=True,
            created_at_ts=now,
            created_at_iso=_utc_iso_now(),
            last_run_ts=None,
            next_run_ts=next_run,
            run_count=0,
            max_runs=max_runs,
            description=(description or "").strip()[:500],
        )
        with self._lock:
            self._jobs[job_id] = job
        logger.info(
            "[ScheduleJob] job_added id=%s handler=%s cron=%s next=%s",
            job_id, handler_name, expression.canonical_cron, next_run,
        )
        self._fire("job_added", self.project_job(job))
        return job

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        logger.info("[ScheduleJob] job_removed id=%s", job_id)
        self._fire("job_removed", self.project_job(job))
        return True

    def enable_job(self, job_id: str) -> Optional[ScheduledJob]:
        return self._update_enabled(job_id, True)

    def disable_job(self, job_id: str) -> Optional[ScheduledJob]:
        return self._update_enabled(job_id, False)

    def _update_enabled(
        self, job_id: str, enabled: bool,
    ) -> Optional[ScheduledJob]:
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is None:
                return None
            updated = existing.with_enabled(enabled)
            self._jobs[job_id] = updated
        logger.info(
            "[ScheduleJob] job_%s id=%s",
            "enabled" if enabled else "disabled", job_id,
        )
        self._fire(
            "job_enabled" if enabled else "job_disabled",
            self.project_job(updated),
        )
        return updated

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(
        self, *, enabled_only: bool = False,
    ) -> List[ScheduledJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        jobs.sort(key=lambda j: j.next_run_ts or float("inf"))
        return jobs

    def record_fire(
        self,
        job_id: str,
        *,
        fired_ts: Optional[float] = None,
    ) -> Optional[ScheduledJob]:
        """Advance run counters after a successful fire.

        Computes the next fire time from the expression; if the job
        has exhausted ``max_runs`` it is disabled (not removed —
        operators can inspect the corpse until they choose to evict).
        """
        fired = fired_ts if fired_ts is not None else time.time()
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is None:
                return None
            try:
                next_run = existing.expression.next_fire_time(after=fired)
            except ScheduleExpressionError:
                next_run = None
            updated = existing.with_run_recorded(
                fired_ts=fired, next_run_ts=next_run,
            )
            if updated.exhausted:
                updated = updated.with_enabled(False)
            self._jobs[job_id] = updated
        self._fire("job_fired", self.project_job(updated))
        return updated

    # --- projection (Slice 4 SSE + GET) ---------------------------------

    @staticmethod
    def project_job(job: ScheduledJob) -> Dict[str, Any]:
        return {
            "schema_version": job.schema_version,
            "job_id": job.job_id,
            "handler_name": job.handler_name,
            "canonical_cron": job.expression.canonical_cron,
            "original_phrase": job.expression.original_phrase,
            "payload_keys": sorted(dict(job.payload).keys()),
            "enabled": job.enabled,
            "created_at_iso": job.created_at_iso,
            "created_at_ts": job.created_at_ts,
            "last_run_ts": job.last_run_ts,
            "next_run_ts": job.next_run_ts,
            "run_count": job.run_count,
            "max_runs": job.max_runs,
            "description": job.description,
        }

    # --- listener hooks -------------------------------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, projection: Dict[str, Any]) -> None:
        payload = {"event_type": event_type, "projection": projection}
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[ScheduleJob] listener exception on %s: %s",
                    event_type, exc,
                )

    # --- test helpers ---------------------------------------------------

    def reset(self) -> None:
        """Test helper. Never called from production."""
        with self._lock:
            self._handlers.clear()
            self._handler_meta.clear()
            self._jobs.clear()
            self._listeners.clear()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_registry: Optional[JobRegistry] = None
_registry_lock = threading.Lock()


def get_default_job_registry() -> JobRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = JobRegistry()
        return _default_registry


def reset_default_job_registry() -> None:
    global _default_registry
    with _registry_lock:
        if _default_registry is not None:
            _default_registry.reset()
        _default_registry = None


__all__ = [
    "SCHEDULED_JOB_SCHEMA_VERSION",
    "Handler",
    "HandlerAuthorityError",
    "HandlerRegistration",
    "HandlerSource",
    "JobRegistry",
    "JobRegistryError",
    "ScheduledJob",
    "get_default_job_registry",
    "reset_default_job_registry",
]
