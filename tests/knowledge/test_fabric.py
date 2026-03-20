"""Tests for Unified Knowledge Fabric."""
import time
import pytest
from backend.knowledge.schema import (
    KGEntityId, FreshnessClass, parse_entity_id,
)
from backend.knowledge.scene_partition import ScenePartition
from backend.knowledge.fabric import KnowledgeFabric


class TestSchema:
    def test_parse_scene_id(self):
        eid = parse_entity_id("kg://scene/button/submit-001")
        assert eid.partition == "scene"
        assert eid.entity_type == "button"
        assert eid.entity_name == "submit-001"

    def test_parse_semantic_id(self):
        eid = parse_entity_id("kg://semantic/pattern/gmail-compose")
        assert eid.partition == "semantic"

    def test_parse_trinity_id(self):
        eid = parse_entity_id("kg://trinity/audit/action-001")
        assert eid.partition == "trinity"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            parse_entity_id("not-a-valid-id")

    def test_freshness_classes(self):
        assert FreshnessClass.HOT.ttl_seconds == 5
        assert FreshnessClass.WARM.ttl_seconds == 86400
        assert FreshnessClass.DURABLE.ttl_seconds is None


class TestScenePartition:
    @pytest.fixture
    def partition(self):
        return ScenePartition()

    def test_write_and_read(self, partition):
        partition.write("kg://scene/button/submit-001", {
            "position": (523, 187),
            "confidence": 0.92,
            "element_type": "button",
            "text_content": "Submit",
        })
        result = partition.read("kg://scene/button/submit-001")
        assert result is not None
        assert result["position"] == (523, 187)

    def test_read_miss_returns_none(self, partition):
        assert partition.read("kg://scene/button/nonexistent") is None

    def test_ttl_expiry(self, partition):
        partition.write("kg://scene/button/old", {
            "position": (100, 100),
        }, ttl_seconds=0.01)  # 10ms TTL
        time.sleep(0.02)
        assert partition.read("kg://scene/button/old") is None

    def test_update_refreshes_ttl(self, partition):
        partition.write("kg://scene/button/btn", {"position": (100, 100)}, ttl_seconds=1.0)
        partition.write("kg://scene/button/btn", {"position": (200, 200)}, ttl_seconds=1.0)
        result = partition.read("kg://scene/button/btn")
        assert result["position"] == (200, 200)

    def test_query_nearest(self, partition):
        partition.write("kg://scene/button/a", {"position": (100, 100), "confidence": 0.9})
        partition.write("kg://scene/button/b", {"position": (500, 500), "confidence": 0.8})
        nearest = partition.query_nearest((110, 110), max_distance=50)
        assert nearest is not None
        assert nearest["position"] == (100, 100)

    def test_query_nearest_no_match(self, partition):
        partition.write("kg://scene/button/far", {"position": (500, 500), "confidence": 0.9})
        nearest = partition.query_nearest((100, 100), max_distance=50)
        assert nearest is None

    def test_clear_all(self, partition):
        partition.write("kg://scene/button/a", {"position": (100, 100)})
        partition.clear()
        assert partition.read("kg://scene/button/a") is None


class TestKnowledgeFabric:
    @pytest.fixture
    def fabric(self):
        return KnowledgeFabric()

    def test_write_scene_routes_to_scene_partition(self, fabric):
        fabric.write("kg://scene/button/test", {"position": (100, 200)})
        result = fabric.query("kg://scene/button/test")
        assert result is not None
        assert result["position"] == (100, 200)

    def test_query_miss_returns_none(self, fabric):
        assert fabric.query("kg://scene/button/nonexistent") is None

    def test_semantic_partition_placeholder(self, fabric):
        # Semantic and trinity partitions are remote — local stub returns None
        result = fabric.query("kg://semantic/pattern/test")
        assert result is None  # no local semantic partition in this task

    def test_query_nearest_delegates_to_scene(self, fabric):
        fabric.write("kg://scene/button/a", {"position": (100, 100), "confidence": 0.9})
        result = fabric.query_nearest_element((110, 110), max_distance=50)
        assert result is not None
