"""Tests for DAG Execution Engine — topological concurrent execution."""
import asyncio
import time

import pytest

from backend.core.dag_execution_engine import (
    DAGExecutor,
    DAGNode,
    DAGPlan,
    DAGPlanner,
    DAGResult,
    NodeState,
    ctx_node_id,
    ctx_repo,
    ctx_parent_results,
)


async def _noop_executor(node, context):
    """Executor that returns immediately."""
    return {"goal": node.goal, "status": "done"}


async def _slow_executor(node, context):
    """Executor that takes 0.1s."""
    await asyncio.sleep(0.1)
    return {"goal": node.goal, "status": "done"}


async def _failing_executor(node, context):
    """Executor that always fails."""
    raise RuntimeError(f"Intentional failure in {node.node_id}")


async def _context_capturing_executor(node, context):
    """Executor that captures contextvars for verification."""
    return {
        "ctx_node_id": ctx_node_id.get(),
        "ctx_repo": ctx_repo.get(),
        "parent_results": ctx_parent_results.get(),
    }


async def _timing_executor(node, context):
    """Executor that records wall-clock time for parallelism verification."""
    start = time.monotonic()
    await asyncio.sleep(0.1)
    return {"start": start, "end": time.monotonic()}


# ---------------------------------------------------------------------------
# DAGPlanner Tests
# ---------------------------------------------------------------------------

