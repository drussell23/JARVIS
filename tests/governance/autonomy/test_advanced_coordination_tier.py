"""Tests for L4 dynamic tier override recommendations."""
import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestDynamicTierOverride:
    def test_recommend_promotion(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        svc.recommend_tier_change(
            repo="jarvis",
            canary_slice="tests/",
            recommended_tier="GOVERNED",
            evidence={"success_rate": 0.95, "sample_size": 20},
        )

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.RECOMMEND_TIER_CHANGE
        assert cmd.payload["recommended_tier"] == "GOVERNED"
        assert cmd.payload["evidence"]["success_rate"] == 0.95

    def test_reject_without_evidence(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        svc.recommend_tier_change(
            repo="jarvis",
            canary_slice="tests/",
            recommended_tier="AUTONOMOUS",
            evidence={},  # empty evidence
        )

        # Should not emit without evidence
        assert bus.qsize() == 0
