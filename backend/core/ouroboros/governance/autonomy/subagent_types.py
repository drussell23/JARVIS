"""Typed contracts for L3 execution graphs and work-unit persistence."""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)


class WorkUnitState(str, Enum):
    """Lifecycle states for an execution-graph work unit."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GraphExecutionPhase(str, Enum):
    """Lifecycle states for an execution graph."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkUnitSpec:
    """Single-repo work item inside an execution graph."""

    unit_id: str
    repo: str
    goal: str
    target_files: Tuple[str, ...]
    dependency_ids: Tuple[str, ...] = ()
    owned_paths: Tuple[str, ...] = ()
    barrier_id: str = ""
    max_attempts: int = 1
    timeout_s: float = 180.0
    acceptance_tests: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("WorkUnitSpec.unit_id is required")
        if not self.repo:
            raise ValueError(f"WorkUnitSpec[{self.unit_id}] repo is required")
        if not self.goal:
            raise ValueError(f"WorkUnitSpec[{self.unit_id}] goal is required")
        if not self.target_files:
            raise ValueError(f"WorkUnitSpec[{self.unit_id}] target_files must be non-empty")
        if self.max_attempts < 1:
            raise ValueError(f"WorkUnitSpec[{self.unit_id}] max_attempts must be >= 1")
        if self.timeout_s <= 0.0:
            raise ValueError(f"WorkUnitSpec[{self.unit_id}] timeout_s must be > 0")

    @property
    def effective_owned_paths(self) -> Tuple[str, ...]:
        """Return owned paths, defaulting to target_files when unspecified."""
        return self.owned_paths or self.target_files


def _validate_unit_dag(units: Tuple[WorkUnitSpec, ...]) -> None:
    """Validate uniqueness and acyclicity for a work-unit DAG."""
    if not units:
        raise ValueError("ExecutionGraph.units must be non-empty")

    unit_map = {unit.unit_id: unit for unit in units}
    if len(unit_map) != len(units):
        raise ValueError("ExecutionGraph contains duplicate unit_id values")

    for unit in units:
        missing = [dep for dep in unit.dependency_ids if dep not in unit_map]
        if missing:
            raise ValueError(
                f"WorkUnitSpec[{unit.unit_id}] references unknown dependency_ids={sorted(missing)}"
            )

    graph: Dict[str, List[str]] = collections.defaultdict(list)
    in_degree: Dict[str, int] = collections.defaultdict(int)
    for unit in units:
        for dep in unit.dependency_ids:
            graph[dep].append(unit.unit_id)
            in_degree[unit.unit_id] += 1
        in_degree.setdefault(unit.unit_id, 0)

    queue = collections.deque(sorted(uid for uid, deg in in_degree.items() if deg == 0))
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in sorted(graph.get(node, ())):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(units):
        cyclic = sorted(uid for uid, deg in in_degree.items() if deg > 0)
        raise ValueError(f"ExecutionGraph dependency cycle detected: {cyclic}")


@dataclass(frozen=True)
class ExecutionGraph:
    """Parallel execution plan emitted by the L3 planner."""

    graph_id: str
    op_id: str
    planner_id: str
    schema_version: str
    units: Tuple[WorkUnitSpec, ...]
    concurrency_limit: int
    plan_digest: str = ""
    causal_trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.graph_id:
            raise ValueError("ExecutionGraph.graph_id is required")
        if not self.op_id:
            raise ValueError("ExecutionGraph.op_id is required")
        if not self.planner_id:
            raise ValueError("ExecutionGraph.planner_id is required")
        if not self.schema_version:
            raise ValueError("ExecutionGraph.schema_version is required")
        if self.concurrency_limit < 1:
            raise ValueError("ExecutionGraph.concurrency_limit must be >= 1")
        _validate_unit_dag(self.units)

        if not self.plan_digest:
            object.__setattr__(self, "plan_digest", self._compute_plan_digest())

        if not self.causal_trace_id:
            object.__setattr__(
                self,
                "causal_trace_id",
                f"{self.graph_id}:{self.plan_digest[:12]}",
            )

    def _compute_plan_digest(self) -> str:
        raw = {
            "graph_id": self.graph_id,
            "op_id": self.op_id,
            "planner_id": self.planner_id,
            "schema_version": self.schema_version,
            "concurrency_limit": self.concurrency_limit,
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "repo": unit.repo,
                    "goal": unit.goal,
                    "target_files": unit.target_files,
                    "dependency_ids": unit.dependency_ids,
                    "owned_paths": unit.effective_owned_paths,
                    "barrier_id": unit.barrier_id,
                    "max_attempts": unit.max_attempts,
                    "timeout_s": unit.timeout_s,
                    "acceptance_tests": unit.acceptance_tests,
                }
                for unit in self.units
            ],
        }
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def unit_map(self) -> Dict[str, WorkUnitSpec]:
        return {unit.unit_id: unit for unit in self.units}


