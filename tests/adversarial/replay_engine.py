"""Replay Engine v1.0

Loads lifecycle and decision JSONL streams, sorts events by causal ordering,
and replays them through invariant checkers for deterministic verification.

Sorting order:
  1. Causal DAG (caused_by_event_id chains)
  2. Lamport sequence (secondary)
  3. ts_wall_utc (tertiary)
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    """Result of a replay pass."""

    passed: bool
    violations: List[str] = field(default_factory=list)
    events_processed: int = 0
    final_state: Dict[str, Any] = field(default_factory=dict)


class ReplayEngine:
    """Loads and replays JSONL event streams for invariant checking."""

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []

    def load_streams(
        self,
        lifecycle_dir: Path,
        decisions_dir: Optional[Path] = None,
    ) -> int:
        """Load events from JSONL directories. Returns total events loaded."""
        self._events.clear()
        lifecycle_dir = Path(lifecycle_dir)
        if lifecycle_dir.exists():
            for jsonl_file in sorted(lifecycle_dir.glob("*.jsonl")):
                self._load_file(jsonl_file, stream="lifecycle")
        if decisions_dir is not None:
            decisions_dir = Path(decisions_dir)
            if decisions_dir.exists():
                for jsonl_file in sorted(decisions_dir.glob("*.jsonl")):
                    self._load_file(jsonl_file, stream="decisions")
        return len(self._events)

    def _load_file(self, path: Path, stream: str) -> None:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # Strip checksums (internal field)
                    record.pop("_checksum", None)
                    record["_stream"] = stream
                    self._events.append(record)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed JSONL line in %s", path)

    def sort_events(self) -> None:
        """Sort events by: causal DAG (topological), Lamport sequence, wall clock."""
        # Build causal ordering via topological sort
        event_map: Dict[str, Dict[str, Any]] = {}
        for e in self._events:
            envelope = e.get("envelope", {})
            eid = envelope.get("event_id", "")
            if eid:
                event_map[eid] = e

        # Topological sort using Kahn's algorithm
        in_degree: Dict[str, int] = {eid: 0 for eid in event_map}
        children: Dict[str, List[str]] = {eid: [] for eid in event_map}

        for eid, e in event_map.items():
            envelope = e.get("envelope", {})
            caused_by = envelope.get("caused_by_event_id")
            if caused_by and caused_by in event_map:
                in_degree[eid] += 1
                children[caused_by].append(eid)

        # Sort within each topological level by (sequence, ts_wall_utc)
        def _sort_key(eid: str) -> tuple:
            e = event_map[eid]
            envelope = e.get("envelope", {})
            return (
                envelope.get("sequence", 0),
                envelope.get("ts_wall_utc", 0.0),
            )

        queue = sorted(
            [eid for eid, deg in in_degree.items() if deg == 0],
            key=_sort_key,
        )
        sorted_events: List[Dict[str, Any]] = []

        while queue:
            eid = queue.pop(0)
            sorted_events.append(event_map[eid])
            for child in sorted(children[eid], key=_sort_key):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
                    queue.sort(key=_sort_key)

        # Add events without event_ids at the end
        orphans = [e for e in self._events if not e.get("envelope", {}).get("event_id")]
        self._events = sorted_events + orphans

    def replay(
        self,
        checker: Optional[Callable] = None,
    ) -> ReplayResult:
        """Replay all loaded events. If checker provided, call it per event."""
        self.sort_events()
        violations: List[str] = []
        state: Dict[str, Any] = {
            "phases_entered": [],
            "phases_exited": [],
            "phases_failed": [],
            "boot_started": False,
            "boot_completed": False,
            "events_seen": 0,
        }

        for event in self._events:
            event_type = event.get("event_type", "")
            phase = event.get("phase", "")

            if event_type == "boot_start":
                state["boot_started"] = True
            elif event_type == "boot_complete":
                state["boot_completed"] = True
            elif event_type == "phase_enter":
                state["phases_entered"].append(phase)
            elif event_type == "phase_exit":
                state["phases_exited"].append(phase)
            elif event_type == "phase_fail":
                state["phases_failed"].append(phase)

            state["events_seen"] += 1

            if checker is not None:
                v = checker(event, state)
                if v:
                    violations.extend(v)

        return ReplayResult(
            passed=len(violations) == 0,
            violations=violations,
            events_processed=len(self._events),
            final_state=dict(state),
        )

    @property
    def events(self) -> List[Dict[str, Any]]:
        """Access loaded events (read-only copy)."""
        return list(self._events)
