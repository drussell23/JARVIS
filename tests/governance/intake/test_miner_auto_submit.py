"""Tests for OpportunityMinerSensor AC2 safety invariant.

AC2: ALL miner-generated envelopes always have requires_human_ack=True,
regardless of confidence level. AI-discovered opportunities must always
be human-approved before execution.
"""
from pathlib import Path
from unittest.mock import MagicMock


async def test_high_confidence_candidate_still_requires_human_ack(tmp_path):
    """CC well above threshold → confidence high → requires_human_ack=True (AC2)."""
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    # File must be at depth >= 2 from repo_root to pass _is_production_code filter
    pkg = tmp_path / "backend" / "core"
    pkg.mkdir(parents=True)
    src = pkg / "complex.py"
    lines = ["def foo(x):\n"] + [f"    if x == {i}: return {i}\n" for i in range(30)] + ["    return -1\n"]
    src.write_text("".join(lines))

    captured = []
    async def capture_ingest(env):
        captured.append(env)
        return "pending_ack"

    router = MagicMock()
    router.ingest = capture_ingest
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=5,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is True


async def test_low_confidence_candidate_requires_human_ack(tmp_path):
    """CC just above threshold → confidence low → requires_human_ack=True (AC2)."""
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    # File must be at depth >= 2 from repo_root to pass _is_production_code filter
    pkg = tmp_path / "backend" / "core"
    pkg.mkdir(parents=True, exist_ok=True)
    src = pkg / "mild.py"
    lines = ["def foo(x):\n"] + [f"    if x == {i}: return {i}\n" for i in range(6)] + ["    return -1\n"]
    src.write_text("".join(lines))

    captured = []
    async def capture_ingest(env):
        captured.append(env)
        return "pending_ack"

    router = MagicMock()
    router.ingest = capture_ingest
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=5,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is True


def test_miner_has_no_auto_submit_threshold():
    """Miner sensor should not accept auto_submit_threshold (AC2 safety invariant)."""
    import inspect
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    sig = inspect.signature(OpportunityMinerSensor.__init__)
    assert "auto_submit_threshold" not in sig.parameters


def test_intake_layer_config_has_miner_auto_submit_threshold():
    """IntakeLayerConfig retains miner_auto_submit_threshold field (backwards compat)."""
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    cfg = IntakeLayerConfig(project_root=Path("/tmp"))
    assert cfg.miner_auto_submit_threshold == 0.75


def test_intake_layer_config_from_env_reads_threshold(monkeypatch, tmp_path):
    """JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD env var is still parsed."""
    monkeypatch.setenv("JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD", "0.60")
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    cfg = IntakeLayerConfig.from_env(project_root=tmp_path)
    assert cfg.miner_auto_submit_threshold == 0.60
