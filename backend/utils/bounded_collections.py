"""
Bounded Collections v1.0 — Memory-safe drop-in replacements.

Provides bounded variants of defaultdict and other collections that
prevent unbounded memory growth. When capacity is exceeded, oldest
entries are evicted (LRU policy).

Usage:
    from backend.utils.bounded_collections import BoundedDefaultDict

    # Drop-in replacement for defaultdict(list)
    d = BoundedDefaultDict(list, max_size=1000)
    d["key"].append("value")

    # With eviction callback
    def on_evict(key, value):
        logger.debug(f"Evicted {key}")

    d = BoundedDefaultDict(list, max_size=500, on_evict=on_evict)
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    Optional,
    TypeVar,
    Union,
)

logger = logging.getLogger("jarvis.bounded_collections")

KT = TypeVar("KT")
VT = TypeVar("VT")


class BoundedDefaultDict(Dict[KT, VT]):
    """
    A dict with a default_factory (like defaultdict) that evicts the
    oldest entries when *max_size* is exceeded.

    - Thread-safe via a threading.Lock.
    - LRU: accessing an existing key refreshes its position.
    - Optional *on_evict* callback fired synchronously on eviction.
    """

    __slots__ = (
        "_default_factory",
        "_max_size",
        "_on_evict",
        "_lock",
        "_order",
        "_eviction_count",
    )

    def __init__(
        self,
        default_factory: Optional[Callable[[], VT]] = None,
        *,
        max_size: int = 10_000,
        on_evict: Optional[Callable[[KT, VT], None]] = None,
    ):
        super().__init__()
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._default_factory = default_factory
        self._max_size = max_size
        self._on_evict = on_evict
        self._lock = threading.Lock()
        self._order: OrderedDict[KT, None] = OrderedDict()
        self._eviction_count = 0

    # -- defaultdict semantics -----------------------------------------------

    def __missing__(self, key: KT) -> VT:
        if self._default_factory is None:
            raise KeyError(key)
        value = self._default_factory()
        self[key] = value
        return value

    # -- dict overrides with bounding ----------------------------------------

    def __setitem__(self, key: KT, value: VT) -> None:
        with self._lock:
            if key in self._order:
                self._order.move_to_end(key)
            else:
                self._order[key] = None
                self._maybe_evict()
            super().__setitem__(key, value)

    def __getitem__(self, key: KT) -> VT:
        with self._lock:
            if key in self._order:
                self._order.move_to_end(key)
        try:
            return super().__getitem__(key)
        except KeyError:
            return self.__missing__(key)

    def __delitem__(self, key: KT) -> None:
        with self._lock:
            self._order.pop(key, None)
            super().__delitem__(key)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key)

    def __len__(self) -> int:
        return super().__len__()

    def __iter__(self) -> Iterator[KT]:
        return super().__iter__()

    # -- capacity management -------------------------------------------------

    def _maybe_evict(self) -> None:
        """Evict oldest entries until within capacity. Caller holds _lock."""
        while len(self._order) > self._max_size:
            oldest_key, _ = self._order.popitem(last=False)
            try:
                evicted_value = super().pop(oldest_key)
            except KeyError:
                continue
            self._eviction_count += 1
            if self._on_evict is not None:
                try:
                    self._on_evict(oldest_key, evicted_value)
                except Exception:
                    pass  # Never let callback break the dict

    # -- extra API -----------------------------------------------------------

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def eviction_count(self) -> int:
        return self._eviction_count

    def resize(self, new_max: int) -> int:
        """Resize the collection, evicting if needed. Returns eviction count."""
        if new_max < 1:
            raise ValueError("max_size must be >= 1")
        before = self._eviction_count
        with self._lock:
            self._max_size = new_max
            self._maybe_evict()
        return self._eviction_count - before

    def get_stats(self) -> Dict[str, Any]:
        """Return collection statistics."""
        return {
            "size": len(self),
            "max_size": self._max_size,
            "eviction_count": self._eviction_count,
            "utilization": len(self) / self._max_size if self._max_size else 0,
        }

    def clear(self) -> None:
        with self._lock:
            self._order.clear()
            super().clear()

    def pop(self, key: KT, *args: Any) -> VT:
        with self._lock:
            self._order.pop(key, None)
            return super().pop(key, *args)

    def setdefault(self, key: KT, default: VT = None) -> VT:  # type: ignore[assignment]
        with self._lock:
            if key not in self:
                self[key] = default  # type: ignore[assignment]
            return super().__getitem__(key)

    def update(self, *args: Any, **kwargs: Any) -> None:
        with self._lock:
            if args:
                other = args[0]
                if isinstance(other, dict):
                    for key, value in other.items():
                        self[key] = value
                else:
                    for key, value in other:
                        self[key] = value
            for key, value in kwargs.items():
                self[key] = value

    def copy(self) -> "BoundedDefaultDict[KT, VT]":
        new = BoundedDefaultDict(
            self._default_factory,
            max_size=self._max_size,
            on_evict=self._on_evict,
        )
        new.update(self)
        return new

    def __repr__(self) -> str:
        return (
            f"BoundedDefaultDict({self._default_factory}, "
            f"max_size={self._max_size}, "
            f"len={len(self)}, "
            f"evictions={self._eviction_count})"
        )
