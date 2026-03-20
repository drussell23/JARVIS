"""
Global ID schema and freshness classes for the Unified Knowledge Fabric.

ID format: kg://partition/entity_type/entity_name
Partitions: scene | semantic | trinity
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

_ID_PATTERN = re.compile(
    r"^kg://(?P<partition>[^/]+)/(?P<entity_type>[^/]+)/(?P<entity_name>.+)$"
)


@dataclass(frozen=True)
class KGEntityId:
    """Parsed representation of a global Knowledge-Graph entity ID."""

    partition: str      # "scene" | "semantic" | "trinity"
    entity_type: str    # e.g. "button", "pattern", "audit"
    entity_name: str    # e.g. "submit-001", "gmail-compose"
    full_id: str        # original kg:// string, preserved verbatim


def parse_entity_id(id_str: str) -> KGEntityId:
    """Parse a kg:// entity ID string into a :class:`KGEntityId`.

    Parameters
    ----------
    id_str:
        A string of the form ``kg://partition/entity_type/entity_name``.

    Returns
    -------
    KGEntityId

    Raises
    ------
    ValueError
        If *id_str* does not match the expected format.
    """
    m = _ID_PATTERN.match(id_str)
    if m is None:
        raise ValueError(
            f"Invalid KG entity ID {id_str!r}. "
            "Expected format: kg://partition/entity_type/entity_name"
        )
    return KGEntityId(
        partition=m.group("partition"),
        entity_type=m.group("entity_type"),
        entity_name=m.group("entity_name"),
        full_id=id_str,
    )


class _FreshnessClassMeta(type(Enum)):  # type: ignore[misc]
    """Metaclass shim — not used externally; keeps Enum clean."""


class FreshnessClass(Enum):
    """Data freshness tier with associated TTL policy.

    Attributes
    ----------
    ttl_seconds:
        The time-to-live in seconds, or ``None`` for durable (no expiry).
    """

    HOT = 5          # L1 scene partition — per-frame data, expires in 5 s
    WARM = 86400     # L2 semantic partition — day-level patterns
    DURABLE = None   # L3 trinity partition — persistent audit / long-term facts

    def __new__(cls, ttl: Optional[int]) -> "FreshnessClass":
        obj = object.__new__(cls)
        obj._value_ = ttl      # value == ttl_seconds for convenient access
        return obj

    @property
    def ttl_seconds(self) -> Optional[int]:
        """TTL in seconds, or ``None`` for durable storage."""
        return self._value_
