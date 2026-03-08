"""tests/governance/autonomy/test_state.py"""
import json
import pytest
from pathlib import Path
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier, GraduationMetrics, SignalAutonomyConfig,
)


def _make_config(tier=AutonomyTier.GOVERNED, source="intent:test_failure", repo="jarvis"):
    return SignalAutonomyConfig(
        trigger_source=source, repo=repo, canary_slice="tests/",
        current_tier=tier, graduation_metrics=GraduationMetrics(successful_ops=10),
    )


class TestStateSaveLoad:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState
        state_file = tmp_path / "autonomy" / "state.json"
        state = AutonomyState(state_path=state_file)
        configs = (_make_config(), _make_config(AutonomyTier.OBSERVE, repo="prime"))
        state.save(configs)
        loaded = state.load()
        assert len(loaded) == 2
        assert loaded[0].current_tier == AutonomyTier.GOVERNED
        assert loaded[0].graduation_metrics.successful_ops == 10
        assert loaded[1].current_tier == AutonomyTier.OBSERVE

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState
        state = AutonomyState(state_path=tmp_path / "missing" / "state.json")
        assert state.load() == ()

    def test_load_corrupted_file_returns_empty(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json {{{")
        state = AutonomyState(state_path=state_file)
        assert state.load() == ()


class TestStateReset:
    def test_reset_deletes_file(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState
        state_file = tmp_path / "state.json"
        state = AutonomyState(state_path=state_file)
        state.save((_make_config(),))
        assert state_file.exists()
        state.reset()
        assert not state_file.exists()

    def test_reset_missing_file_is_noop(self, tmp_path: Path):
        from backend.core.ouroboros.governance.autonomy.state import AutonomyState
        state = AutonomyState(state_path=tmp_path / "nonexistent.json")
        state.reset()  # Should not raise