@dataclass(frozen=True)
class WorkUnitResult:
    """Terminal result for a work unit."""

    unit_id: str
    repo: str
    status: WorkUnitState
    patch: Optional[RepoPatch]
    attempt_count: int
    started_at_ns: int
    finished_at_ns: int
    failure_class: str = ""
    error: str = ""
    causal_parent_id: str = ""

    def __post_init__(self) -> None:
        if self.finished_at_ns < self.started_at_ns:
            raise ValueError("WorkUnitResult.finished_at_ns must be >= started_at_ns")
        if self.attempt_count < 0:
            raise ValueError("WorkUnitResult.attempt_count must be >= 0")


@dataclass(frozen=True)
class MergeDecision:
    """Deterministic merge boundary decision for a repo/barrier pair."""

    graph_id: str
    barrier_id: str
    repo: str
    merged_unit_ids: Tuple[str, ...]
    skipped_unit_ids: Tuple[str, ...]
    conflict_units: Tuple[str, ...]
    decision_hash: str


@dataclass(frozen=True)
class GraphExecutionState:
    """Durable scheduler state for an execution graph."""

    graph: ExecutionGraph
    phase: GraphExecutionPhase = GraphExecutionPhase.CREATED
    ready_units: Tuple[str, ...] = ()
    running_units: Tuple[str, ...] = ()
    completed_units: Tuple[str, ...] = ()
    failed_units: Tuple[str, ...] = ()
    cancelled_units: Tuple[str, ...] = ()
    results: Dict[str, WorkUnitResult] = field(default_factory=dict)
    last_error: str = ""
    updated_at_ns: int = field(default_factory=time.monotonic_ns)
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.checksum:
            object.__setattr__(self, "checksum", self.compute_checksum())

    @property
    def graph_id(self) -> str:
        return self.graph.graph_id

    @property
    def op_id(self) -> str:
        return self.graph.op_id

    def compute_checksum(self) -> str:
        raw = {
            "graph_id": self.graph.graph_id,
            "phase": self.phase.value,
            "ready_units": sorted(self.ready_units),
            "running_units": sorted(self.running_units),
            "completed_units": sorted(self.completed_units),
            "failed_units": sorted(self.failed_units),
            "cancelled_units": sorted(self.cancelled_units),
            "results": sorted(self.results.keys()),
            "last_error": self.last_error,
        }
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _bytes_to_text(value: Optional[bytes]) -> Optional[str]:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _text_to_bytes(value: Optional[str]) -> Optional[bytes]:
    if value in (None, ""):
        return None if value is None else b""
    return base64.b64decode(value.encode("ascii"))


def repo_patch_to_dict(patch: RepoPatch) -> Dict[str, Any]:
    """Serialize RepoPatch for durable execution-graph state."""
    return {
        "repo": patch.repo,
        "files": [
            {
                "path": pf.path,
                "op": pf.op.value,
                "preimage": _bytes_to_text(pf.preimage),
            }
            for pf in patch.files
        ],
        "new_content": [
            {"path": path, "content": _bytes_to_text(content) or ""}
            for path, content in patch.new_content
        ],
    }


def repo_patch_from_dict(data: Dict[str, Any]) -> RepoPatch:
    """Deserialize RepoPatch from durable execution-graph state."""
    files = tuple(
        PatchedFile(
            path=str(entry["path"]),
            op=FileOp(str(entry["op"])),
            preimage=_text_to_bytes(entry.get("preimage")),
        )
        for entry in data.get("files", [])
    )
    new_content = tuple(
        (str(entry["path"]), _text_to_bytes(entry.get("content")) or b"")
        for entry in data.get("new_content", [])
    )
    return RepoPatch(repo=str(data["repo"]), files=files, new_content=new_content)


def work_unit_result_to_dict(result: WorkUnitResult) -> Dict[str, Any]:
    """Serialize WorkUnitResult for durable execution-graph state."""
    return {
        "unit_id": result.unit_id,
        "repo": result.repo,
        "status": result.status.value,
        "patch": repo_patch_to_dict(result.patch) if result.patch is not None else None,
        "attempt_count": result.attempt_count,
        "started_at_ns": result.started_at_ns,
        "finished_at_ns": result.finished_at_ns,
        "failure_class": result.failure_class,
        "error": result.error,
        "causal_parent_id": result.causal_parent_id,
    }


