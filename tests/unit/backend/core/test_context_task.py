"""Tests for context-propagating task creation."""
import asyncio
import contextvars
import unittest


_test_var: contextvars.ContextVar[str] = contextvars.ContextVar("test_var", default="unset")


class TestContextTask(unittest.TestCase):
    def test_propagates_contextvars(self):
        from backend.core.context_task import create_traced_task

        results = []

        async def child():
            results.append(_test_var.get())

        async def parent():
            _test_var.set("parent-value")
            task = create_traced_task(child(), name="test-child")
            await task

        asyncio.run(parent())
        assert results == ["parent-value"]

    def test_default_context_without_parent(self):
        from backend.core.context_task import create_traced_task

        results = []

        async def child():
            results.append(_test_var.get())

        async def run():
            task = create_traced_task(child(), name="test-orphan")
            await task

        asyncio.run(run())
        assert results == ["unset"]

    def test_cancelled_error_propagates(self):
        from backend.core.context_task import create_traced_task

        async def slow_task():
            await asyncio.sleep(100)

        async def run():
            task = create_traced_task(slow_task(), name="cancellable")
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def test_exception_callback_fires(self):
        from backend.core.context_task import create_traced_task

        errors = []

        async def failing():
            raise ValueError("boom")

        async def run():
            task = create_traced_task(
                failing(), name="fail-task",
                on_error=lambda name, exc: errors.append((name, str(exc))),
            )
            try:
                await task
            except ValueError:
                pass

        asyncio.run(run())
        assert len(errors) == 1
        assert errors[0][0] == "fail-task"
        assert "boom" in errors[0][1]

    def test_correlation_context_propagates(self):
        """Verify that CorrelationContext set in parent is visible in child."""
        results = []

        async def run():
            # Import inside async context so that any module-level
            # asyncio.Lock() calls in transitive imports (e.g.
            # vector_clock) find a running event loop (Python 3.9).
            from backend.core.context_task import create_traced_task
            from backend.core.resilience.correlation_context import (
                CorrelationContext, set_current_context, get_current_context,
            )

            async def child():
                ctx = get_current_context()
                results.append(ctx.correlation_id if ctx else None)

            ctx = CorrelationContext.create(
                operation="test-op", source_component="test"
            )
            set_current_context(ctx)
            task = create_traced_task(child(), name="corr-child")
            await task

        asyncio.run(run())
        assert results[0] is not None


if __name__ == "__main__":
    unittest.main()
