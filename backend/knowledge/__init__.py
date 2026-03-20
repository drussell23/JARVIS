"""
backend.knowledge — Unified Knowledge Fabric.

Public surface:
    KnowledgeFabric   — one API, three partition routing
    ScenePartition    — L1 hot in-memory cache
    KGEntityId        — parsed global entity ID
    FreshnessClass    — TTL tier enum (HOT / WARM / DURABLE)
    parse_entity_id   — parse a kg:// string into KGEntityId
    route_partition   — return the partition name for an entity ID
"""

from backend.knowledge.schema import KGEntityId, FreshnessClass, parse_entity_id
from backend.knowledge.scene_partition import ScenePartition
from backend.knowledge.fabric_router import route_partition
from backend.knowledge.fabric import KnowledgeFabric

__all__ = [
    "KnowledgeFabric",
    "ScenePartition",
    "KGEntityId",
    "FreshnessClass",
    "parse_entity_id",
    "route_partition",
]
