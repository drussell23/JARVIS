# Multi-Repo Sensor Extension Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend intake sensors to discover and submit work items from all registered repos (jarvis, prime, reactor-core) instead of always hardcoding "jarvis".

**Architecture:** Three targeted changes. `IntakeLayerConfig` gains an optional `repo_registry` field. `IntakeLayerService._build_components()` loops over enabled repos to create one `TestFailureSensor` and one `OpportunityMinerSensor` per repo when a registry is present (falls back to single "jarvis" sensor when not). `VoiceCommandSensor` is fixed to use `payload.repo` instead of the hardcoded `self._repo`. `BacklogSensor` is already multi-repo ready — no changes needed.

**Tech Stack:** Python 3.9, asyncio, pytest (asyncio_mode=auto — never add @pytest.mark.asyncio), dataclasses

---

## Context for the implementer

### Key files
- `backend/core/ouroboros/governance/intake/intake_layer_service.py` — `IntakeLayerConfig` (lines 42-99), `_build_components()` (lines 302-366)
- `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py` — line 96: `repo=self._repo` is the bug
- `backend/core/ouroboros/governance/multi_repo/registry.py` — `RepoRegistry`, `RepoConfig`
- Tests live in `tests/governance/intake/` (create if missing) and `tests/test_ouroboros_governance/`

### Current state of `_build_components()` sensor instantiation (lines 341-362):
```python
test_failure_sensor = TestFailureSensor(
    repo="jarvis",           # <-- hardcoded
    router=self._router,
)
opportunity_miner_sensor = OpportunityMinerSensor(
    repo_root=self._config.project_root,
    router=self._router,
    scan_paths=self._config.miner_scan_paths,
    complexity_threshold=self._config.miner_complexity_threshold,
    poll_interval_s=self._config.miner_scan_interval_s,
    auto_submit_threshold=self._config.miner_auto_submit_threshold,
    # repo defaults to "jarvis"
)
self._voice_sensor = VoiceCommandSensor(
    router=self._router,
    repo="jarvis",           # <-- hardcoded
    stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
)
self._sensors = [backlog_sensor, test_failure_sensor, opportunity_miner_sensor]
```

### VoiceCommandSensor bug (voice_command_sensor.py line 96):
```python
envelope = make_envelope(
    source="voice_human",
    ...
    repo=self._repo,   # BUG: ignores payload.repo
    ...
)
```
`payload.repo` exists on the `VoiceCommandPayload` dataclass but is never used.

### Sensor constructor signatures (already correct, no changes needed):
```python
# TestFailureSensor
def __init__(self, repo: str, router: Any, test_watcher: Any = None) -> None:

# OpportunityMinerSensor
def __init__(self, repo_root: Path, router: Any, scan_paths=None,
             complexity_threshold=10, repo: str = "jarvis",
             poll_interval_s=3600.0, auto_submit_threshold=0.75) -> None:
```

---

## Task 1: Add `repo_registry` to `IntakeLayerConfig`

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py:1-40` (imports) and `42-99` (IntakeLayerConfig)
- Test: `tests/governance/intake/test_intake_layer_config.py` (create file and directory if needed)

### Step 1: Create test directory if needed

```bash
mkdir -p tests/governance/intake
touch tests/governance/intake/__init__.py
```

### Step 2: Write the failing test

Create `tests/governance/intake/test_intake_layer_config.py`:

```python
"""Tests for IntakeLayerConfig multi-repo registry field."""
from pathlib import Path

from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig, RepoRegistry,
)


def _make_registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=tmp_path / "jarvis", canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=tmp_path / "prime", canary_slices=("tests/",)),
    ))


def test_intake_layer_config_accepts_repo_registry(tmp_path):
    """IntakeLayerConfig can be constructed with repo_registry."""
    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(project_root=tmp_path, repo_registry=registry)
    assert config.repo_registry is registry


def test_intake_layer_config_defaults_registry_to_none(tmp_path):
    """IntakeLayerConfig.repo_registry defaults to None (backward compat)."""
    config = IntakeLayerConfig(project_root=tmp_path)
    assert config.repo_registry is None
```

### Step 3: Run to verify failure

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_config.py -v --tb=short
```
Expected: FAIL — `IntakeLayerConfig() got unexpected keyword argument 'repo_registry'`

### Step 4: Implement

In `backend/core/ouroboros/governance/intake/intake_layer_service.py`:

**4a.** At the top of the file, find the existing `from typing import ...` import and add `TYPE_CHECKING` if not present. Then add the TYPE_CHECKING guard:

```python
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry
```

Check what's already imported before editing — only add what's missing.

