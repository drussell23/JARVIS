"""
REM Graduation Auditor -- Module 2.

Scans the Ouroboros ledger for ephemeral tools eligible for graduation
(count >= 3 completed ops) and stale tools (not used in 30+ days).

Ledger format: ~/.jarvis/ouroboros/ledger/op-{id}-{repo}.jsonl
Each line: {"op_id": str, "state": str, "wall_time": float, "data": dict}
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

_GRADUATION_THRESHOLD = 3
_STRONG_SIGNAL_THRESHOLD = 5
_STALE_DAYS = int(os.environ.get("JARVIS_HIVE_TOOL_STALE_DAYS", "30"))


class GraduationAuditor:
    """Scans Ouroboros ledger for graduation candidates and stale tools.

    Parameters
    ----------
    persona_engine:
        :class:`PersonaEngine` (or compatible) for LLM-grounded reasoning.
    thread_manager:
        :class:`ThreadManager` for thread lifecycle.
    relay:
        :class:`HudRelayAgent` for IPC projection.
    ledger_dir:
        Path to the Ouroboros ledger directory.  Falls back to
        ``JARVIS_OUROBOROS_LEDGER_DIR`` env var, then
        ``~/.jarvis/ouroboros/ledger/``.
    """

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
        ledger_dir: Optional[Path] = None,
    ) -> None:
        self._engine = persona_engine
        self._tm = thread_manager
        self._relay = relay
        self._ledger_dir = ledger_dir or Path(
            os.environ.get(
                "JARVIS_OUROBOROS_LEDGER_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "ledger"),
            )
        )

    # ------------------------------------------------------------------
    # Public API (ReviewModule protocol)
    # ------------------------------------------------------------------

    async def run(
        self, budget: int
    ) -> Tuple[List[str], int, bool, Optional[str]]:
        """Run graduation audit.

        Returns
        -------
        tuple
            ``(thread_ids, calls_used, should_escalate, escalation_id)``
        """
        status_counts, stale_ops = self._scan_ledger()
        thread_ids: List[str] = []
        calls_used = 0
        should_escalate = False
        escalation_id: Optional[str] = None

        completed_count = status_counts.get("completed", 0)

        # --- Graduation candidates ---
        if completed_count >= _GRADUATION_THRESHOLD and calls_used < budget:
            thread = self._tm.create_thread(
                title=f"Graduation Candidates: {completed_count} completed ops",
                trigger_event="rem_graduation_auditor:candidates",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="graduation_auditor",
                trinity_parent="jarvis",
                severity="info",
                category="graduation",
                payload={
                    "completed_count": completed_count,
                    "threshold": _GRADUATION_THRESHOLD,
                },
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis",
                PersonaIntent.OBSERVE,
                thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1
            thread_ids.append(thread.thread_id)

            if completed_count >= _STRONG_SIGNAL_THRESHOLD:
                should_escalate = True
                escalation_id = thread.thread_id

        # --- Stale tools ---
        if stale_ops and calls_used < budget:
            thread = self._tm.create_thread(
                title=f"Stale Tools: {len(stale_ops)} ops unused >{_STALE_DAYS}d",
                trigger_event="rem_graduation_auditor:stale",
                cognitive_state=CognitiveState.REM,
            )
            log = AgentLogMessage(
                thread_id=thread.thread_id,
                agent_name="graduation_auditor",
                trinity_parent="jarvis",
                severity="info",
                category="stale_tools",
                payload={
                    "stale_count": len(stale_ops),
                    "threshold_days": _STALE_DAYS,
                },
            )
            self._tm.add_message(thread.thread_id, log)
            await self._relay.project_message(log)

            self._tm.transition(thread.thread_id, ThreadState.DEBATING)
            observe_msg = await self._engine.generate_reasoning(
                "jarvis",
                PersonaIntent.OBSERVE,
                thread,
            )
            self._tm.add_message(thread.thread_id, observe_msg)
            await self._relay.project_message(observe_msg)
            calls_used += 1
            thread_ids.append(thread.thread_id)

        return (thread_ids, calls_used, should_escalate, escalation_id)

    # ------------------------------------------------------------------
    # Ledger scanning
    # ------------------------------------------------------------------

    def _scan_ledger(self) -> Tuple[Dict[str, int], List[str]]:
        """Scan ledger directory for op status counts and stale op IDs.

        Returns
        -------
        tuple
            ``(status_counts, stale_op_ids)`` where *status_counts* maps
            state strings to their occurrence count (based on each file's
            latest entry by ``wall_time``), and *stale_op_ids* lists the
            stems of files whose latest ``wall_time`` is older than
            ``_STALE_DAYS``.
        """
        status_counts: Dict[str, int] = {}
        stale_ops: List[str] = []
        stale_cutoff = time.time() - (_STALE_DAYS * 86400)

        if not self._ledger_dir.exists():
            return (status_counts, stale_ops)

        for path in self._ledger_dir.glob("op-*.jsonl"):
            latest_wall_time = 0.0
            latest_state = "unknown"
            try:
                for line in path.read_text().strip().split("\n"):
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    state = entry.get("state", "unknown")
                    wt = entry.get("wall_time", 0.0)
                    if wt > latest_wall_time:
                        latest_wall_time = wt
                        latest_state = state
            except (json.JSONDecodeError, OSError):
                continue

            status_counts[latest_state] = status_counts.get(latest_state, 0) + 1
            if latest_wall_time > 0 and latest_wall_time < stale_cutoff:
                stale_ops.append(path.stem)

        return (status_counts, stale_ops)
