"""Tests for OpportunityMinerSensor auto-submit threshold (Phase 2C.4)."""
from pathlib import Path
from unittest.mock import MagicMock


async def test_high_confidence_candidate_no_human_ack(tmp_path):
    """CC well above threshold → confidence high → requires_human_ack=False."""
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    src = tmp_path / "complex.py"
    lines = ["def foo(x):\n"] + [f"    if x == {i}: return {i}\n" for i in range(30)] + ["    return -1\n"]
    src.write_text("".join(lines))

    captured = []
    async def capture_ingest(env):
        captured.append(env)
        return "enqueued"

    router = MagicMock()
    router.ingest = capture_ingest
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=5,
        auto_submit_threshold=0.75,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is False


async def test_low_confidence_candidate_requires_human_ack(tmp_path):
    """CC just above threshold → confidence low → requires_human_ack=True."""
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    src = tmp_path / "mild.py"
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
        auto_submit_threshold=0.75,
    )
    await sensor.scan_once()
    assert len(captured) == 1
    assert captured[0].requires_human_ack is True


def test_default_auto_submit_threshold():
    """Default threshold is 0.75 when not specified."""
    from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import OpportunityMinerSensor
    router = MagicMock()
    sensor = OpportunityMinerSensor(repo_root=Path("."), router=router)
    assert sensor._auto_submit_threshold == 0.75


def test_intake_layer_config_has_miner_auto_submit_threshold():
    """IntakeLayerConfig exposes miner_auto_submit_threshold with default 0.75."""
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    cfg = IntakeLayerConfig(project_root=Path("/tmp"))
    assert cfg.miner_auto_submit_threshold == 0.75


def test_intake_layer_config_from_env_reads_threshold(monkeypatch, tmp_path):
    """JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD env var is read."""
    monkeypatch.setenv("JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD", "0.60")
    from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
    cfg = IntakeLayerConfig.from_env(project_root=tmp_path)
    assert cfg.miner_auto_submit_threshold == 0.60
