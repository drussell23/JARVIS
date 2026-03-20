"""
FabricRouter — maps a global KG entity ID to its partition name.

Routing is a pure, stateless function derived from parsing the entity ID.
No external dependencies; O(1) cost via regex.
"""

from __future__ import annotations

from backend.knowledge.schema import parse_entity_id

_VALID_PARTITIONS = frozenset({"scene", "semantic", "trinity"})


def route_partition(entity_id: str) -> str:
    """Return the partition name encoded in *entity_id*.

    Parameters
    ----------
    entity_id:
        A ``kg://`` entity ID string such as ``kg://scene/button/submit-001``.

    Returns
    -------
    str
        One of ``"scene"``, ``"semantic"``, or ``"trinity"``.

    Raises
    ------
    ValueError
        If *entity_id* is malformed or encodes an unknown partition.
    """
    eid = parse_entity_id(entity_id)
    if eid.partition not in _VALID_PARTITIONS:
        raise ValueError(
            f"Unknown partition {eid.partition!r} in entity ID {entity_id!r}. "
            f"Valid partitions: {sorted(_VALID_PARTITIONS)}"
        )
    return eid.partition