**4b.** Add `repo_registry` field to `IntakeLayerConfig` after `test_failure_min_count_for_narration`:

```python
    test_failure_min_count_for_narration: int = 2
    repo_registry: Optional["RepoRegistry"] = None  # Forward ref; multi-repo sensor fan-out
```

That's the only change to `IntakeLayerConfig`. Do NOT touch `from_env()` — registry is passed in explicitly by `IntakeLayerService`'s caller (GovernedLoopService already builds it).

### Step 5: Run test

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_config.py -v --tb=short
```
Expected: 2 PASSED.

### Step 6: Commit

```bash
git add tests/governance/intake/__init__.py \
        tests/governance/intake/test_intake_layer_config.py \
        backend/core/ouroboros/governance/intake/intake_layer_service.py
git commit -m "$(cat <<'EOF'
feat(intake): add repo_registry field to IntakeLayerConfig

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Fix VoiceCommandSensor to use payload.repo

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py:96`
- Test: `tests/governance/intake/test_voice_command_sensor_repo.py` (new file)

### Step 1: Write the failing test

Create `tests/governance/intake/test_voice_command_sensor_repo.py`:

```python
"""VoiceCommandSensor must route envelope.repo from payload.repo, not self._repo."""
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandPayload,
    VoiceCommandSensor,
)


async def test_voice_sensor_uses_payload_repo_not_self_repo(tmp_path):
    """Envelope repo comes from payload.repo, ignoring the sensor's default repo."""
    captured = []

    mock_router = MagicMock()
    async def fake_ingest(envelope):
        captured.append(envelope)
        return "enqueued"
    mock_router.ingest = fake_ingest

    # Sensor constructed with repo="jarvis"
    sensor = VoiceCommandSensor(
        router=mock_router,
        repo="jarvis",
        stt_confidence_threshold=0.5,
    )

    # But payload says repo="prime"
    payload = VoiceCommandPayload(
        description="fix prime test failures",
        target_files=["tests/test_prime.py"],
        repo="prime",
        stt_confidence=0.95,
    )

    await sensor.handle_voice_command(payload)

    assert len(captured) == 1
    assert captured[0].repo == "prime", (
        f"Expected envelope.repo='prime', got '{captured[0].repo}'"
    )


async def test_voice_sensor_self_repo_is_fallback_when_payload_repo_empty(tmp_path):
    """When payload.repo is empty string, fall back to self._repo."""
    captured = []

    mock_router = MagicMock()
    async def fake_ingest(envelope):
        captured.append(envelope)
        return "enqueued"
    mock_router.ingest = fake_ingest

    sensor = VoiceCommandSensor(
        router=mock_router,
        repo="jarvis",
        stt_confidence_threshold=0.5,
    )

    payload = VoiceCommandPayload(
        description="fix something",
        target_files=["tests/test_x.py"],
        repo="",   # empty — should fall back
        stt_confidence=0.95,
    )

    await sensor.handle_voice_command(payload)

    assert len(captured) == 1
    assert captured[0].repo == "jarvis"
```

### Step 2: Run to verify failure

```bash
python3 -m pytest tests/governance/intake/test_voice_command_sensor_repo.py -v --tb=short
```
Expected: first test FAIL — `assert 'prime' == 'jarvis'` (or similar).

### Step 3: Implement

In `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py`, change line 96:

**Current:**
```python
            repo=self._repo,
```

**Replace with:**
```python
            repo=payload.repo or self._repo,
```

That's the entire change. `payload.repo or self._repo` uses the payload repo when non-empty, falls back to `self._repo` when empty string.

### Step 4: Run test

```bash
python3 -m pytest tests/governance/intake/test_voice_command_sensor_repo.py -v --tb=short
```
Expected: 2 PASSED.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py \
        tests/governance/intake/test_voice_command_sensor_repo.py
git commit -m "$(cat <<'EOF'
fix(intake): VoiceCommandSensor routes envelope.repo from payload.repo with self._repo fallback

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Fan out sensors per registered repo in `_build_components()`

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py:330-362` (`_build_components` sensor section)
- Test: `tests/governance/intake/test_intake_sensor_fanout.py` (new file)

### Step 1: Write the failing tests

Create `tests/governance/intake/test_intake_sensor_fanout.py`:

