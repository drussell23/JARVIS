"""Tests for L4 consensus voting — multi-brain validation."""
import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
    ConsensusResult,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestConsensusVoting:
    def test_majority_agree_emits_consensus(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_1",
            candidates=["candidate_A"],
            votes={"qwen_coder": "approve", "qwen_coder_32b": "approve", "phi3_lightweight": "reject"},
        )

        assert result.majority is True
        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REPORT_CONSENSUS
        assert cmd.payload["majority"] is True

    def test_no_majority_escalates(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_2",
            candidates=["candidate_A"],
            votes={"qwen_coder": "approve", "qwen_coder_32b": "reject", "phi3_lightweight": "reject"},
        )

        assert result.majority is False
        # Should still emit consensus result (with majority=False)
        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.payload["majority"] is False

    def test_unanimous_agreement(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_3",
            candidates=["candidate_A"],
            votes={"a": "approve", "b": "approve"},
        )

        assert result.majority is True

    def test_consensus_result_fields(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        votes = {"brain_a": "approve", "brain_b": "reject", "brain_c": "approve"}
        result = svc.record_vote(
            op_id="op_fields",
            candidates=["c1"],
            votes=votes,
        )

        assert result.op_id == "op_fields"
        assert result.votes == votes
        assert result.approved_count == 2
        assert result.total_count == 3
        assert result.majority is True

    def test_command_envelope_fields(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        svc.record_vote(
            op_id="op_env",
            candidates=["c1", "c2"],
            votes={"x": "approve"},
        )

        cmd = bus._heap[0][2]
        assert cmd.source_layer == "L4"
        assert cmd.target_layer == "L1"
        assert cmd.command_type == CommandType.REPORT_CONSENSUS
        assert cmd.ttl_s == 300.0
        assert cmd.payload["op_id"] == "op_env"
        assert cmd.payload["candidates"] == ["c1", "c2"]
        assert cmd.payload["votes"] == {"x": "approve"}
        assert cmd.payload["majority"] is True

    def test_single_reject_no_majority(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_single_rej",
            candidates=["c1"],
            votes={"only_brain": "reject"},
        )

        assert result.majority is False
        assert result.approved_count == 0
        assert result.total_count == 1

    def test_even_split_no_majority(self, state_dir):
        """50/50 split should NOT be majority (need strictly > 50%)."""
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_even",
            candidates=["c1"],
            votes={"a": "approve", "b": "reject"},
        )

        assert result.majority is False
        assert result.approved_count == 1
        assert result.total_count == 2
