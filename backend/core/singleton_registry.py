"""backend/core/singleton_registry.py — Diseases 6 + 10: async-safe singleton factory.

Problems solved
---------------
Disease 6 — Race window in ``if _instance is None:``
    Two coroutines can both see ``None`` before the first one has finished
    constructing the instance (there is a yield point between the check and
    the assignment).  The second coroutine then constructs a *second*
    instance, silently replacing the first, leaving the original caller with
    a reference to the discarded object.

Disease 10 — No way to reset singletons between DMS restart cycles
    When the Dead Man's Switch (startup watchdog) triggers a restart, stale
    singleton state leaks into the new cycle.  Without a coordinated
    ``reset_all()`` the restart is partial and components may behave as if
    they are already initialised.

Design
------
* ``AsyncSingletonFactory[T]`` — asyncio.Lock-guarded factory for one type.
  Uses double-checked locking (fast no-lock read, then re-check under lock).
* ``SingletonRegistry``        — named collection of factories.
  ``reset_all()`` tears down every instance atomically for a clean restart.
* ``get_singleton_registry()`` — module-level singleton, itself guarded by
  a ``threading.Lock`` so it is safe from thread-pool callers.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Coroutine, Dict, Generic, List, Optional, TypeVar

__all__ = [
    "AsyncSingletonFactory",
    "SingletonRegistry",
    "get_singleton_registry",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# AsyncSingletonFactory
# ---------------------------------------------------------------------------


class AsyncSingletonFactory(Generic[T]):
    """asyncio.Lock-guarded lazy singleton factory.

    The factory callable may be synchronous *or* async::

        factory: AsyncSingletonFactory[MyService] = AsyncSingletonFactory(
            "my_service",
            lambda: MyService(),          # sync — also accepted
        )
        instance = await factory.get_or_create()

    Between ``reset()`` calls the instance is created at most once,
    regardless of how many coroutines call ``get_or_create`` concurrently.
    """

    def __init__(
        self,
        name: str,
        factory_fn: Callable[[], "T | Coroutine[Any, Any, T]"],
    ) -> None:
        self._name = name
        self._factory_fn = factory_fn
        self._instance: Optional[T] = None
        # Lock is created lazily so the factory can be constructed outside an
        # event loop (module import time).
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._instance is not None

    async def get_or_create(self) -> T:
        """Return the singleton, creating it under lock if necessary.

        Uses double-checked locking:
        1. Fast path: check without lock (common case — already initialised).
        2. Slow path: acquire lock, re-check, call factory if still ``None``.
        """
        if self._instance is not None:
            return self._instance

        async with self._ensure_lock():
            if self._instance is not None:  # re-check under lock
                return self._instance

            logger.debug("[SingletonFactory] initialising '%s'", self._name)
            result = self._factory_fn()
            if asyncio.iscoroutine(result):
                self._instance = await result
            else:
                self._instance = result  # type: ignore[assignment]
            logger.info("[SingletonFactory] '%s' ready", self._name)

        return self._instance  # type: ignore[return-value]

    async def reset(self) -> None:
        """Destroy the current instance.  Next ``get_or_create`` re-creates it."""
        async with self._ensure_lock():
            if self._instance is not None:
                logger.info(
                    "[SingletonFactory] '%s' reset — discarding instance",
                    self._name,
                )
            self._instance = None


# ---------------------------------------------------------------------------
# SingletonRegistry
# ---------------------------------------------------------------------------


class SingletonRegistry:
    """Named collection of ``AsyncSingletonFactory`` instances.

    Supports ``reset_all()`` for clean DMS restart cycles::

        registry = get_singleton_registry()

        # At module init time (sync):
        registry.register("ml_engine", lambda: MLEngine())

        # At runtime (async):
        engine = await registry.get("ml_engine")

        # DMS restart:
        await registry.reset_all()
    """

    def __init__(self) -> None:
        self._factories: Dict[str, AsyncSingletonFactory[Any]] = {}
        # threading.Lock (not asyncio.Lock) guards factory dict mutations so
        # register() can be called at module import time from any thread.
        self._meta_lock = threading.Lock()

    def register(
        self,
        name: str,
        factory_fn: Callable[[], Any],
        *,
        replace: bool = False,
    ) -> "AsyncSingletonFactory[Any]":
        """Register a zero-argument factory under *name*.

        Parameters
        ----------
        name:
            Unique key (e.g. ``"ml_engine"``).
        factory_fn:
            Sync or async callable producing the singleton.
        replace:
            If ``True`` silently overwrite an existing registration.
            If ``False`` (default) raise ``ValueError`` on duplicate.
        """
        with self._meta_lock:
            if name in self._factories and not replace:
                raise ValueError(
                    f"[SingletonRegistry] '{name}' already registered. "
                    "Pass replace=True to overwrite."
                )
            factory: AsyncSingletonFactory[Any] = AsyncSingletonFactory(
                name, factory_fn
            )
            self._factories[name] = factory
            logger.debug("[SingletonRegistry] registered '%s'", name)
        return factory

    async def get(self, name: str) -> Any:
        """Return the singleton for *name*, creating it if necessary.

        Raises ``KeyError`` if *name* has not been registered.
        """
        with self._meta_lock:
            factory = self._factories.get(name)
        if factory is None:
            raise KeyError(f"[SingletonRegistry] '{name}' not registered")
        return await factory.get_or_create()

    def get_factory(self, name: str) -> Optional["AsyncSingletonFactory[Any]"]:
        """Return the factory object for *name*, or ``None``."""
        with self._meta_lock:
            return self._factories.get(name)

    async def reset(self, name: str) -> None:
        """Reset one named singleton so it is re-created on next access.

        Raises ``KeyError`` if *name* has not been registered.
        """
        with self._meta_lock:
            factory = self._factories.get(name)
        if factory is None:
            raise KeyError(f"[SingletonRegistry] '{name}' not registered")
        await factory.reset()

    async def reset_all(self) -> None:
        """Reset ALL registered singletons.

        Each factory is reset independently — a failure in one does NOT abort
        the others.  All errors are logged and summarised at the end.

        Call this at the top of every DMS restart cycle before reinitialising
        any component.
        """
        with self._meta_lock:
            factories = list(self._factories.values())

        errors: List[tuple[str, BaseException]] = []
        for factory in factories:
            try:
                await factory.reset()
            except BaseException as exc:  # noqa: BLE001
                logger.error(
                    "[SingletonRegistry] reset '%s' raised: %s",
                    factory.name, exc,
                )
                errors.append((factory.name, exc))

        if errors:
            names = ", ".join(n for n, _ in errors)
            logger.warning(
                "[SingletonRegistry] reset_all completed with %d error(s): %s",
                len(errors), names,
            )
        else:
            logger.info(
                "[SingletonRegistry] reset_all — %d singleton(s) cleared",
                len(factories),
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def registered_names(self) -> List[str]:
        """Names of all registered factories (snapshot)."""
        with self._meta_lock:
            return list(self._factories.keys())

    @property
    def initialized_names(self) -> List[str]:
        """Names of factories that have been initialised."""
        with self._meta_lock:
            return [n for n, f in self._factories.items() if f.is_initialized]


# ---------------------------------------------------------------------------
# Module-level singleton — guarded by threading.Lock for thread safety
# ---------------------------------------------------------------------------

_g_registry: Optional[SingletonRegistry] = None
_g_registry_lock = threading.Lock()


def get_singleton_registry() -> SingletonRegistry:
    """Return (lazily creating) the process-wide SingletonRegistry.

    Thread-safe via ``threading.Lock`` so this can be called at module import
    time from any thread before an event loop is running.
    """
    global _g_registry
    if _g_registry is not None:
        return _g_registry
    with _g_registry_lock:
        if _g_registry is None:
            _g_registry = SingletonRegistry()
    return _g_registry