```python
"""IntakeLayerService creates one sensor per registered repo when registry is set."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
)
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig, RepoRegistry,
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


async def test_three_miner_sensors_created_for_three_repos(tmp_path):
    """With a 3-repo registry, _build_components creates 3 OpportunityMinerSensors."""
    from backend.core.ouroboros.governance.intake.sensors import OpportunityMinerSensor

    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)

    with patch(
        "backend.core.ouroboros.governance.intake.intake_layer_service.UnifiedIntakeRouter"
    ) as MockRouter:
        MockRouter.return_value.start = AsyncMock()
        with patch.object(
            svc, "_start_sensors", new=AsyncMock()
        ):
            await svc._build_components()

    miner_sensors = [s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)]
    assert len(miner_sensors) == 3, f"Expected 3 miners, got {len(miner_sensors)}"
    repos = {s._repo for s in miner_sensors}
    assert repos == {"jarvis", "prime", "reactor-core"}


async def test_three_test_failure_sensors_created_for_three_repos(tmp_path):
    """With a 3-repo registry, _build_components creates 3 TestFailureSensors."""
    from backend.core.ouroboros.governance.intake.sensors import TestFailureSensor

    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)

    with patch(
        "backend.core.ouroboros.governance.intake.intake_layer_service.UnifiedIntakeRouter"
    ) as MockRouter:
        MockRouter.return_value.start = AsyncMock()
        with patch.object(
            svc, "_start_sensors", new=AsyncMock()
        ):
            await svc._build_components()

    tf_sensors = [s for s in svc._sensors if isinstance(s, TestFailureSensor)]
    assert len(tf_sensors) == 3, f"Expected 3 TF sensors, got {len(tf_sensors)}"
    repos = {s._repo for s in tf_sensors}
    assert repos == {"jarvis", "prime", "reactor-core"}


async def test_single_sensor_fallback_when_no_registry(tmp_path):
    """Without a registry, exactly one miner and one TF sensor are created (backward compat)."""
    from backend.core.ouroboros.governance.intake.sensors import (
        OpportunityMinerSensor, TestFailureSensor,
    )

    config = IntakeLayerConfig(project_root=tmp_path)  # no repo_registry
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)

    with patch(
        "backend.core.ouroboros.governance.intake.intake_layer_service.UnifiedIntakeRouter"
    ) as MockRouter:
        MockRouter.return_value.start = AsyncMock()
        with patch.object(
            svc, "_start_sensors", new=AsyncMock()
        ):
            await svc._build_components()

    miners = [s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)]
    tf = [s for s in svc._sensors if isinstance(s, TestFailureSensor)]
    assert len(miners) == 1
    assert len(tf) == 1
    assert miners[0]._repo == "jarvis"
    assert tf[0]._repo == "jarvis"


async def test_miner_sensor_root_matches_registry_local_path(tmp_path):
    """Each OpportunityMinerSensor uses its repo's local_path, not project_root."""
    from backend.core.ouroboros.governance.intake.sensors import OpportunityMinerSensor

    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    svc = IntakeLayerService(gls=_mock_gls(), config=config, say_fn=None)

    with patch(
        "backend.core.ouroboros.governance.intake.intake_layer_service.UnifiedIntakeRouter"
    ) as MockRouter:
        MockRouter.return_value.start = AsyncMock()
        with patch.object(
            svc, "_start_sensors", new=AsyncMock()
        ):
            await svc._build_components()

    miners = {s._repo: s for s in svc._sensors if isinstance(s, OpportunityMinerSensor)}
    assert miners["prime"]._repo_root == tmp_path / "prime"
    assert miners["reactor-core"]._repo_root == tmp_path / "reactor-core"
```

**NOTE on `_start_sensors`:** The current `_build_components` does `await sensor.start()` inline. If there's no separate `_start_sensors` method, patch `sensor.start` on each sensor class instead, OR check the actual code and adjust the test patching accordingly. Read `_build_components` carefully before writing the test — adapt the patch target to match how sensor startup is called.

### Step 2: Run to verify failure

```bash
python3 -m pytest tests/governance/intake/test_intake_sensor_fanout.py -v --tb=short
```
Expected: FAIL — only 1 miner sensor created, not 3.

### Step 3: Implement

In `backend/core/ouroboros/governance/intake/intake_layer_service.py`, replace the sensor instantiation block (lines 330-362):