class TestDAGPlanner:
    def test_build_simple_plan(self):
        steps = [
            {"goal": "step A"},
            {"goal": "step B"},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        assert plan.total_nodes == 2
        assert len(plan.topological_order) == 2

    def test_build_with_dependencies(self):
        steps = [
            {"goal": "step A"},
            {"goal": "step B", "dependencies": [0]},
            {"goal": "step C", "dependencies": [0, 1]},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        order = plan.topological_order
        assert order.index("node-0") < order.index("node-1")
        assert order.index("node-1") < order.index("node-2")

    def test_parallelism_groups_independent(self):
        steps = [
            {"goal": "A"},
            {"goal": "B"},
            {"goal": "C"},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        assert plan.max_parallelism == 3  # all independent

    def test_parallelism_groups_chain(self):
        steps = [
            {"goal": "A"},
            {"goal": "B", "dependencies": [0]},
            {"goal": "C", "dependencies": [1]},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        assert plan.max_parallelism == 1  # fully sequential

    def test_parallelism_groups_diamond(self):
        steps = [
            {"goal": "A"},
            {"goal": "B", "dependencies": [0]},
            {"goal": "C", "dependencies": [0]},
            {"goal": "D", "dependencies": [1, 2]},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        # A → (B, C parallel) → D
        assert plan.max_parallelism == 2

    def test_cycle_detection(self):
        steps = [
            {"goal": "A", "dependencies": [1]},
            {"goal": "B", "dependencies": [0]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            DAGPlanner.build(steps, _noop_executor)

    def test_string_dependencies(self):
        steps = [
            {"goal": "fetch docs"},
            {"goal": "synthesize code", "dependencies": ["fetch docs"]},
        ]
        plan = DAGPlanner.build(steps, _noop_executor)
        assert plan.total_nodes == 2
        order = plan.topological_order
        assert order.index("node-0") < order.index("node-1")


# ---------------------------------------------------------------------------
# DAGExecutor Tests
# ---------------------------------------------------------------------------

class TestDAGExecutor:
    @pytest.mark.asyncio
    async def test_execute_independent_nodes_parallel(self):
        """Independent nodes should execute in parallel, not sequentially."""
        steps = [
            {"goal": "A"},
            {"goal": "B"},
            {"goal": "C"},
        ]
        plan = DAGPlanner.build(steps, _timing_executor)
        executor = DAGExecutor(max_concurrency=16)

        start = time.monotonic()
        result = await executor.execute(plan)
        elapsed = time.monotonic() - start

        assert result.success
        assert result.completed_count == 3
        # 3 tasks at 0.1s each — if parallel, total < 0.25s
        # if sequential, total > 0.3s
        assert elapsed < 0.25, f"Expected parallel execution but took {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_execute_sequential_dependencies(self):
        """Dependent nodes must wait for their deps."""
        steps = [
            {"goal": "A"},
            {"goal": "B", "dependencies": [0]},
        ]
        plan = DAGPlanner.build(steps, _timing_executor)
        executor = DAGExecutor()

        result = await executor.execute(plan)
        assert result.success

        node_a = result.nodes["node-0"]
        node_b = result.nodes["node-1"]
        # B must start AFTER A finishes
        assert node_b.result["start"] >= node_a.result["end"] - 0.01

    @pytest.mark.asyncio
    async def test_diamond_dag(self):
        """Diamond: A → (B, C parallel) → D."""
        steps = [
            {"goal": "A"},
            {"goal": "B", "dependencies": [0]},
            {"goal": "C", "dependencies": [0]},
            {"goal": "D", "dependencies": [1, 2]},
        ]
        plan = DAGPlanner.build(steps, _timing_executor)
        executor = DAGExecutor()

        start = time.monotonic()
        result = await executor.execute(plan)
        elapsed = time.monotonic() - start

        assert result.success
        assert result.completed_count == 4
        # A(0.1) → B,C parallel(0.1) → D(0.1) = 0.3s total
        assert elapsed < 0.45, f"Diamond should take ~0.3s, took {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_failure_blocks_dependents(self):
        """A failed node should BLOCK all transitive dependents."""
        async def maybe_fail(node, ctx):
            if node.node_id == "node-0":
                raise RuntimeError("intentional")
            return {"ok": True}

        steps = [
            {"goal": "A (will fail)"},
            {"goal": "B (depends on A)", "dependencies": [0]},
            {"goal": "C (independent)"},
        ]
        plan = DAGPlanner.build(steps, maybe_fail)
        executor = DAGExecutor()
        result = await executor.execute(plan)

        assert not result.success
        assert result.nodes["node-0"].state == NodeState.FAILED
        assert result.nodes["node-1"].state == NodeState.BLOCKED
        assert result.nodes["node-2"].state == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Node that exceeds timeout should be marked FAILED."""
        async def hang(node, ctx):
            await asyncio.sleep(100)

        steps = [{"goal": "will timeout", "timeout_s": 0.1}]
        plan = DAGPlanner.build(steps, hang)
        executor = DAGExecutor()
        result = await executor.execute(plan)

        assert not result.success
        assert result.nodes["node-0"].state == NodeState.FAILED
        assert "Timeout" in result.nodes["node-0"].error

    @pytest.mark.asyncio
    async def test_context_isolation(self):
        """Parallel nodes should have isolated contextvars."""
        steps = [
            {"goal": "A", "repo": "jarvis"},
            {"goal": "B", "repo": "prime"},
        ]
        plan = DAGPlanner.build(steps, _context_capturing_executor)
        executor = DAGExecutor()
        result = await executor.execute(plan)

        assert result.success
        assert result.nodes["node-0"].result["ctx_repo"] == "jarvis"
        assert result.nodes["node-1"].result["ctx_repo"] == "prime"

    @pytest.mark.asyncio
    async def test_parent_results_propagated(self):
        """Child nodes should receive parent results in context."""
        async def producer(node, ctx):
            return {"data": 42}

        async def consumer(node, ctx):
            parents = ctx.get("parent_results", {})
            return {"received": parents}

        steps = [
            {"goal": "produce"},
            {"goal": "consume", "dependencies": [0]},
        ]
        # Need different executors per node — use metadata routing
        async def routing_executor(node, ctx):
            if node.node_id == "node-0":
                return await producer(node, ctx)
            return await consumer(node, ctx)

        plan = DAGPlanner.build(steps, routing_executor)
        executor = DAGExecutor()
        result = await executor.execute(plan)

        assert result.success
        consumer_result = result.nodes["node-1"].result
        assert "node-0" in consumer_result["received"]
        assert consumer_result["received"]["node-0"]["data"] == 42

    @pytest.mark.asyncio
    async def test_max_concurrency_respected(self):
        """Semaphore should limit concurrent execution."""
        concurrent_count = 0
        max_seen = 0

        async def counting_executor(node, ctx):
            nonlocal concurrent_count, max_seen
            concurrent_count += 1
            max_seen = max(max_seen, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return {"ok": True}

        steps = [{"goal": f"task-{i}"} for i in range(10)]
        plan = DAGPlanner.build(steps, counting_executor)
        executor = DAGExecutor(max_concurrency=3)
        result = await executor.execute(plan)

        assert result.success
        assert max_seen <= 3

    @pytest.mark.asyncio
    async def test_empty_plan(self):
        plan = DAGPlanner.build([], _noop_executor)
        executor = DAGExecutor()
        result = await executor.execute(plan)
        assert result.success
        assert result.completed_count == 0

    @pytest.mark.asyncio
    async def test_single_node(self):
        plan = DAGPlanner.build([{"goal": "only"}], _noop_executor)
        executor = DAGExecutor()
        result = await executor.execute(plan)
        assert result.success
        assert result.completed_count == 1
