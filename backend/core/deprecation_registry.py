"""backend/core/deprecation_registry.py — P3-5 formal deprecation policy.

Provides a lightweight, zero-dependency deprecation DSL that:

* Lets callers register schema fields, API paths, or any named item with a
  deprecation date and a hard removal version.
* Enforces grace windows: items in the DEPRECATED_WARN window emit a warning;
  items past their removal version raise ``DeprecationExpiredError``.
* Exposes a ``@deprecated`` decorator for functions and classes.
* Ships a process-wide registry so all modules share one canonical source of truth.

Version comparison uses a simple (major, minor, patch) tuple — no third-party
package required.
"""
from __future__ import annotations

import enum
import functools
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

__all__ = [
    "GraceWindowStatus",
    "DeprecationEntry",
    "DeprecationExpiredError",
    "DeprecationRegistry",
    "deprecated",
    "get_deprecation_registry",
]

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)

# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

VersionTuple = Tuple[int, int, int]


def _parse_version(v: str) -> VersionTuple:
    """Parse ``"major.minor.patch"`` or ``"major.minor"`` → int triple."""
    parts = v.split(".")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse version: {v!r}")
    major = int(parts[0])
    minor = int(parts[1])
    patch = int(parts[2]) if len(parts) > 2 else 0
    return (major, minor, patch)


# ---------------------------------------------------------------------------
# GraceWindowStatus
# ---------------------------------------------------------------------------


class GraceWindowStatus(str, enum.Enum):
    """Status of a deprecation entry relative to the current software version."""

    CURRENT = "current"               # Not deprecated; in active use
    DEPRECATED_WARN = "deprecated_warn"   # Deprecated; grace window open; warning
    DEPRECATED_FAIL = "deprecated_fail"   # Past removal version; hard fail


# ---------------------------------------------------------------------------
# DeprecationEntry — immutable record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeprecationEntry:
    """Immutable deprecation record for one named item.

    Parameters
    ----------
    item_id:
        Unique identifier for the deprecated item (e.g.
        ``"comm_protocol.CommMessage.payload_v1"``).
    deprecated_since:
        Version string at which the item became deprecated (e.g. ``"2.3.0"``).
    removal_version:
        Version string at which the item WILL BE (or has been) removed.
    migration_path:
        Human-readable description of the replacement / migration steps.
    registered_at_mono:
        ``time.monotonic()`` at registration time (for diagnostics).
    """

    item_id: str
    deprecated_since: str
    removal_version: str
    migration_path: str
    registered_at_mono: float = 0.0

    def status(self, current_version: str) -> GraceWindowStatus:
        """Evaluate the grace window status relative to *current_version*."""
        cv = _parse_version(current_version)
        ds = _parse_version(self.deprecated_since)
        rv = _parse_version(self.removal_version)

        if cv < ds:
            return GraceWindowStatus.CURRENT
        if cv >= rv:
            return GraceWindowStatus.DEPRECATED_FAIL
        return GraceWindowStatus.DEPRECATED_WARN


# ---------------------------------------------------------------------------
# DeprecationExpiredError
# ---------------------------------------------------------------------------


class DeprecationExpiredError(RuntimeError):
    """Raised when a caller uses an item whose removal version has been reached.

    Attributes
    ----------
    item_id:
        The deprecated item identifier.
    removal_version:
        The version at which removal was scheduled.
    migration_path:
        Instructions for migrating away from the deprecated item.
    """

    def __init__(self, entry: DeprecationEntry, current_version: str) -> None:
        self.item_id = entry.item_id
        self.removal_version = entry.removal_version
        self.migration_path = entry.migration_path
        super().__init__(
            f"'{entry.item_id}' was scheduled for removal in "
            f"v{entry.removal_version} (current: v{current_version}). "
            f"Migrate: {entry.migration_path}"
        )


# ---------------------------------------------------------------------------
# DeprecationRegistry
# ---------------------------------------------------------------------------


