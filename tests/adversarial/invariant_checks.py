"""Invariant checkers for adversarial replay testing.

Each check function takes a lifecycle directory (Path) and returns
a list of violation strings. Empty list = all checks passed.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def _load_lifecycle_events(lifecycle_dir: Path) -> List[Dict[str, Any]]:
    """Load all lifecycle events from JSONL files in directory."""
    events: List[Dict[str, Any]] = []
    lifecycle_dir = Path(lifecycle_dir)
    if not lifecycle_dir.exists():
        return events
    for jsonl_file in sorted(lifecycle_dir.glob("*.jsonl")):
        with open(jsonl_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    record.pop("_checksum", None)
                    events.append(record)
                except json.JSONDecodeError:
                    pass
    return events


# ---------------------------------------------------------------------------
# Invariant 1: No orphan lifecycle phases
# ---------------------------------------------------------------------------

def check_no_orphan_phases(lifecycle_dir: Path) -> List[str]:
    """Every phase_enter must have a matching phase_exit or phase_fail."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    entered: Set[str] = set()
    completed: Set[str] = set()

    for event in events:
        event_type = event.get("event_type", "")
        phase = event.get("phase", "")
        if event_type == "phase_enter" and phase:
            entered.add(phase)
        elif event_type in ("phase_exit", "phase_fail") and phase:
            completed.add(phase)

    orphans = entered - completed
    for phase in sorted(orphans):
        violations.append(
            f"Orphan phase: '{phase}' was entered but never exited or failed"
        )
    return violations


# ---------------------------------------------------------------------------
# Invariant 2: Phase DAG consistency
# ---------------------------------------------------------------------------

# Declared phase order (each phase requires all previous phases to complete)
_PHASE_ORDER = [
    "clean_slate",
    "preflight",
    "resources",
    "backend",
    "intelligence",
    "trinity",
    "enterprise",
]


def check_phase_dag_consistency(lifecycle_dir: Path) -> List[str]:
    """Phase transitions must respect the declared dependency DAG."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    completed_phases: List[str] = []

    for event in events:
        event_type = event.get("event_type", "")
        phase = event.get("phase", "")

        if event_type == "phase_enter" and phase in _PHASE_ORDER:
            idx = _PHASE_ORDER.index(phase)
            # All previous phases should be completed
            for required in _PHASE_ORDER[:idx]:
                if required not in completed_phases:
                    violations.append(
                        f"Phase '{phase}' entered before '{required}' completed"
                    )

        if event_type in ("phase_exit", "phase_fail") and phase in _PHASE_ORDER:
            if phase not in completed_phases:
                completed_phases.append(phase)

    return violations


# ---------------------------------------------------------------------------
# Invariant 3: No duplicate side-effects (idempotency)
# ---------------------------------------------------------------------------

def check_no_duplicate_side_effects(lifecycle_dir: Path) -> List[str]:
    """No two events with the same idempotency_key should both succeed."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    seen_keys: Dict[str, str] = {}  # idempotency_key -> first event_id

    for event in events:
        envelope = event.get("envelope", {})
        key = envelope.get("idempotency_key")
        if not key:
            continue
        event_id = envelope.get("event_id", "unknown")
        status = event.get("to_state", event.get("status", ""))

        if status in ("success", "completed", ""):
            if key in seen_keys:
                violations.append(
                    f"Duplicate side-effect: idempotency_key '{key}' "
                    f"succeeded in both {seen_keys[key]} and {event_id}"
                )
            else:
                seen_keys[key] = event_id

    return violations


# ---------------------------------------------------------------------------
# Invariant 4: Causal chain integrity
# ---------------------------------------------------------------------------