**Current block to replace:**
```python
        # Build sensors — using actual constructor parameter names.
        # BacklogSensor uses poll_interval_s (not scan_interval_s).
        # VoiceCommandSensor is event-driven (no start/stop); stored separately.
        backlog_path = self._config.project_root / ".jarvis" / "backlog.json"

        backlog_sensor = BacklogSensor(
            backlog_path=backlog_path,
            repo_root=self._config.project_root,
            router=self._router,
            poll_interval_s=self._config.backlog_scan_interval_s,
        )
        test_failure_sensor = TestFailureSensor(
            repo="jarvis",
            router=self._router,
        )
        opportunity_miner_sensor = OpportunityMinerSensor(
            repo_root=self._config.project_root,
            router=self._router,
            scan_paths=self._config.miner_scan_paths,
            complexity_threshold=self._config.miner_complexity_threshold,
            poll_interval_s=self._config.miner_scan_interval_s,
            auto_submit_threshold=self._config.miner_auto_submit_threshold,
        )

        # VoiceCommandSensor has no start/stop lifecycle; store as attribute only.
        self._voice_sensor = VoiceCommandSensor(
            router=self._router,
            repo="jarvis",
            stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
        )

        # Sensors with start/stop lifecycle
        self._sensors = [backlog_sensor, test_failure_sensor, opportunity_miner_sensor]
```

**Replace with:**
```python
        # Build sensors — using actual constructor parameter names.
        # BacklogSensor uses poll_interval_s (not scan_interval_s).
        # VoiceCommandSensor is event-driven (no start/stop); stored separately.
        backlog_path = self._config.project_root / ".jarvis" / "backlog.json"

        backlog_sensor = BacklogSensor(
            backlog_path=backlog_path,
            repo_root=self._config.project_root,
            router=self._router,
            poll_interval_s=self._config.backlog_scan_interval_s,
        )

        # Fan out per-repo sensors when a registry is available; fall back to
        # single "jarvis" sensor for backward compatibility.
        registry = self._config.repo_registry
        if registry is not None:
            enabled_repos = list(registry.list_enabled())
        else:
            enabled_repos = []

        if enabled_repos:
            test_failure_sensors = [
                TestFailureSensor(repo=rc.name, router=self._router)
                for rc in enabled_repos
            ]
            miner_sensors = [
                OpportunityMinerSensor(
                    repo_root=rc.local_path,
                    router=self._router,
                    scan_paths=self._config.miner_scan_paths,
                    complexity_threshold=self._config.miner_complexity_threshold,
                    poll_interval_s=self._config.miner_scan_interval_s,
                    auto_submit_threshold=self._config.miner_auto_submit_threshold,
                    repo=rc.name,
                )
                for rc in enabled_repos
            ]
        else:
            test_failure_sensors = [TestFailureSensor(repo="jarvis", router=self._router)]
            miner_sensors = [
                OpportunityMinerSensor(
                    repo_root=self._config.project_root,
                    router=self._router,
                    scan_paths=self._config.miner_scan_paths,
                    complexity_threshold=self._config.miner_complexity_threshold,
                    poll_interval_s=self._config.miner_scan_interval_s,
                    auto_submit_threshold=self._config.miner_auto_submit_threshold,
                )
            ]

        # VoiceCommandSensor has no start/stop lifecycle; store as attribute only.
        # Primary repo is jarvis; payload.repo overrides per-command (fixed in Task 2).
        self._voice_sensor = VoiceCommandSensor(
            router=self._router,
            repo="jarvis",
            stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
        )

        # Sensors with start/stop lifecycle
        self._sensors = [backlog_sensor] + test_failure_sensors + miner_sensors
```

### Step 4: Adjust tests if needed

After implementing, re-read the actual `_build_components` to see if sensor startup is inline (`await sensor.start()`) or via a separate helper. If startup is inline:
- The test's `patch.object(svc, "_start_sensors", ...)` won't work
- Instead patch `sensor.start` on the sensor class: `patch("backend.core.ouroboros.governance.intake.sensors.BacklogSensor.start", new=AsyncMock())`
- Or simpler: patch `sensor.start` as a method on each mock

The goal of the test is to assert how many sensors are in `svc._sensors` and what their `_repo` attributes are — adjust the patching strategy to ensure `_build_components` runs fully without hitting real filesystems.

### Step 5: Run tests

```bash
python3 -m pytest tests/governance/intake/test_intake_sensor_fanout.py -v --tb=short
```
Expected: 4 PASSED.

### Step 6: Run full intake + governance suite to check regressions

```bash
python3 -m pytest tests/governance/intake/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -15
```
Expected: 0 new failures.

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py \
        tests/governance/intake/test_intake_sensor_fanout.py
git commit -m "$(cat <<'EOF'
feat(intake): fan out OpportunityMinerSensor and TestFailureSensor per registered repo

