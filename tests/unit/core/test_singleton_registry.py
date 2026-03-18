"""tests/unit/core/test_singleton_registry.py — Diseases 6+10 registry tests."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.singleton_registry import (
    AsyncSingletonFactory,
    SingletonRegistry,
    get_singleton_registry,
)


class TestAsyncSingletonFactory:
    @pytest.mark.asyncio
    async def test_factory_called_once(self):
        calls = []

        def factory():
            calls.append(1)
            return object()

        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", factory)
        await f.get_or_create()
        await f.get_or_create()
        await f.get_or_create()
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_returns_same_instance(self):
        sentinel = object()
        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", lambda: sentinel)
        a = await f.get_or_create()
        b = await f.get_or_create()
        assert a is b is sentinel

    @pytest.mark.asyncio
    async def test_async_factory_supported(self):
        async def async_factory():
            await asyncio.sleep(0)
            return 42

        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", async_factory)
        result = await f.get_or_create()
        assert result == 42

    @pytest.mark.asyncio
    async def test_is_initialized_false_before_first_call(self):
        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", object)
        assert not f.is_initialized

    @pytest.mark.asyncio
    async def test_is_initialized_true_after_creation(self):
        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", object)
        await f.get_or_create()
        assert f.is_initialized

    @pytest.mark.asyncio
    async def test_reset_allows_recreation(self):
        calls = []

        def factory():
            calls.append(1)
            return object()

        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", factory)
        inst1 = await f.get_or_create()
        await f.reset()
        inst2 = await f.get_or_create()
        assert len(calls) == 2
        assert inst1 is not inst2

    @pytest.mark.asyncio
    async def test_concurrent_calls_create_exactly_one_instance(self):
        calls = []

        async def slow_factory():
            await asyncio.sleep(0.01)
            calls.append(1)
            return object()

        f: AsyncSingletonFactory = AsyncSingletonFactory("svc", slow_factory)
        results = await asyncio.gather(*[f.get_or_create() for _ in range(10)])
        # All 10 should return the same object
        assert all(r is results[0] for r in results)
        assert len(calls) == 1


class TestSingletonRegistry:
    @pytest.mark.asyncio
    async def test_register_and_get(self):
        r = SingletonRegistry()
        r.register("svc", lambda: 99)
        val = await r.get("svc")
        assert val == 99

    @pytest.mark.asyncio
    async def test_get_unknown_raises_key_error(self):
        r = SingletonRegistry()
        with pytest.raises(KeyError):
            await r.get("not_registered")

    def test_duplicate_register_raises_value_error(self):
        r = SingletonRegistry()
        r.register("svc", lambda: 1)
        with pytest.raises(ValueError):
            r.register("svc", lambda: 2)

    def test_duplicate_register_with_replace(self):
        r = SingletonRegistry()
        r.register("svc", lambda: 1)
        r.register("svc", lambda: 2, replace=True)  # should not raise

    @pytest.mark.asyncio
    async def test_reset_single_name(self):
        calls = []

        def factory():
            calls.append(1)
            return object()

        r = SingletonRegistry()
        r.register("svc", factory)
        await r.get("svc")
        await r.reset("svc")
        await r.get("svc")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_reset_unknown_raises_key_error(self):
        r = SingletonRegistry()
        with pytest.raises(KeyError):
            await r.reset("unknown")

    @pytest.mark.asyncio
    async def test_reset_all_clears_all_instances(self):
        calls = {"a": 0, "b": 0}

        r = SingletonRegistry()
        r.register("a", lambda: calls.update({"a": calls["a"] + 1}) or "a")
        r.register("b", lambda: calls.update({"b": calls["b"] + 1}) or "b")

        await r.get("a")
        await r.get("b")
        assert calls == {"a": 1, "b": 1}

        await r.reset_all()
        await r.get("a")
        await r.get("b")
        assert calls == {"a": 2, "b": 2}

    def test_registered_names_lists_all(self):
        r = SingletonRegistry()
        r.register("x", lambda: None)
        r.register("y", lambda: None)
        assert set(r.registered_names) == {"x", "y"}

    @pytest.mark.asyncio
    async def test_initialized_names_only_shows_created(self):
        r = SingletonRegistry()
        r.register("created", lambda: 1)
        r.register("not_yet", lambda: 2)
        await r.get("created")
        assert "created" in r.initialized_names
        assert "not_yet" not in r.initialized_names

    def test_get_factory_returns_factory(self):
        r = SingletonRegistry()
        r.register("svc", lambda: 42)
        factory = r.get_factory("svc")
        assert factory is not None
        assert factory.name == "svc"

    def test_get_factory_returns_none_for_unknown(self):
        r = SingletonRegistry()
        assert r.get_factory("nope") is None


class TestModuleSingleton:
    def test_get_singleton_registry_is_reused(self):
        r1 = get_singleton_registry()
        r2 = get_singleton_registry()
        assert r1 is r2