def check_causal_chain_integrity(lifecycle_dir: Path) -> List[str]:
    """Every caused_by_event_id must reference an event that exists earlier."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    seen_event_ids: Set[str] = set()

    for event in events:
        envelope = event.get("envelope", {})
        event_id = envelope.get("event_id", "")
        caused_by = envelope.get("caused_by_event_id")

        if caused_by and caused_by not in seen_event_ids:
            violations.append(
                f"Broken causal chain: event '{event_id}' references "
                f"caused_by_event_id '{caused_by}' which hasn't been seen yet"
            )

        if event_id:
            seen_event_ids.add(event_id)

    return violations


# ---------------------------------------------------------------------------
# Invariant 5: Critical boundaries carry envelope
# ---------------------------------------------------------------------------

def check_critical_boundaries_have_envelope(events: List[Dict[str, Any]]) -> List[str]:
    """All events should have a non-empty envelope with trace_id."""
    violations: List[str] = []

    for i, event in enumerate(events):
        envelope = event.get("envelope", {})
        event_type = event.get("event_type", f"event_{i}")
        if not envelope:
            violations.append(f"Event '{event_type}' (index {i}) has no envelope")
        elif not envelope.get("trace_id"):
            violations.append(
                f"Event '{event_type}' (index {i}) has envelope without trace_id"
            )

    return violations


# ---------------------------------------------------------------------------
# Invariant 6: Lamport sequence monotonicity
# ---------------------------------------------------------------------------

def check_lamport_monotonic(lifecycle_dir: Path) -> List[str]:
    """Within a trace, Lamport sequences must be monotonically increasing."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    # Group by trace_id
    traces: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        envelope = event.get("envelope", {})
        trace_id = envelope.get("trace_id", "")
        if trace_id:
            traces.setdefault(trace_id, []).append(event)

    for trace_id, trace_events in traces.items():
        # Sort by timestamp to get temporal order
        trace_events.sort(key=lambda e: e.get("envelope", {}).get("ts_wall_utc", 0))
        last_seq = -1
        for event in trace_events:
            seq = event.get("envelope", {}).get("sequence", 0)
            if seq <= last_seq:
                event_id = event.get("envelope", {}).get("event_id", "?")
                violations.append(
                    f"Lamport non-monotonic in trace '{trace_id}': "
                    f"event '{event_id}' has sequence {seq} <= previous {last_seq}"
                )
            last_seq = seq

    return violations


# ---------------------------------------------------------------------------
# Invariant 7: Causality DAG is acyclic
# ---------------------------------------------------------------------------

def check_causality_acyclic(lifecycle_dir: Path) -> List[str]:
    """The causality graph (caused_by_event_id) must be a DAG (no cycles)."""
    events = _load_lifecycle_events(lifecycle_dir)
    violations: List[str] = []

    # Build adjacency: caused_by -> children
    edges: Dict[str, Optional[str]] = {}
    for event in events:
        envelope = event.get("envelope", {})
        event_id = envelope.get("event_id", "")
        caused_by = envelope.get("caused_by_event_id")
        if event_id:
            edges[event_id] = caused_by

    children_map: Dict[str, List[str]] = {}
    for event_id, caused_by in edges.items():
        if caused_by is not None:
            children_map.setdefault(caused_by, []).append(event_id)

    # DFS cycle detection
    visited: Set[str] = set()
    in_stack: Set[str] = set()

    def dfs(node: str, path: List[str]) -> None:
        if node in in_stack:
            cycle_start = path.index(node) if node in path else 0
            cycle = path[cycle_start:]
            violations.append(f"Causality cycle detected: {' -> '.join(cycle + [node])}")
            return
        if node in visited:
            return
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for child in children_map.get(node, []):
            dfs(child, path)
        path.pop()
        in_stack.remove(node)

    # Check self-references first
    for event_id, caused_by in edges.items():
        if caused_by == event_id:
            violations.append(f"Self-referencing causality: event '{event_id}'")
            visited.add(event_id)

    for node in edges:
        if node not in visited:
            dfs(node, [])

    return violations


# ---------------------------------------------------------------------------
# Invariant 8: Cross-repo envelope round-trip
# ---------------------------------------------------------------------------

def check_envelope_round_trip(envelope_dict: Dict[str, Any]) -> List[str]:
    """Envelope serialized and deserialized must preserve all fields."""
    from backend.core.trace_envelope import TraceEnvelope

    violations: List[str] = []
    try:
        env = TraceEnvelope.from_dict(envelope_dict)
        round_tripped = env.to_dict()
        for key in envelope_dict:
            if key not in round_tripped:
                violations.append(f"Field '{key}' lost during round-trip")
            elif round_tripped[key] != envelope_dict[key]:
                violations.append(
                    f"Field '{key}' changed: {envelope_dict[key]!r} -> {round_tripped[key]!r}"
                )
    except Exception as exc:
        violations.append(f"Round-trip failed with exception: {exc}")

    return violations