When IntakeLayerConfig.repo_registry is set, _build_components() creates one
miner and one test-failure sensor per enabled repo instead of hardcoding "jarvis".
Falls back to single-repo mode when registry is None (backward compat).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire registry from GovernedLoopService into IntakeLayerConfig

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` — find where `IntakeLayerConfig` is constructed and pass `repo_registry`
- Test: add to `tests/test_ouroboros_governance/test_governed_loop_service.py`

### Step 1: Find the IntakeLayerConfig construction site

Read `governed_loop_service.py` and grep for `IntakeLayerConfig`. It is constructed somewhere during service startup. Find the exact line and confirm the current kwargs.

```bash
grep -n "IntakeLayerConfig" backend/core/ouroboros/governance/governed_loop_service.py
```

### Step 2: Write the failing test

Append to `tests/test_ouroboros_governance/test_governed_loop_service.py`:

```python
class TestGovernedLoopIntakeRegistryWiring:
    async def test_intake_layer_config_receives_registry(self, tmp_path, monkeypatch):
        """GovernedLoopService passes RepoRegistry to IntakeLayerConfig at startup."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        from backend.core.ouroboros.governance.intake.intake_layer_service import (
            IntakeLayerConfig,
        )

        prime_path = tmp_path / "prime"
        prime_path.mkdir()
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("JARVIS_PRIME_REPO_PATH", str(prime_path))

        captured_intake_config: list = []

        original_init = IntakeLayerService.__init__

        def capturing_init(self_inner, gls, config, say_fn):
            captured_intake_config.append(config)
            original_init(self_inner, gls=gls, config=config, say_fn=say_fn)

        config = GovernedLoopConfig(project_root=tmp_path)
        stack = _mock_stack()
        svc = GovernedLoopService(stack=stack, prime_client=None, config=config)

        with patch(
            "backend.core.ouroboros.governance.governed_loop_service.IntakeLayerService.__init__",
            side_effect=capturing_init,
            autospec=True,
        ):
            try:
                await svc.start()
            except Exception:
                pass  # full start may fail without real infra; we only need the config captured

        assert len(captured_intake_config) > 0, "IntakeLayerService was never constructed"
        intake_cfg = captured_intake_config[0]
        assert intake_cfg.repo_registry is not None
        names = {r.name for r in intake_cfg.repo_registry.list_enabled()}
        assert "jarvis" in names
        assert "prime" in names
```

NOTE: This test is tricky because `GovernedLoopService.start()` may fail before reaching `IntakeLayerService` construction in a test env. Adjust the approach: if `IntakeLayerService` is built inside `_build_components()`, you may need to call `svc._build_components()` directly rather than `svc.start()`. Read the code first to understand the construction flow.

### Step 3: Run to verify failure

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestGovernedLoopIntakeRegistryWiring -v --tb=short
```
Expected: FAIL — `intake_cfg.repo_registry is None`

### Step 4: Implement

Find the `IntakeLayerConfig(...)` instantiation in `governed_loop_service.py`. Add `repo_registry=self._repo_registry` (or however the registry is stored on the service). The `GovernedLoopService` already builds `RepoRegistry.from_env()` in `_build_components()` and stores it on `OrchestratorConfig` — reuse the same registry object.

The pattern will look something like:

```python
# Existing registry build (already there from Task 2 of previous plan):
repo_registry = RepoRegistry.from_env()
logger.info("[GovernedLoop] RepoRegistry enabled repos: %s", ...)

# ... further down, where IntakeLayerConfig is built ...
intake_config = IntakeLayerConfig(
    project_root=self._config.project_root,
    repo_registry=repo_registry,   # <-- ADD THIS
    # ... other existing kwargs ...
)
```

Read the actual code before editing — exact variable names and order may differ.

### Step 5: Run test

```bash
python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py::TestGovernedLoopIntakeRegistryWiring -v --tb=short
```
Expected: PASS.

### Step 6: Run full suite

```bash
python3 -m pytest tests/governance/ tests/test_ouroboros_governance/ -q --tb=short 2>&1 | tail -10
```
Expected: 0 new failures (30 pre-existing failures are acceptable).

### Step 7: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py \
        tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "$(cat <<'EOF'
feat(governance): pass RepoRegistry to IntakeLayerConfig so sensors fan out per repo

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Summary

| Task | What changes | Effect |
|------|-------------|--------|
| 1 | `IntakeLayerConfig.repo_registry` field | Config can carry registry |
| 2 | `VoiceCommandSensor` uses `payload.repo` | Voice ops target correct repo |
| 3 | `_build_components()` fans out sensors | 3 miners + 3 TF sensors for 3 repos |
| 4 | `GovernedLoopService` passes registry to intake | Full end-to-end wiring |

After all 4 tasks:
- BacklogSensor: multi-repo (already was)
- VoiceCommandSensor: multi-repo ✅
- TestFailureSensor: 3 instances, one per repo ✅
- OpportunityMinerSensor: 3 instances, one per repo, each scanning its own filesystem ✅