class DeprecationRegistry:
    """Central registry of deprecation entries.

    Usage::

        registry = get_deprecation_registry()
        registry.register(
            "comm_protocol.CommMessage.payload_v1",
            deprecated_since="2.3.0",
            removal_version="3.0.0",
            migration_path="Use CommMessage.payload_v2 instead.",
        )
        registry.check("comm_protocol.CommMessage.payload_v1", "2.5.0")
        # → GraceWindowStatus.DEPRECATED_WARN  (logs a warning)
    """

    def __init__(self) -> None:
        self._entries: Dict[str, DeprecationEntry] = {}

    def register(
        self,
        item_id: str,
        *,
        deprecated_since: str,
        removal_version: str,
        migration_path: str,
    ) -> DeprecationEntry:
        """Register (or replace) a deprecation entry.

        Validates that ``deprecated_since < removal_version``.
        """
        ds = _parse_version(deprecated_since)
        rv = _parse_version(removal_version)
        if ds >= rv:
            raise ValueError(
                f"deprecated_since={deprecated_since!r} must be < "
                f"removal_version={removal_version!r}"
            )
        entry = DeprecationEntry(
            item_id=item_id,
            deprecated_since=deprecated_since,
            removal_version=removal_version,
            migration_path=migration_path,
            registered_at_mono=time.monotonic(),
        )
        self._entries[item_id] = entry
        logger.debug(
            "[DeprecationRegistry] registered '%s': deprecated=%s removal=%s",
            item_id, deprecated_since, removal_version,
        )
        return entry

    def check(self, item_id: str, current_version: str) -> GraceWindowStatus:
        """Evaluate and enforce the deprecation status for *item_id*.

        * ``CURRENT`` → returns silently.
        * ``DEPRECATED_WARN`` → logs a WARNING and returns.
        * ``DEPRECATED_FAIL`` → raises ``DeprecationExpiredError``.

        Returns the ``GraceWindowStatus`` (never raises for CURRENT/WARN).
        """
        entry = self._entries.get(item_id)
        if entry is None:
            return GraceWindowStatus.CURRENT

        status = entry.status(current_version)
        if status == GraceWindowStatus.DEPRECATED_WARN:
            logger.warning(
                "[DeprecationRegistry] DEPRECATED '%s' (since v%s, removal v%s). "
                "Migrate: %s",
                entry.item_id,
                entry.deprecated_since,
                entry.removal_version,
                entry.migration_path,
            )
        elif status == GraceWindowStatus.DEPRECATED_FAIL:
            raise DeprecationExpiredError(entry, current_version)
        return status

    def get_all_expired(self, current_version: str) -> List[DeprecationEntry]:
        """Return all entries whose removal version has been reached."""
        return [
            e for e in self._entries.values()
            if e.status(current_version) == GraceWindowStatus.DEPRECATED_FAIL
        ]

    def get_all_warned(self, current_version: str) -> List[DeprecationEntry]:
        """Return all entries that are in the DEPRECATED_WARN window."""
        return [
            e for e in self._entries.values()
            if e.status(current_version) == GraceWindowStatus.DEPRECATED_WARN
        ]

    def all_entries(self) -> Dict[str, DeprecationEntry]:
        """Return a snapshot of all registered entries."""
        return dict(self._entries)

    def has_migration_path(self, item_id: str) -> bool:
        """Return True if the entry exists and has a non-empty migration path."""
        entry = self._entries.get(item_id)
        return bool(entry and entry.migration_path.strip())


# ---------------------------------------------------------------------------
# @deprecated decorator
# ---------------------------------------------------------------------------


def deprecated(
    since: str,
    remove_by: str,
    migrate_to: str,
    registry: Optional[DeprecationRegistry] = None,
) -> Callable[[F], F]:
    """Decorator that registers and enforces deprecation for a function or class.

    Usage::

        @deprecated(since="2.3.0", remove_by="3.0.0",
                    migrate_to="Use new_function() instead.")
        def old_function():
            ...

    When the decorated callable is *called* (not just defined), the registry
    ``check()`` runs against the version embedded in the item_id.  The version
    is read from ``<module>.__version__`` if available, else ``"0.0.0"``.

    The item_id used for registration is ``"<module>.<qualname>"``.
    """
    _registry = registry or get_deprecation_registry()

    def decorator(fn: F) -> F:
        item_id = f"{fn.__module__}.{fn.__qualname__}"
        _registry.register(
            item_id,
            deprecated_since=since,
            removal_version=remove_by,
            migration_path=migrate_to,
        )

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            import sys
            module = sys.modules.get(fn.__module__)
            current_version = getattr(module, "__version__", "0.0.0")
            _registry.check(item_id, current_version)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_registry: Optional[DeprecationRegistry] = None


def get_deprecation_registry() -> DeprecationRegistry:
    """Return (lazily creating) the process-wide DeprecationRegistry."""
    global _g_registry
    if _g_registry is None:
        _g_registry = DeprecationRegistry()
    return _g_registry
