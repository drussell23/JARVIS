# tests/integration/test_budget_propagation.py
"""Integration tests for timeout budget propagation across phase boundaries."""
import asyncio
import pytest


class TestBudgetPropagationIntegration:
    """End-to-end budget propagation through simulated startup phases."""

    @pytest.mark.asyncio
    async def test_phase_budget_propagates_to_services(self):
        """Phase with 0.5s budget, 3 services each requesting 0.3s.
        Third service should get less than 0.3s."""
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError, LocalCapExceededError,
            remaining_budget,
        )

        results = []

        async def service(name, duration):
            await asyncio.sleep(duration)
            results.append(name)
            return name

        async with execution_budget("phase_test", 0.5,
                                    phase_id="1", phase_name="test"):
            await budget_aware_wait_for(
                service("svc1", 0.15), local_cap=0.3, label="svc1"
            )
            await budget_aware_wait_for(
                service("svc2", 0.15), local_cap=0.3, label="svc2"
            )
            # By now ~0.3s elapsed, ~0.2s remaining
            remaining = remaining_budget()
            assert remaining is not None
            assert remaining < 0.3  # Less than svc3's local_cap

            with pytest.raises((BudgetExhaustedError, LocalCapExceededError)):
                await budget_aware_wait_for(
                    service("svc3", 0.5), local_cap=0.3, label="svc3"
                )

        assert "svc1" in results
        assert "svc2" in results

    @pytest.mark.asyncio
    async def test_budget_exhaustion_error_type(self):
        """Verify BudgetExhaustedError (not TimeoutError) when parent expires."""
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError,
        )

        async def slow_service():
            await asyncio.sleep(10.0)

        with pytest.raises(BudgetExhaustedError) as exc_info:
            async with execution_budget("phase", 0.1,
                                        phase_id="1", phase_name="test"):
                await budget_aware_wait_for(
                    slow_service(), local_cap=5.0, label="slow"
                )

        assert not isinstance(exc_info.value, TimeoutError)
        assert exc_info.value.timeout_origin == "budget"

    @pytest.mark.asyncio
    async def test_budget_metadata_audit_trail(self):
        """Verify parent_ctx chain is inspectable from innermost context."""
        from backend.core.execution_context import (
            execution_budget, current_context,
        )

        async with execution_budget("supervisor", 60.0,
                                    phase_id="0", phase_name="root"):
            async with execution_budget("phase_preflight", 30.0,
                                        phase_id="1", phase_name="preflight"):
                async with execution_budget("svc_lock", 10.0,
                                            phase_id="1", phase_name="preflight"):
                    ctx = current_context()
                    assert ctx.owner_id == "svc_lock"
                    assert ctx.parent_ctx.owner_id == "phase_preflight"
                    assert ctx.parent_ctx.parent_ctx.owner_id == "supervisor"

    @pytest.mark.asyncio
    async def test_concurrent_sibling_budget_isolation(self):
        """Concurrent tasks with different budgets never cross-contaminate."""
        from backend.core.execution_context import (
            execution_budget, remaining_budget,
        )

        async def worker(name, budget_s):
            async with execution_budget(name, budget_s):
                await asyncio.sleep(0.05)
                r = remaining_budget()
                assert r is not None
                return name, r

        async with execution_budget("supervisor", 60.0):
            results = await asyncio.gather(
                worker("fast", 1.0),
                worker("slow", 10.0),
            )

        budget_map = dict(results)
        # Fast worker should have ~0.9s remaining, slow ~9.9s
        assert budget_map["fast"] < budget_map["slow"]
        assert budget_map["fast"] < 1.0
        assert budget_map["slow"] > 5.0
