"""
SagaOrchestrator — WAL-backed state machine for executing ArchitecturalPlans.

Each saga maps one-to-one to an ArchitecturalPlan execution run.  The
orchestrator:

1. Decomposes the plan into IntentEnvelopes via PlanDecomposer.
2. Submits each envelope to the Unified Intake Router in topological order,
   checking inter-step dependencies at each transition.
3. Persists every state change to a Write-Ahead Log (WAL) at
   ``{saga_dir}/{saga_id}.json`` so that saga state survives process restarts.
4. Runs AcceptanceChecks after all steps complete and marks the saga
   COMPLETE or ABORTED accordingly.

Usage::

    orchestrator = SagaOrchestrator(
        plan_store=plan_store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
    )
    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.architect.plan import ArchitecturalPlan
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer
from backend.core.ouroboros.architect.saga import (
    SagaPhase,
    SagaRecord,
    StepPhase,
)

_log = logging.getLogger(__name__)

_DEFAULT_SAGA_DIR = Path.home() / ".jarvis" / "ouroboros" / "sagas"


class SagaOrchestrator:
    """WAL-backed orchestrator that executes multi-step architectural plans.

    Parameters
    ----------
    plan_store:
        Object with a ``load(plan_hash: str) -> Optional[ArchitecturalPlan]``
        method.  Used to retrieve the plan for a given saga.
    intake_router:
        Object with an ``async ingest(envelope) -> str`` method.  Each
        decomposed step envelope is submitted here.
    acceptance_runner:
        Object with an ``async run_checks(checks, saga_id) -> List[AcceptanceResult]``
        method.  Runs end-of-saga acceptance checks.
    saga_dir:
        Directory where ``{saga_id}.json`` WAL files are written.
        Defaults to ``~/.jarvis/ouroboros/sagas``.
    """

    def __init__(
        self,
        plan_store,
        intake_router,
        acceptance_runner,
        saga_dir: Optional[Path] = None,
        spinal_cord: Any = None,
        narrator: Any = None,
    ) -> None:
        self._plan_store = plan_store
        self._intake_router = intake_router
        self._acceptance_runner = acceptance_runner
        self._saga_dir: Path = saga_dir if saga_dir is not None else _DEFAULT_SAGA_DIR
        self._saga_dir.mkdir(parents=True, exist_ok=True)
        self._spinal_cord = spinal_cord
        self._narrator = narrator

        # In-memory index — populated from WAL on startup and kept in sync.
        self._sagas: Dict[str, SagaRecord] = {}
        self._load_from_wal()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_saga(self, plan: ArchitecturalPlan) -> SagaRecord:
        """Create a new PENDING saga for *plan* and persist it to WAL.

        Parameters
        ----------
        plan:
            The plan to create a saga for.

        Returns
        -------
        SagaRecord
            A freshly created record with all steps in PENDING phase.
        """
        saga_id = uuid.uuid4().hex[:16]
        saga = SagaRecord.create(
            saga_id=saga_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            num_steps=len(plan.steps),
        )
        self._sagas[saga_id] = saga
        self._persist(saga)
        _log.info(
            "saga.created saga_id=%s plan_id=%s plan_hash=%s steps=%d",
            saga_id,
            plan.plan_id,
            plan.plan_hash,
            len(plan.steps),
        )
        return saga

    async def execute(self, saga_id: str) -> SagaRecord:
        """Execute the saga identified by *saga_id*.

        Execution steps:

        1. Load the saga record; abort if not found.
        2. Load the associated plan from ``plan_store``; abort if missing.
        3. Transition saga to RUNNING and persist.
        4. Decompose the plan into envelopes (topological order).
        5. For each envelope, verify its dependencies are COMPLETE, then
           submit it via ``intake_router.ingest``; on exception, mark the
           step FAILED, mark remaining steps BLOCKED, abort the saga.
        6. Run acceptance checks; any failure aborts the saga.
        7. Mark the saga COMPLETE and persist.

        Parameters
        ----------
        saga_id:
            Identifier returned by :meth:`create_saga`.

        Returns
        -------
        SagaRecord
            The final saga state (COMPLETE or ABORTED).
        """
        saga = self._sagas.get(saga_id)
        if saga is None:
            _log.error("saga.execute.missing saga_id=%s", saga_id)
            # Return a minimal aborted record without a real plan reference.
            dummy = SagaRecord(
                saga_id=saga_id,
                plan_id="",
                plan_hash="",
                phase=SagaPhase.ABORTED,
                step_states={},
                created_at=time.time(),
                completed_at=time.time(),
                abort_reason=f"No saga record found for saga_id={saga_id}",
            )
            return dummy

        # Load the plan from the store.
        plan: Optional[ArchitecturalPlan] = self._plan_store.load(saga.plan_hash)
        if plan is None:
            _log.error(
                "saga.execute.plan_missing saga_id=%s plan_hash=%s",
                saga_id,
                saga.plan_hash,
            )
            return await self._abort_async(
                saga,
                reason=f"Plan not found in store for plan_hash={saga.plan_hash}",
            )

        # Transition to RUNNING.
        saga.phase = SagaPhase.RUNNING
        self._persist(saga)
        _log.info("saga.running saga_id=%s", saga_id)
        if self._spinal_cord:
            await self._spinal_cord.stream_up("saga.started", {"saga_id": saga_id, "plan_id": saga.plan_id})
        if self._narrator:
            await self._narrator.on_event("saga.started", {"saga_id": saga_id, "plan_id": saga.plan_id})

        # Emit to TelemetryBus for TUI panel (best-effort)
        try:
            from backend.core.telemetry_contract import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            bus.emit(TelemetryEnvelope.create(
                event_schema="ouroboros.saga.started@1.0.0",
                source="saga_orchestrator",
                trace_id=f"saga-{saga_id}",
                span_id=f"saga-start-{saga_id}",
                partition_key="ouroboros",
                payload={
                    "saga_id": saga_id,
                    "plan_id": saga.plan_id,
                    "title": saga.plan_id,
                    "step_count": len(envelopes),
                },
            ))
        except Exception:
            pass  # TUI telemetry is best-effort

        # Decompose plan into ordered envelopes.
        envelopes = PlanDecomposer.decompose(plan, saga_id)

        # Execute each envelope in topological order.
        for envelope in envelopes:
            step_index: int = envelope.evidence.get("step_index", 0)
            step_state = saga.step_states[step_index]

            # Verify all declared dependencies are COMPLETE.
            plan_step = plan.steps[step_index]
            for dep_idx in plan_step.depends_on:
                dep_state = saga.step_states[dep_idx]
                if dep_state.phase is not StepPhase.COMPLETE:
                    reason = (
                        f"Dependency step {dep_idx} is in phase "
                        f"{dep_state.phase.value}, expected complete"
                    )
                    _log.error(
                        "saga.dependency_not_met saga_id=%s step=%d dep=%d",
                        saga_id,
                        step_index,
                        dep_idx,
                    )
                    step_state.phase = StepPhase.FAILED
                    step_state.error = reason
                    return await self._abort_remaining_async(saga, step_index, reason=reason)

            # Mark step RUNNING.
            step_state.phase = StepPhase.RUNNING
            step_state.started_at = time.time()
            self._persist(saga)
            _log.debug("saga.step.running saga_id=%s step=%d", saga_id, step_index)

            # Submit to intake router.
            try:
                await self._intake_router.ingest(envelope)
            except Exception as exc:  # pylint: disable=broad-except
                error_msg = f"{type(exc).__name__}: {exc}"
                _log.error(
                    "saga.step.failed saga_id=%s step=%d error=%s",
                    saga_id,
                    step_index,
                    error_msg,
                )
                step_state.phase = StepPhase.FAILED
                step_state.error = error_msg
                step_state.completed_at = time.time()
                return await self._abort_remaining_async(
                    saga, step_index, reason=error_msg
                )

            # Mark step COMPLETE.
            step_state.phase = StepPhase.COMPLETE
            step_state.completed_at = time.time()
            self._persist(saga)
            _log.info("saga.step.complete saga_id=%s step=%d", saga_id, step_index)

        # All steps complete — run acceptance checks.
        results = await self._acceptance_runner.run_checks(
            plan.acceptance_checks, saga_id
        )
        failed_checks = [r for r in results if not r.passed]
        if failed_checks:
            failed_ids = ", ".join(r.check_id for r in failed_checks)
            reason = f"Acceptance check(s) failed: {failed_ids}"
            _log.warning(
                "saga.acceptance_failed saga_id=%s checks=%s", saga_id, failed_ids
            )
            return await self._abort_async(saga, reason=reason)

        # Success.
        saga.phase = SagaPhase.COMPLETE
        saga.completed_at = time.time()
        self._persist(saga)
        _log.info("saga.complete saga_id=%s", saga_id)
        if self._spinal_cord:
            await self._spinal_cord.stream_up("saga.complete", {"saga_id": saga_id, "plan_id": saga.plan_id})
        if self._narrator:
            await self._narrator.on_event("saga.complete", {"saga_id": saga_id, "plan_id": saga.plan_id})

        # Emit to TelemetryBus for TUI panel (best-effort)
        try:
            from backend.core.telemetry_contract import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            bus.emit(TelemetryEnvelope.create(
                event_schema="ouroboros.saga.complete@1.0.0",
                source="saga_orchestrator",
                trace_id=f"saga-{saga_id}",
                span_id=f"saga-complete-{saga_id}",
                partition_key="ouroboros",
                payload={"saga_id": saga_id, "plan_id": saga.plan_id},
            ))
        except Exception:
            pass  # TUI telemetry is best-effort

        return saga

    def get_saga(self, saga_id: str) -> Optional[SagaRecord]:
        """Return the :class:`SagaRecord` for *saga_id*, or ``None`` if unknown.

        Parameters
        ----------
        saga_id:
            Identifier of the saga to look up.
        """
        return self._sagas.get(saga_id)

    def list_sagas(self) -> List[SagaRecord]:
        """Return all known sagas, in arbitrary order.

        Returns
        -------
        List[SagaRecord]
            All sagas loaded from WAL at startup plus any created in this
            instance's lifetime.
        """
        return list(self._sagas.values())

    # ------------------------------------------------------------------
    # WAL helpers
    # ------------------------------------------------------------------

    def _persist(self, saga: SagaRecord) -> None:
        """Write *saga* to its WAL file atomically."""
        target = self._saga_dir / f"{saga.saga_id}.json"
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(saga.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(target)
        except Exception as exc:  # pylint: disable=broad-except
            tmp.unlink(missing_ok=True)
            _log.error(
                "saga.wal_write_failed saga_id=%s error=%s", saga.saga_id, exc
            )

    def _load_from_wal(self) -> None:
        """Scan *saga_dir* and load all ``{saga_id}.json`` files into memory."""
        for wal_file in self._saga_dir.glob("*.json"):
            try:
                raw = json.loads(wal_file.read_text(encoding="utf-8"))
                saga = SagaRecord.from_dict(raw)
                self._sagas[saga.saga_id] = saga
                _log.debug("saga.wal_loaded saga_id=%s phase=%s", saga.saga_id, saga.phase.value)
            except Exception as exc:  # pylint: disable=broad-except
                _log.warning(
                    "saga.wal_corrupt file=%s error=%s — skipping",
                    wal_file.name,
                    exc,
                )

    # ------------------------------------------------------------------
    # State transition helpers
    # ------------------------------------------------------------------

    def _abort(self, saga: SagaRecord, *, reason: str) -> SagaRecord:
        """Mark *saga* ABORTED with *reason* and persist."""
        saga.phase = SagaPhase.ABORTED
        saga.abort_reason = reason
        saga.completed_at = time.time()
        self._persist(saga)
        _log.warning("saga.aborted saga_id=%s reason=%s", saga.saga_id, reason)
        return saga

    async def _abort_async(self, saga: SagaRecord, *, reason: str) -> SagaRecord:
        """Async variant of :meth:`_abort` that also emits lifecycle events."""
        result = self._abort(saga, reason=reason)
        if self._spinal_cord:
            await self._spinal_cord.stream_up("saga.aborted", {"saga_id": saga.saga_id, "reason": reason})
        if self._narrator:
            await self._narrator.on_event("saga.aborted", {"saga_id": saga.saga_id, "reason": reason})

        # Emit to TelemetryBus for TUI panel (best-effort)
        try:
            from backend.core.telemetry_contract import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            bus.emit(TelemetryEnvelope.create(
                event_schema="ouroboros.saga.aborted@1.0.0",
                source="saga_orchestrator",
                trace_id=f"saga-{saga.saga_id}",
                span_id=f"saga-abort-{saga.saga_id}",
                partition_key="ouroboros",
                payload={"saga_id": saga.saga_id, "reason": reason},
            ))
        except Exception:
            pass  # TUI telemetry is best-effort

        return result

    async def _abort_remaining_async(
        self, saga: SagaRecord, failed_step_index: int, *, reason: str
    ) -> SagaRecord:
        """Async variant of :meth:`_abort_remaining` that also emits lifecycle events."""
        result = self._abort_remaining(saga, failed_step_index, reason=reason)
        if self._spinal_cord:
            await self._spinal_cord.stream_up("saga.aborted", {"saga_id": saga.saga_id, "reason": reason})
        if self._narrator:
            await self._narrator.on_event("saga.aborted", {"saga_id": saga.saga_id, "reason": reason})

        # Emit to TelemetryBus for TUI panel (best-effort)
        try:
            from backend.core.telemetry_contract import get_telemetry_bus, TelemetryEnvelope
            bus = get_telemetry_bus()
            bus.emit(TelemetryEnvelope.create(
                event_schema="ouroboros.saga.aborted@1.0.0",
                source="saga_orchestrator",
                trace_id=f"saga-{saga.saga_id}",
                span_id=f"saga-abort-remaining-{saga.saga_id}",
                partition_key="ouroboros",
                payload={"saga_id": saga.saga_id, "reason": reason},
            ))
        except Exception:
            pass  # TUI telemetry is best-effort

        return result

    def _abort_remaining(
        self, saga: SagaRecord, failed_step_index: int, *, reason: str
    ) -> SagaRecord:
        """Mark all PENDING/RUNNING steps after *failed_step_index* as BLOCKED
        then abort the saga.

        The failed step is expected to already be in FAILED phase before this
        helper is called.
        """
        for step_state in saga.step_states.values():
            if step_state.step_index != failed_step_index and step_state.phase in (
                StepPhase.PENDING,
                StepPhase.RUNNING,
            ):
                step_state.phase = StepPhase.BLOCKED
        return self._abort(saga, reason=reason)
