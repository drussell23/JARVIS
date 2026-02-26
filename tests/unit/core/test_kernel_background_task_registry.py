import asyncio
import ast
from pathlib import Path
from typing import Any, Callable, List, Optional

import pytest


def _load_registry_class():
    supervisor_path = Path(__file__).resolve().parents[3] / "unified_supervisor.py"
    source = supervisor_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KernelBackgroundTaskRegistry"
    )
    isolated = ast.Module(body=[class_node], type_ignores=[])
    ast.fix_missing_locations(isolated)
    namespace = {
        "asyncio": asyncio,
        "Any": Any,
        "Callable": Callable,
        "List": List,
        "Optional": Optional,
        "UnifiedLogger": object,
    }
    exec(compile(isolated, str(supervisor_path), "exec"), namespace)
    return namespace["KernelBackgroundTaskRegistry"]


KernelBackgroundTaskRegistry = _load_registry_class()


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_registry_dedupes_and_prunes_done_tasks():
    gate = {"open": True}
    registry = KernelBackgroundTaskRegistry(
        logger=_StubLogger(),
        can_accept_new=lambda: gate["open"],
    )

    async def short_task():
        await asyncio.sleep(0)

    task = asyncio.create_task(short_task(), name="registry-short")

    assert registry.append(task) is True
    assert registry.append(task) is False
    assert len(registry) == 1

    await task
    await asyncio.sleep(0)  # let done callbacks run

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_rejects_late_registration_and_cancels_task():
    gate = {"open": False}
    registry = KernelBackgroundTaskRegistry(
        logger=_StubLogger(),
        can_accept_new=lambda: gate["open"],
    )

    started = asyncio.Event()

    async def blocking_task():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(blocking_task(), name="registry-late")
    await started.wait()

    assert registry.append(task) is False
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)

    assert len(registry) == 0


@pytest.mark.asyncio
async def test_registry_extend_accepts_only_unique_live_tasks():
    gate = {"open": True}
    registry = KernelBackgroundTaskRegistry(
        logger=_StubLogger(),
        can_accept_new=lambda: gate["open"],
    )

    async def long_task():
        await asyncio.sleep(30)

    async def short_task():
        await asyncio.sleep(0)

    task_a = asyncio.create_task(long_task(), name="registry-a")
    task_b = asyncio.create_task(long_task(), name="registry-b")
    task_done = asyncio.create_task(short_task(), name="registry-done")
    await task_done

    accepted = registry.extend([task_a, task_b, task_b, task_done])
    assert accepted == 2

    task_a.cancel()
    await asyncio.gather(task_a, return_exceptions=True)
    assert task_b in registry
    task_b.cancel()
    await asyncio.gather(task_b, return_exceptions=True)
    await asyncio.sleep(0)

    assert len(registry) == 0
