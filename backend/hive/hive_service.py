"""
Hive Service — Top-Level Orchestrator for the Autonomous Engineering Hive
=========================================================================

Wires all Hive components together:
  - CognitiveFsm       — compute governance state machine
  - ThreadManager       — thread lifecycle, persistence, budget enforcement
  - HiveModelRouter     — cognitive-state-aware model selection
  - PersonaEngine       — LLM-powered Trinity persona reasoning
  - HudRelayAgent       — bus-to-IPC projection for the HUD

Subscribes to the AgentCommunicationBus for HIVE_AGENT_LOG messages,
creates threads, runs Trinity debates (observe -> propose -> validate),
and hands off consensus to the Ouroboros governance pipeline.

Background REM polling periodically checks whether conditions allow
cheap triage cycles.

Public surface:
  - HiveService.start()   — subscribe to bus, begin REM polling
  - HiveService.stop()    — cancel REM task, persist active threads
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from backend.hive.cognitive_fsm import CognitiveEvent, CognitiveFsm
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.model_router import HiveModelRouter
from backend.hive.ouroboros_handoff import serialize_consensus
from backend.hive.persona_engine import PersonaEngine
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read from environment at import time
# ---------------------------------------------------------------------------

_MAX_REJECTS: int = int(os.environ.get("JARVIS_HIVE_MAX_REJECTS", "2"))
_REM_POLL_INTERVAL_S: float = float(
    os.environ.get("JARVIS_HIVE_REM_POLL_INTERVAL_S", "1800")
)
_OUROBOROS_MODE: str = os.environ.get("JARVIS_HIVE_OUROBOROS_MODE", "autonomous")

# Regex for extracting file paths from reasoning text
_FILE_PATH_RE = re.compile(r"(?:^|\s)((?:backend|frontend|tests|scripts)/[\w/]+\.py)\b")


# ---------------------------------------------------------------------------
# HiveService
# ---------------------------------------------------------------------------


class HiveService:
    """Top-level orchestrator that wires all Hive components together.

    Parameters
    ----------
    bus:
        AgentCommunicationBus instance (must expose ``subscribe_broadcast``).
    governed_loop:
        GovernedLoopService instance (must expose ``submit``).
    doubleword:
        Doubleword client instance (must expose ``prompt_only``).
    state_dir:
        Optional directory for persistent state.  Defaults to
        ``~/.jarvis/hive``.
    """

    def __init__(
        self,
        bus: Any,
        governed_loop: Any,
        doubleword: Any,
        state_dir: Optional[Path] = None,
    ) -> None:
        self._bus = bus
        self._governed_loop = governed_loop
        self._doubleword = doubleword

        resolved_state_dir = state_dir or Path(
            os.environ.get(
                "JARVIS_HIVE_STATE_DIR",
                str(Path.home() / ".jarvis" / "hive"),
            )
        )

        # Core components
        self._fsm = CognitiveFsm(
            state_file=resolved_state_dir / "cognitive_state.json"
        )
        self._thread_mgr = ThreadManager(
            storage_dir=resolved_state_dir / "threads"
        )
        self._model_router = HiveModelRouter()
        self._persona_engine = PersonaEngine(
            doubleword=doubleword,
            model_router=self._model_router,
        )
        self._relay = HudRelayAgent()

        # Runtime state
        self._flow_thread_ids: Set[str] = set()
        self._running: bool = False
        self._rem_task: Optional[asyncio.Task] = None
        self._last_activity_mono: float = time.monotonic()

    # ------------------------------------------------------------------
    # Properties (for testing / introspection)
    # ------------------------------------------------------------------

    @property
    def fsm(self) -> CognitiveFsm:
        return self._fsm

    @property
    def thread_manager(self) -> ThreadManager:
        return self._thread_mgr

    @property
    def relay(self) -> HudRelayAgent:
        return self._relay

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the bus and begin background REM polling."""
        self._running = True
        await self._bus.subscribe_broadcast(
            MessageType.HIVE_AGENT_LOG, self._on_agent_log
        )
        self._rem_task = asyncio.create_task(self._rem_poll_loop())
        logger.info("HiveService started (ouroboros_mode=%s)", _OUROBOROS_MODE)

    async def stop(self) -> None:
        """Cancel REM task and persist all active threads."""
        self._running = False
        if self._rem_task is not None:
            self._rem_task.cancel()
            try:
                await self._rem_task
            except asyncio.CancelledError:
                pass
            self._rem_task = None

        # Persist active threads
        for thread_id in list(self._thread_mgr.active_threads):
            try:
                self._thread_mgr.persist_thread(thread_id)
            except Exception:
                logger.warning(
                    "Failed to persist thread %s on shutdown",
                    thread_id,
                    exc_info=True,
                )

        logger.info("HiveService stopped")

    # ------------------------------------------------------------------
    # Bus handler
    # ------------------------------------------------------------------

    async def _on_agent_log(self, message: AgentMessage) -> None:
        """Handle an incoming HIVE_AGENT_LOG message from the bus.

        Creates or finds a thread, adds the AgentLogMessage, projects
        it via the HUD relay, and escalates to FLOW + debate when
        severity warrants.
        """
        self._last_activity_mono = time.monotonic()

        payload = message.payload
        agent_name = payload.get("agent_name", message.from_agent or "unknown")
        severity = payload.get("severity", "info")
        category = payload.get("category", "general")
        trinity_parent = payload.get("trinity_parent", "jarvis")

        # Find or create a thread for this category/agent
        thread = self._find_or_create_thread(category, agent_name)

        # Build the AgentLogMessage
        log_msg = AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name=agent_name,
            trinity_parent=trinity_parent,
            severity=severity,
            category=category,
            payload=payload.get("data", payload),
        )

        # Add to thread + project to HUD
        self._thread_mgr.add_message(thread.thread_id, log_msg)
        await self._relay.project_message(log_msg)

        # Escalate: warning/error/critical in BASELINE -> FLOW + debate
        if severity in ("warning", "error", "critical") and self._fsm.state == CognitiveState.BASELINE:
            decision = self._fsm.decide(CognitiveEvent.FLOW_TRIGGER)
            self._fsm.apply_last_decision()
            if not decision.noop:
                await self._relay.project_cognitive_transition(
                    from_state=decision.from_state.value,
                    to_state=decision.to_state.value,
                    reason_code=decision.reason_code,
                )

        # If thread is OPEN and FSM is in FLOW, start debate
        if thread.state == ThreadState.OPEN and self._fsm.state == CognitiveState.FLOW:
            self._thread_mgr.transition(thread.thread_id, ThreadState.DEBATING)
            self._flow_thread_ids.add(thread.thread_id)
            asyncio.create_task(self._run_debate_round(thread.thread_id))

    # ------------------------------------------------------------------
    # Debate loop
    # ------------------------------------------------------------------

    async def _run_debate_round(self, thread_id: str) -> None:
        """Run the Trinity debate loop for a thread.

        1. JARVIS observe (always)
        2. J-Prime propose
        3. Reactor validate
           - approve -> CONSENSUS -> _handle_consensus()
           - reject  -> increment reject_count, loop back to step 2
           - reject_count >= MAX_REJECTS -> STALE
        After each LLM call: check_and_advance() for budget exhaustion.
        """
        thread = self._thread_mgr.get_thread(thread_id)
        if thread is None:
            logger.warning("Debate aborted: thread %s not found", thread_id)
            return

        reject_count = 0

        # Step 1: JARVIS observe
        observe_msg = await self._persona_engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, thread
        )
        self._thread_mgr.add_message(thread_id, observe_msg)
        await self._relay.project_message(observe_msg)

        # Check budget after observe
        advanced = self._thread_mgr.check_and_advance(thread_id)
        if advanced is not None:
            await self._relay.project_lifecycle(
                thread_id, advanced.value
            )
            if advanced in (ThreadState.STALE, ThreadState.CONSENSUS):
                self._flow_thread_ids.discard(thread_id)
                await self._check_flow_completion()
            return

        # Propose-validate loop
        while reject_count < _MAX_REJECTS:
            # Step 2: J-Prime propose
            propose_msg = await self._persona_engine.generate_reasoning(
                "j_prime", PersonaIntent.PROPOSE, thread
            )
            self._thread_mgr.add_message(thread_id, propose_msg)
            await self._relay.project_message(propose_msg)

            # Check budget after propose
            advanced = self._thread_mgr.check_and_advance(thread_id)
            if advanced is not None:
                await self._relay.project_lifecycle(
                    thread_id, advanced.value
                )
                if advanced in (ThreadState.STALE, ThreadState.CONSENSUS):
                    self._flow_thread_ids.discard(thread_id)
                    await self._check_flow_completion()
                return

            # Step 3: Reactor validate
            validate_msg = await self._persona_engine.generate_reasoning(
                "reactor", PersonaIntent.VALIDATE, thread
            )
            self._thread_mgr.add_message(thread_id, validate_msg)
            await self._relay.project_message(validate_msg)

            # Check budget after validate
            advanced = self._thread_mgr.check_and_advance(thread_id)
            if advanced is not None:
                await self._relay.project_lifecycle(
                    thread_id, advanced.value
                )
                if advanced == ThreadState.CONSENSUS:
                    await self._handle_consensus(thread_id)
                elif advanced == ThreadState.STALE:
                    self._flow_thread_ids.discard(thread_id)
                    await self._check_flow_completion()
                return

            # Evaluate verdict
            if validate_msg.validate_verdict == "approve":
                # Consensus reached — transition handled by check_and_advance
                # or we do it manually here if budget check didn't fire
                self._thread_mgr.transition(thread_id, ThreadState.CONSENSUS)
                await self._relay.project_lifecycle(
                    thread_id, ThreadState.CONSENSUS.value
                )
                await self._handle_consensus(thread_id)
                return

            # Rejection
            reject_count += 1
            logger.info(
                "Thread %s: reactor rejected (%d/%d)",
                thread_id,
                reject_count,
                _MAX_REJECTS,
            )

        # Max rejects reached — mark STALE
        self._thread_mgr.transition(thread_id, ThreadState.STALE)
        await self._relay.project_lifecycle(
            thread_id, ThreadState.STALE.value
        )
        self._flow_thread_ids.discard(thread_id)
        await self._check_flow_completion()

    # ------------------------------------------------------------------
    # Consensus handoff
    # ------------------------------------------------------------------

    async def _handle_consensus(self, thread_id: str) -> None:
        """Serialize consensus and submit to the Ouroboros governance pipeline."""
        thread = self._thread_mgr.get_thread(thread_id)
        if thread is None:
            return

        target_files = tuple(self._extract_target_files(thread))

        ctx = serialize_consensus(thread, target_files=target_files)

        result = await self._governed_loop.submit(
            ctx, trigger_source="hive_consensus"
        )

        # Link the operation ID back to the thread
        op_id = getattr(result, "op_id", None) or (
            result.get("op_id") if isinstance(result, dict) else str(result)
        )
        thread.linked_op_id = op_id

        # Transition to EXECUTING
        self._thread_mgr.transition(thread_id, ThreadState.EXECUTING)
        await self._relay.project_lifecycle(
            thread_id,
            ThreadState.EXECUTING.value,
            metadata={"linked_op_id": op_id},
        )

        # Discard from flow set and check completion
        self._flow_thread_ids.discard(thread_id)
        await self._check_flow_completion()

    # ------------------------------------------------------------------
    # Flow completion
    # ------------------------------------------------------------------

    async def _check_flow_completion(self) -> None:
        """If FSM is FLOW and all flow threads are resolved, spin down."""
        if self._fsm.state == CognitiveState.FLOW and not self._flow_thread_ids:
            decision = self._fsm.decide(
                CognitiveEvent.SPINDOWN,
                spindown_reason="pr_merged",
            )
            self._fsm.apply_last_decision()
            if not decision.noop:
                await self._relay.project_cognitive_transition(
                    from_state=decision.from_state.value,
                    to_state=decision.to_state.value,
                    reason_code=decision.reason_code,
                )
                logger.info(
                    "All flow threads resolved — cognitive FSM spun down to %s",
                    decision.to_state.value,
                )

    # ------------------------------------------------------------------
    # REM polling
    # ------------------------------------------------------------------

    async def _rem_poll_loop(self) -> None:
        """Background loop that periodically checks REM eligibility."""
        while self._running:
            try:
                await asyncio.sleep(_REM_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return

            if self._fsm.state != CognitiveState.BASELINE:
                continue

            idle_seconds = time.monotonic() - self._last_activity_mono
            decision = self._fsm.decide(
                CognitiveEvent.REM_TRIGGER,
                idle_seconds=idle_seconds,
                system_load_pct=0.0,
            )
            if not decision.noop:
                self._fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    from_state=decision.from_state.value,
                    to_state=decision.to_state.value,
                    reason_code=decision.reason_code,
                )
                logger.info("REM cycle triggered after %.0fs idle", idle_seconds)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_or_create_thread(
        self, category: str, agent_name: str
    ) -> Any:
        """Find an existing OPEN or DEBATING thread for *category*, or create one.

        Returns the :class:`HiveThread`.
        """
        for thread in self._thread_mgr.active_threads.values():
            if (
                thread.state in (ThreadState.OPEN, ThreadState.DEBATING)
                and thread.trigger_event == category
            ):
                return thread

        # Create new thread
        return self._thread_mgr.create_thread(
            title=f"{category} issue from {agent_name}",
            trigger_event=category,
            cognitive_state=self._fsm.state,
        )

    def _extract_target_files(self, thread: Any) -> list:
        """Heuristic: find paths like 'backend/foo/bar.py' in persona reasoning text."""
        files: list = []
        seen: set = set()
        for msg in thread.messages:
            if isinstance(msg, PersonaReasoningMessage):
                for match in _FILE_PATH_RE.findall(msg.reasoning):
                    if match not in seen:
                        seen.add(match)
                        files.append(match)
        return files