def work_unit_result_from_dict(data: Dict[str, Any]) -> WorkUnitResult:
    """Deserialize WorkUnitResult from durable execution-graph state."""
    patch_data = data.get("patch")
    patch = repo_patch_from_dict(patch_data) if isinstance(patch_data, dict) else None
    return WorkUnitResult(
        unit_id=str(data["unit_id"]),
        repo=str(data["repo"]),
        status=WorkUnitState(str(data["status"])),
        patch=patch,
        attempt_count=int(data.get("attempt_count", 0)),
        started_at_ns=int(data.get("started_at_ns", 0)),
        finished_at_ns=int(data.get("finished_at_ns", 0)),
        failure_class=str(data.get("failure_class", "")),
        error=str(data.get("error", "")),
        causal_parent_id=str(data.get("causal_parent_id", "")),
    )


def execution_graph_to_dict(graph: ExecutionGraph) -> Dict[str, Any]:
    """Serialize ExecutionGraph for persistence."""
    return {
        "graph_id": graph.graph_id,
        "op_id": graph.op_id,
        "planner_id": graph.planner_id,
        "schema_version": graph.schema_version,
        "concurrency_limit": graph.concurrency_limit,
        "plan_digest": graph.plan_digest,
        "causal_trace_id": graph.causal_trace_id,
        "units": [
            {
                "unit_id": unit.unit_id,
                "repo": unit.repo,
                "goal": unit.goal,
                "target_files": list(unit.target_files),
                "dependency_ids": list(unit.dependency_ids),
                "owned_paths": list(unit.owned_paths),
                "barrier_id": unit.barrier_id,
                "max_attempts": unit.max_attempts,
                "timeout_s": unit.timeout_s,
                "acceptance_tests": list(unit.acceptance_tests),
            }
            for unit in graph.units
        ],
    }


def execution_graph_from_dict(data: Dict[str, Any]) -> ExecutionGraph:
    """Deserialize ExecutionGraph from persistence."""
    units = tuple(
        WorkUnitSpec(
            unit_id=str(unit["unit_id"]),
            repo=str(unit["repo"]),
            goal=str(unit["goal"]),
            target_files=tuple(unit.get("target_files", ())),
            dependency_ids=tuple(unit.get("dependency_ids", ())),
            owned_paths=tuple(unit.get("owned_paths", ())),
            barrier_id=str(unit.get("barrier_id", "")),
            max_attempts=int(unit.get("max_attempts", 1)),
            timeout_s=float(unit.get("timeout_s", 180.0)),
            acceptance_tests=tuple(unit.get("acceptance_tests", ())),
        )
        for unit in data.get("units", [])
    )
    return ExecutionGraph(
        graph_id=str(data["graph_id"]),
        op_id=str(data["op_id"]),
        planner_id=str(data["planner_id"]),
        schema_version=str(data["schema_version"]),
        units=units,
        concurrency_limit=int(data["concurrency_limit"]),
        plan_digest=str(data.get("plan_digest", "")),
        causal_trace_id=str(data.get("causal_trace_id", "")),
    )


def graph_state_to_dict(state: GraphExecutionState) -> Dict[str, Any]:
    """Serialize GraphExecutionState for durable storage."""
    return {
        "graph": execution_graph_to_dict(state.graph),
        "phase": state.phase.value,
        "ready_units": list(state.ready_units),
        "running_units": list(state.running_units),
        "completed_units": list(state.completed_units),
        "failed_units": list(state.failed_units),
        "cancelled_units": list(state.cancelled_units),
        "results": {uid: work_unit_result_to_dict(result) for uid, result in state.results.items()},
        "last_error": state.last_error,
        "updated_at_ns": state.updated_at_ns,
        "checksum": state.checksum,
    }


def graph_state_from_dict(data: Dict[str, Any]) -> GraphExecutionState:
    """Deserialize GraphExecutionState from durable storage."""
    graph = execution_graph_from_dict(data["graph"])
    results = {
        str(uid): work_unit_result_from_dict(result)
        for uid, result in data.get("results", {}).items()
    }
    return GraphExecutionState(
        graph=graph,
        phase=GraphExecutionPhase(str(data.get("phase", GraphExecutionPhase.CREATED.value))),
        ready_units=tuple(data.get("ready_units", ())),
        running_units=tuple(data.get("running_units", ())),
        completed_units=tuple(data.get("completed_units", ())),
        failed_units=tuple(data.get("failed_units", ())),
        cancelled_units=tuple(data.get("cancelled_units", ())),
        results=results,
        last_error=str(data.get("last_error", "")),
        updated_at_ns=int(data.get("updated_at_ns", time.monotonic_ns())),
        checksum=str(data.get("checksum", "")),
    )
