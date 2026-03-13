"""Durable storage for L3 execution-graph scheduler state."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    GraphExecutionPhase,
    GraphExecutionState,
    graph_state_from_dict,
    graph_state_to_dict,
)

logger = logging.getLogger("Ouroboros.ExecutionGraphStore")


class ExecutionGraphStore:
    """Persist execution-graph state using atomic file replacement."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, graph_id: str) -> Path:
        """Return the persistence path for an execution graph."""
        return self._state_dir / f"graph_{graph_id}.json"

    def load_inflight(self) -> Dict[str, GraphExecutionState]:
        """Load all non-terminal execution graphs from storage."""
        inflight: Dict[str, GraphExecutionState] = {}
        for path in self._state_dir.glob("graph_*.json"):
            state = self._read(path)
            if state is None:
                continue
            if state.phase in (
                GraphExecutionPhase.COMPLETED,
                GraphExecutionPhase.FAILED,
                GraphExecutionPhase.CANCELLED,
            ):
                continue
            inflight[state.graph_id] = state
        return inflight

    def get(self, graph_id: str) -> Optional[GraphExecutionState]:
        """Load one graph state from disk, returning None if unreadable."""
        return self._read(self.path_for(graph_id))

    def save(self, state: GraphExecutionState) -> None:
        """Persist a graph state atomically."""
        payload = graph_state_to_dict(state)
        target = self.path_for(state.graph_id)
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.stem}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    def mark_terminal(self, graph_id: str, terminal_state: str) -> None:
        """Update a persisted graph to a terminal state when possible."""
        state = self.get(graph_id)
        if state is None:
            return
        try:
            phase = GraphExecutionPhase(terminal_state)
        except ValueError:
            logger.warning(
                "[ExecutionGraphStore] Unknown terminal_state=%s for graph_id=%s",
                terminal_state,
                graph_id,
            )
            return
        updated = GraphExecutionState(
            graph=state.graph,
            phase=phase,
            ready_units=state.ready_units,
            running_units=(),
            completed_units=state.completed_units,
            failed_units=state.failed_units,
            cancelled_units=state.cancelled_units,
            results=state.results,
            last_error=state.last_error,
        )
        self.save(updated)

    def _read(self, path: Path) -> Optional[GraphExecutionState]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return graph_state_from_dict(data)
        except Exception as exc:
            logger.warning(
                "[ExecutionGraphStore] Failed to load %s: %s",
                path.name,
                exc,
            )
            return None
