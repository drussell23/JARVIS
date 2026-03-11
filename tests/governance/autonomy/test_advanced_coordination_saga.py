"""Tests for L4 cross-repo saga persistence and idempotency."""
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
    SagaState,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestSagaPersistence:
    def test_create_saga_persists_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis", "jarvis-prime"],
            patches={"jarvis": "patch1", "jarvis-prime": "patch2"},
        )

        state_file = state_dir / f"saga_{saga_id}.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["saga_id"] == saga_id
        assert data["phase"] == "CREATED"

    def test_advance_saga_updates_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc.advance_saga(saga_id, repo="jarvis", success=True)

        data = json.loads((state_dir / f"saga_{saga_id}.json").read_text())
        assert "jarvis" in data["repos_applied"]

    def test_idempotent_advance(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc.advance_saga(saga_id, repo="jarvis", success=True)
        svc.advance_saga(saga_id, repo="jarvis", success=True)  # duplicate

        data = json.loads((state_dir / f"saga_{saga_id}.json").read_text())
        assert data["repos_applied"].count("jarvis") == 1

    def test_saga_emits_submit_command(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis"],
            patches={"jarvis": "patch_data"},
        )
        svc.request_saga_submit(saga_id)

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REQUEST_SAGA_SUBMIT
        assert cmd.payload["saga_id"] == saga_id

    def test_restart_recovers_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)

        # First instance creates saga
        svc1 = AdvancedAutonomyService(command_bus=bus, config=config)
        saga_id = svc1.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc1.advance_saga(saga_id, repo="jarvis", success=True)

        # Second instance (simulating restart) recovers
        svc2 = AdvancedAutonomyService(command_bus=bus, config=config)
        state = svc2.get_saga_state(saga_id)
        assert state is not None
        assert "jarvis" in state.repos_applied

    def test_saga_phase_transitions_to_completed(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis", "jarvis-prime"],
            patches={"jarvis": "p1", "jarvis-prime": "p2"},
        )
        svc.advance_saga(saga_id, repo="jarvis", success=True)
        state = svc.get_saga_state(saga_id)
        assert state.phase == "IN_PROGRESS"

        svc.advance_saga(saga_id, repo="jarvis-prime", success=True)
        state = svc.get_saga_state(saga_id)
        assert state.phase == "COMPLETED"

    def test_saga_phase_transitions_to_failed(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis", "jarvis-prime"],
            patches={"jarvis": "p1", "jarvis-prime": "p2"},
        )
        svc.advance_saga(saga_id, repo="jarvis", success=False)
        state = svc.get_saga_state(saga_id)
        assert state.phase == "FAILED"
        assert "jarvis" in state.repos_failed

    def test_advance_unknown_saga_is_noop(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        # Should not raise
        svc.advance_saga("nonexistent", repo="jarvis", success=True)

    def test_request_submit_unknown_saga_is_noop(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        # Should not raise, and bus should remain empty
        svc.request_saga_submit("nonexistent")
        assert bus.qsize() == 0

    def test_get_saga_state_returns_none_for_unknown(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        assert svc.get_saga_state("nonexistent") is None

    def test_checksum_integrity_on_persist(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})

        data = json.loads((state_dir / f"saga_{saga_id}.json").read_text())
        assert "checksum" in data
        assert len(data["checksum"]) == 16  # sha256[:16]

    def test_corrupted_state_file_skipped_on_load(self, state_dir):
        # Write a corrupt file
        (state_dir / "saga_bad123.json").write_text("not json{{{")

        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        assert svc.get_saga_state("bad123") is None

    def test_submit_command_has_correct_envelope_fields(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis"],
            patches={"jarvis": "patch_data"},
        )
        svc.request_saga_submit(saga_id)

        cmd = bus._heap[0][2]
        assert cmd.source_layer == "L4"
        assert cmd.target_layer == "L1"
        assert cmd.ttl_s == 300.0
        assert cmd.payload["idempotency_key"] == saga_id

    def test_idempotent_failure_advance(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc.advance_saga(saga_id, repo="jarvis", success=False)
        svc.advance_saga(saga_id, repo="jarvis", success=False)  # duplicate

        state = svc.get_saga_state(saga_id)
        assert state.repos_failed.count("jarvis") == 1

    def test_multiple_sagas_independent(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga1 = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p1"})
        saga2 = svc.create_saga(repos=["prime"], patches={"prime": "p2"})

        svc.advance_saga(saga1, repo="jarvis", success=True)

        state1 = svc.get_saga_state(saga1)
        state2 = svc.get_saga_state(saga2)
        assert state1.phase == "COMPLETED"
        assert state2.phase == "CREATED"


class TestSagaState:
    def test_checksum_changes_with_phase(self):
        s1 = SagaState(saga_id="abc", repos=["r"], patches={"r": "p"}, phase="CREATED")
        s2 = SagaState(saga_id="abc", repos=["r"], patches={"r": "p"}, phase="IN_PROGRESS")
        assert s1.checksum != s2.checksum

    def test_default_idempotency_key_is_saga_id(self):
        s = SagaState(saga_id="test123", repos=["r"], patches={"r": "p"})
        assert s.idempotency_key == "test123"

    def test_repos_applied_sorted_for_checksum(self):
        s1 = SagaState(
            saga_id="abc", repos=["a", "b"], patches={"a": "p", "b": "p"},
            repos_applied=["a", "b"],
        )
        s2 = SagaState(
            saga_id="abc", repos=["a", "b"], patches={"a": "p", "b": "p"},
            repos_applied=["b", "a"],
        )
        assert s1.checksum == s2.checksum
