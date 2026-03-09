"""IntakeLayerService creates one sensor per registered repo when registry is set."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
)
from backend.core.ouroboros.governance.intake.sensors import (
    OpportunityMinerSensor,
    TestFailureSensor,
)
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig,
    RepoRegistry,
)

_ROUTER_PATH = "backend.core.ouroboros.governance.intake.UnifiedIntakeRouter"
_BACKLOG_START = (
    "backend.core.ouroboros.governance.intake.sensors.backlog_sensor.BacklogSensor.start"
)
_TF_START = (
    "backend.core.ouroboros.governance.intake.sensors.test_failure_sensor.TestFailureSensor.start"
)
_MINER_START = (
    "backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor.OpportunityMinerSensor.start"
)


def _make_registry(tmp_path: Path) -> RepoRegistry:
    for name in ("jarvis", "prime", "reactor-core"):
        (tmp_path / name).mkdir(exist_ok=True)
    return RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=tmp_path / "jarvis", canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=tmp_path / "prime", canary_slices=("tests/",)),
        RepoConfig(name="reactor-core", local_path=tmp_path / "reactor-core", canary_slices=("tests/",)),
    ))


def _mock_gls() -> MagicMock:
    gls = MagicMock()
    gls.submit = AsyncMock()
    return gls


async def _run_build(svc: IntakeLayerService) -> None:
    with patch(_ROUTER_PATH) as MockRouter, \
         patch(_BACKLOG_START, new=AsyncMock()), \
         patch(_TF_START, new=AsyncMock()), \
         patch(_MINER_START, new=AsyncMock()):
        MockRouter.return_value.start = AsyncMock()
        await svc._build_components()


async def test_three_miner_sensors_created_for_three_repos(tmp_path):
    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(
        project_root=tmp_path / "jarvis",
        repo_registry=registry,
    )
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)
    await _run_build(svc)

    miners = [s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)]
    assert len(miners) == 3
    assert {s._repo for s in miners} == {"jarvis", "prime", "reactor-core"}


async def test_three_test_failure_sensors_created_for_three_repos(tmp_path):
    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(
        project_root=tmp_path / "jarvis",
        repo_registry=registry,
    )
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)
    await _run_build(svc)

    tf_sensors = [s for s in svc._sensors if isinstance(s, TestFailureSensor)]
    assert len(tf_sensors) == 3
    assert {s._repo for s in tf_sensors} == {"jarvis", "prime", "reactor-core"}


async def test_single_sensor_fallback_when_no_registry(tmp_path):
    config = IntakeLayerConfig(
        project_root=tmp_path,
        repo_registry=None,
    )
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)
    await _run_build(svc)

    miners = [s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)]
    tf_sensors = [s for s in svc._sensors if isinstance(s, TestFailureSensor)]

    assert len(miners) == 1
    assert miners[0]._repo == "jarvis"
    assert len(tf_sensors) == 1
    assert tf_sensors[0]._repo == "jarvis"


async def test_miner_sensor_root_matches_registry_local_path(tmp_path):
    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(
        project_root=tmp_path / "jarvis",
        repo_registry=registry,
    )
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)
    await _run_build(svc)

    miners = {s._repo: s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)}
    assert miners["prime"]._repo_root == tmp_path / "prime"
    assert miners["reactor-core"]._repo_root == tmp_path / "reactor-core"
