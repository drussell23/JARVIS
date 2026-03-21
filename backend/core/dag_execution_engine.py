"""DAG Execution Engine — Dependency-Aware Asynchronous Topological Executor.

Replaces sequential/polling-based workflow execution with a true concurrent
topological sort engine using asyncio.Event signaling.

Architecture:
    - Each node in the DAG is a coroutine waiting on asyncio.Events for its deps
    - When a dep completes, it signals its Event → dependents wake immediately
    - Independent nodes execute perfectly in parallel via asyncio.TaskGroup
    - Zero polling loops, zero sleep() calls, zero race conditions
    - Cross-repo context propagated via contextvars (not shared mutable state)

Invariants:
    - No node executes before ALL its dependencies have completed
    - Independent nodes execute with maximum parallelism
    - A failed node marks all transitive dependents as BLOCKED (no cascading exec)
    - Context is immutable per-node via contextvars.copy_context()
    - Cycle detection at plan time, not execution time (fail fast)
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context Variables — per-node isolated context for cross-repo safety
# ---------------------------------------------------------------------------

# Each node gets its own copy of these vars via copy_context().run()
ctx_node_id: contextvars.ContextVar[str] = contextvars.ContextVar("ctx_node_id", default="")
ctx_workflow_id: contextvars.ContextVar[str] = contextvars.ContextVar("ctx_workflow_id", default="")
ctx_repo: contextvars.ContextVar[str] = contextvars.ContextVar("ctx_repo", default="jarvis")
ctx_parent_results: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "ctx_parent_results"
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class NodeState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"     # dep failed → this node can't run


@dataclass
class DAGNode:
    """A single node in the execution DAG."""
    node_id: str
    goal: str
    dependencies: FrozenSet[str]          # node_ids this depends on
    executor: Callable[..., Coroutine]    # async callable(node, context) -> result
    repo: str = "jarvis"                  # which repo context this runs in
    timeout_s: float = 120.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Runtime state (mutated during execution)
    state: NodeState = field(default=NodeState.PENDING, init=False)
    result: Optional[Any] = field(default=None, init=False)
    error: Optional[str] = field(default=None, init=False)
    started_at: float = field(default=0.0, init=False)
    finished_at: float = field(default=0.0, init=False)

    @property
    def elapsed_s(self) -> float:
        if self.finished_at and self.started_at:
            return self.finished_at - self.started_at
        return 0.0


@dataclass(frozen=True)
class DAGPlan:
    """Immutable execution plan — validated at creation time."""
    plan_id: str
    nodes: Tuple[DAGNode, ...]
    topological_order: Tuple[str, ...]    # valid topological sort
    parallelism_groups: Tuple[Tuple[str, ...], ...]  # groups that can run in parallel
    total_nodes: int
    max_parallelism: int                  # max nodes in any single group


@dataclass
class DAGResult:
    """Complete execution result."""
    plan_id: str
    nodes: Dict[str, DAGNode]
    success: bool
    total_elapsed_s: float
    completed_count: int
    failed_count: int
    blocked_count: int


# ---------------------------------------------------------------------------
# DAG Planner — builds and validates the execution plan
# ---------------------------------------------------------------------------

class DAGPlanner:
    """Builds a validated DAGPlan from a list of steps with dependencies.

    Performs cycle detection and computes parallelism groups at plan time,
    not execution time. If the DAG has a cycle, it raises immediately.
    """

    @staticmethod
    def build(
        steps: List[Dict[str, Any]],
        executor: Callable[..., Coroutine],
    ) -> DAGPlan:
        """Build a DAGPlan from a list of step dicts.

        Each step dict must have:
            - goal: str
            - dependencies: list of step indices or goal strings (optional)
            - repo: str (optional, default "jarvis")
            - timeout_s: float (optional, default 120.0)

        Returns a validated DAGPlan with topological order and parallelism groups.
        Raises ValueError on cycle detection.
        """
        # Create nodes
        nodes: List[DAGNode] = []
        goal_to_id: Dict[str, str] = {}

        for i, step in enumerate(steps):
            node_id = f"node-{i}"
            goal = step.get("goal", f"step-{i}")
            goal_to_id[goal] = node_id
            goal_to_id[str(i)] = node_id  # allow index-based deps

        for i, step in enumerate(steps):
            node_id = f"node-{i}"
            goal = step.get("goal", f"step-{i}")

            # Resolve dependencies to node IDs
            raw_deps = step.get("dependencies", [])
            dep_ids: Set[str] = set()
            for dep in raw_deps:
                if isinstance(dep, int):
                    dep_ids.add(f"node-{dep}")
                elif isinstance(dep, str):
                    resolved = goal_to_id.get(dep)
                    if resolved:
                        dep_ids.add(resolved)

            nodes.append(DAGNode(
                node_id=node_id,
                goal=goal,
                dependencies=frozenset(dep_ids),
                executor=executor,
                repo=step.get("repo", "jarvis"),
                timeout_s=step.get("timeout_s", 120.0),
                metadata=step,
            ))

        # Topological sort + cycle detection (Kahn's algorithm)
        topo_order = DAGPlanner._topological_sort(nodes)

        # Compute parallelism groups (nodes at the same "depth" in the DAG)
        groups = DAGPlanner._compute_parallelism_groups(nodes, topo_order)

        return DAGPlan(
            plan_id=f"dag-{uuid.uuid4().hex[:12]}",
            nodes=tuple(nodes),
            topological_order=tuple(topo_order),
            parallelism_groups=tuple(tuple(g) for g in groups),
            total_nodes=len(nodes),
            max_parallelism=max(len(g) for g in groups) if groups else 0,
        )

    @staticmethod
    def _topological_sort(nodes: List[DAGNode]) -> List[str]:
        """Kahn's algorithm for topological sort with cycle detection."""
        node_map = {n.node_id: n for n in nodes}
        in_degree: Dict[str, int] = {n.node_id: len(n.dependencies) for n in nodes}
        # Only count deps that exist in this DAG
        for n in nodes:
            valid_deps = n.dependencies & set(node_map.keys())
            in_degree[n.node_id] = len(valid_deps)

        queue: deque = deque()
        for nid, degree in in_degree.items():
            if degree == 0:
                queue.append(nid)

        order: List[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            # Reduce in-degree for all nodes that depend on this one
            for n in nodes:
                if nid in n.dependencies:
                    in_degree[n.node_id] -= 1
                    if in_degree[n.node_id] == 0:
                        queue.append(n.node_id)

        if len(order) != len(nodes):
            remaining = set(n.node_id for n in nodes) - set(order)
            raise ValueError(
                f"DAG cycle detected — cannot topologically sort. "
                f"Nodes in cycle: {remaining}"
            )

        return order

    @staticmethod
    def _compute_parallelism_groups(
        nodes: List[DAGNode], topo_order: List[str],
    ) -> List[List[str]]:
        """Compute groups of nodes that can execute in parallel.

        Nodes at the same "depth" (all deps satisfied at the same time)
        form a parallelism group.
        """
        node_map = {n.node_id: n for n in nodes}
        depth: Dict[str, int] = {}

        for nid in topo_order:
            node = node_map[nid]
            if not node.dependencies:
                depth[nid] = 0
            else:
                valid_deps = node.dependencies & set(node_map.keys())
                if valid_deps:
                    depth[nid] = max(depth.get(d, 0) for d in valid_deps) + 1
                else:
                    depth[nid] = 0

        # Group by depth
        groups_map: Dict[int, List[str]] = defaultdict(list)
        for nid in topo_order:
            groups_map[depth[nid]].append(nid)

        return [groups_map[d] for d in sorted(groups_map.keys())]


# ---------------------------------------------------------------------------
# DAG Executor — event-driven concurrent execution engine
# ---------------------------------------------------------------------------

class DAGExecutor:
    """Executes a DAGPlan with maximum concurrency using asyncio.Event signaling.

    Each node waits on Events for its dependencies. When a dep completes,
    its Event is set → all waiters wake immediately. Independent nodes
    run truly in parallel — no polling, no sleep, no sequential bottleneck.

    Cross-repo context isolation via contextvars.copy_context().run() ensures
    parallel nodes from different repos never corrupt each other's state.
    """

    def __init__(self, max_concurrency: int = 16) -> None:
        self._max_concurrency = max_concurrency

    async def execute(
        self,
        plan: DAGPlan,
        workflow_context: Optional[Dict[str, Any]] = None,
    ) -> DAGResult:
        """Execute the DAG plan with event-driven concurrency."""
        start = time.monotonic()
        node_map = {n.node_id: n for n in plan.nodes}
        completion_events: Dict[str, asyncio.Event] = {
            n.node_id: asyncio.Event() for n in plan.nodes
        }
        semaphore = asyncio.Semaphore(self._max_concurrency)
        wf_ctx = workflow_context or {}

        logger.info(
            "[DAGExecutor] Starting %s: %d nodes, max_parallel=%d",
            plan.plan_id, plan.total_nodes, plan.max_parallelism,
        )

        # Launch all nodes concurrently — each waits on its own deps
        tasks: Dict[str, asyncio.Task] = {}
        for node in plan.nodes:
            task = asyncio.create_task(
                self._run_node(
                    node=node,
                    node_map=node_map,
                    events=completion_events,
                    semaphore=semaphore,
                    workflow_context=wf_ctx,
                ),
                name=f"dag_{node.node_id}",
            )
            tasks[node.node_id] = task

        # Wait for ALL nodes to finish (completed, failed, or blocked)
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        elapsed = time.monotonic() - start
        completed = sum(1 for n in plan.nodes if n.state == NodeState.COMPLETED)
        failed = sum(1 for n in plan.nodes if n.state == NodeState.FAILED)
        blocked = sum(1 for n in plan.nodes if n.state == NodeState.BLOCKED)

        logger.info(
            "[DAGExecutor] %s finished: %d completed, %d failed, %d blocked (%.2fs)",
            plan.plan_id, completed, failed, blocked, elapsed,
        )

        return DAGResult(
            plan_id=plan.plan_id,
            nodes=node_map,
            success=failed == 0 and blocked == 0,
            total_elapsed_s=elapsed,
            completed_count=completed,
            failed_count=failed,
            blocked_count=blocked,
        )

    async def _run_node(
        self,
        node: DAGNode,
        node_map: Dict[str, DAGNode],
        events: Dict[str, asyncio.Event],
        semaphore: asyncio.Semaphore,
        workflow_context: Dict[str, Any],
    ) -> None:
        """Execute a single node after all its dependencies complete.

        Uses asyncio.Event for zero-latency dep signaling.
        Uses contextvars for cross-repo isolation.
        Uses semaphore for max concurrency control.
        """
        # Wait for all dependencies to complete
        for dep_id in node.dependencies:
            if dep_id in events:
                await events[dep_id].wait()

            # Check if dep failed → block this node
            dep_node = node_map.get(dep_id)
            if dep_node and dep_node.state in (NodeState.FAILED, NodeState.BLOCKED):
                node.state = NodeState.BLOCKED
                node.error = f"Blocked: dependency {dep_id} {dep_node.state.value}"
                events[node.node_id].set()  # Signal dependents so they can check
                logger.info(
                    "[DAGExecutor] %s BLOCKED (dep %s %s)",
                    node.node_id, dep_id, dep_node.state.value,
                )
                return

        # Acquire concurrency slot
        async with semaphore:
            node.state = NodeState.RUNNING
            node.started_at = time.monotonic()

            # Collect parent results for context propagation
            parent_results = {}
            for dep_id in node.dependencies:
                dep_node = node_map.get(dep_id)
                if dep_node and dep_node.result is not None:
                    parent_results[dep_id] = dep_node.result

            # Execute in isolated context (cross-repo safety)
            ctx = contextvars.copy_context()
            try:
                result = await asyncio.wait_for(
                    ctx.run(
                        self._execute_in_context,
                        node, parent_results, workflow_context,
                    ),
                    timeout=node.timeout_s,
                )
                node.result = result
                node.state = NodeState.COMPLETED
                node.finished_at = time.monotonic()
                logger.info(
                    "[DAGExecutor] %s COMPLETED (%.2fs)",
                    node.node_id, node.elapsed_s,
                )
            except asyncio.TimeoutError:
                node.state = NodeState.FAILED
                node.error = f"Timeout after {node.timeout_s}s"
                node.finished_at = time.monotonic()
                logger.warning("[DAGExecutor] %s TIMEOUT", node.node_id)
            except Exception as exc:
                node.state = NodeState.FAILED
                node.error = str(exc)
                node.finished_at = time.monotonic()
                logger.warning("[DAGExecutor] %s FAILED: %s", node.node_id, exc)

        # Signal dependents that this node is done (success or failure)
        events[node.node_id].set()

    async def _execute_in_context(
        self,
        node: DAGNode,
        parent_results: Dict[str, Any],
        workflow_context: Dict[str, Any],
    ) -> Any:
        """Execute node's coroutine with contextvars set for isolation.

        This runs inside copy_context().run() — changes to contextvars
        in this node are invisible to other parallel nodes.
        """
        # Set context variables for this node
        ctx_node_id.set(node.node_id)
        ctx_repo.set(node.repo)
        ctx_parent_results.set(parent_results)

        # Build execution context
        exec_ctx = {
            "node_id": node.node_id,
            "goal": node.goal,
            "repo": node.repo,
            "parent_results": parent_results,
            "workflow_context": workflow_context,
            **node.metadata,
        }

        return await node.executor(node, exec_ctx)
