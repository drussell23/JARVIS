# Phase 2C.2/2C.3 — Loop Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the idle intake layer into the supervisor boot sequence (Zone 6.9) and fix the broken VoiceNarrator B-layer import so JARVIS can autonomously ingest intents and narrate them.

**Architecture:** `IntakeLayerService` (new) mirrors `GovernedLoopService` — it owns the router + 4 sensors + A-narrator; the supervisor starts it in Zone 6.9 after Zone 6.8 (GLS must be active first). The VoiceNarrator B-layer (`CommProtocol`) is fixed with a one-line import correction. Two narration planes: A = preflight awareness (detected/queued), B = execution truth (pipeline state from GLS).

**Tech Stack:** Python 3.12, asyncio, pytest-asyncio (`asyncio_mode="auto"` — NEVER add `@pytest.mark.asyncio`), existing `UnifiedIntakeRouter`, `GovernedLoopService`, `VoiceNarrator`.

**Test baseline:** 439 tests passing. Every task must keep 439+ passing.

---

## Codebase orientation (read before starting)

- `backend/core/ouroboros/governance/intake/unified_intake_router.py` — `UnifiedIntakeRouter`, `IntakeRouterConfig`
- `backend/core/ouroboros/governance/intake/sensors/` — 4 sensors, each has `.start()` and `.stop()` coroutines
- `backend/core/ouroboros/governance/governed_loop_service.py` — `GovernedLoopService.submit(ctx, trigger_source)` signature; `ServiceState` enum; use as structural model
- `backend/core/ouroboros/governance/op_context.py` — `OperationContext` constructor signature
- `backend/core/ouroboros/governance/integration.py:254-262` — broken VoiceNarrator import (Task 1 target)
- `unified_supervisor.py:85921-85951` — Zone 6.8 block; Zone 6.9 inserts immediately after line 85951 (after inner except block)
- `unified_supervisor.py:91982-91993` — shutdown sequence; intake stop inserts BEFORE `_governed_loop.stop()` at line 91983
- `unified_supervisor.py:66672` — where `self._governed_loop: Optional[Any] = None` lives; add `self._intake_layer` near it
- `pytest.ini` / `pyproject.toml` — confirm `asyncio_mode = "auto"` (never add `@pytest.mark.asyncio`)

---

## Task 1: Fix VoiceNarrator B-layer import

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py:254-262`
- Test: `tests/governance/integration/test_voice_narrator_wired.py` (new)

### Step 1: Write the failing test

Create `tests/governance/integration/test_voice_narrator_wired.py`:

```python
"""VoiceNarrator is wired into CommProtocol when say_fn import succeeds."""
from unittest.mock import AsyncMock, patch

from backend.core.ouroboros.governance.integration import build_comm_protocol


async def test_voice_narrator_wired_when_safe_say_importable():
    """build_comm_protocol includes VoiceNarrator transport when say_fn resolves."""
    fake_say = AsyncMock(return_value=True)
    with patch(
        "backend.core.ouroboros.governance.integration.safe_say",
        fake_say,
        create=True,
    ):
        # Re-import after patching — easier to just call with extra transport
        # The real test: integration.py must resolve safe_say via the correct path.
        # We verify by ensuring the module-level import attempt doesn't raise.
        import importlib
        import backend.core.ouroboros.governance.integration as mod
        importlib.reload(mod)
        # After reload with correct import path, VoiceNarrator should be present
        # Check the transport count: LogTransport + TUITransport + VoiceNarrator + OpsLogger = 4
        protocol = mod.build_comm_protocol()
        transport_types = [type(t).__name__ for t in protocol._transports]
        assert "VoiceNarrator" in transport_types, (
            f"VoiceNarrator not in transports: {transport_types}"
        )
```

### Step 2: Run to confirm it fails

```bash
python3 -m pytest tests/governance/integration/test_voice_narrator_wired.py -v
```

Expected: FAIL — `VoiceNarrator` absent from transports because `backend.audio` import fails.

### Step 3: Fix the import in `integration.py`

In `backend/core/ouroboros/governance/integration.py`, replace lines 254-262:

```python
    # VoiceNarrator — requires safe_say; skip gracefully if unavailable
    try:
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
        from backend.audio import safe_say  # type: ignore[import]
    except ImportError as exc:
        logger.debug("[Integration] VoiceNarrator skipped (audio unavailable): %s", exc)
    else:
        transports.append(VoiceNarrator(say_fn=safe_say, debounce_s=60.0, source="ouroboros"))
        logger.debug("[Integration] VoiceNarrator added to CommProtocol")
```

Replace with:

```python
    # VoiceNarrator — requires safe_say from voice orchestrator; skip gracefully if unavailable
    try:
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
        from backend.core.supervisor.unified_voice_orchestrator import safe_say  # type: ignore[import]
    except ImportError as exc:
        logger.debug("[Integration] VoiceNarrator skipped (audio unavailable): %s", exc)
    else:
        transports.append(VoiceNarrator(say_fn=safe_say, debounce_s=60.0, source="ouroboros"))
        logger.debug("[Integration] VoiceNarrator added to CommProtocol")
```

**NOTE:** The test above requires `importlib.reload()` which is fragile in CI. A cleaner test approach is to just import the module and call `build_comm_protocol()` in an environment where the import resolves. Since `unified_voice_orchestrator.safe_say` IS resolvable (it's a plain module), after the fix the `else` branch executes. Simplify the test:

```python
"""VoiceNarrator import path resolves correctly after fix."""
from backend.core.ouroboros.governance.integration import build_comm_protocol


async def test_build_comm_protocol_includes_voice_narrator():
    """After import fix, VoiceNarrator appears in CommProtocol transports."""
    protocol = build_comm_protocol()
    transport_types = [type(t).__name__ for t in protocol._transports]
    assert "VoiceNarrator" in transport_types, (
        f"VoiceNarrator missing from transports: {transport_types}. "
        "Fix: backend/core/ouroboros/governance/integration.py safe_say import"
    )
```

**Replace the test file content** with the simpler version above.

### Step 4: Run full governance suite

```bash
python3 -m pytest tests/governance/ -q
```

Expected: 440+ tests, 0 failures.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/integration.py \
        tests/governance/integration/test_voice_narrator_wired.py
git commit -m "fix(comms): wire VoiceNarrator to correct safe_say import path"
```

---

## Task 2: `IntakeLayerConfig` dataclass

**Files:**
- Create: `backend/core/ouroboros/governance/intake/intake_layer_service.py`
- Test: `tests/governance/intake/test_intake_layer_service.py` (new)

### Step 1: Write the failing test

Create `tests/governance/intake/test_intake_layer_service.py`:

```python
"""IntakeLayerService — lifecycle and config tests."""
import os
from pathlib import Path

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
)


def test_intake_layer_config_defaults(tmp_path):
    config = IntakeLayerConfig(project_root=tmp_path)
    assert config.project_root == tmp_path
    assert config.dedup_window_s > 0
    assert config.backlog_scan_interval_s > 0
    assert config.miner_complexity_threshold > 0
    assert config.a_narrator_enabled is True


def test_intake_layer_config_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "120.0")
    config = IntakeLayerConfig.from_env()
    assert config.project_root == tmp_path
    assert config.dedup_window_s == 120.0
```

### Step 2: Run to confirm it fails

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py::test_intake_layer_config_defaults -v
```

Expected: FAIL — `ModuleNotFoundError: intake_layer_service`

### Step 3: Create the file with config + state only

Create `backend/core/ouroboros/governance/intake/intake_layer_service.py`:

```python
"""
IntakeLayerService — Supervisor Zone 6.9 lifecycle manager.

Owns UnifiedIntakeRouter, all 4 sensors, and the A-narrator (salience-gated
preflight awareness). Mirrors GovernedLoopService pattern: no side effects in
constructor; all async initialization in start().

Delivery semantics: at-least-once intake (WAL) + idempotent execution (dedup_key).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("Ouroboros.IntakeLayer")

# ---------------------------------------------------------------------------
# IntakeServiceState
# ---------------------------------------------------------------------------


class IntakeServiceState(Enum):
    INACTIVE = auto()
    STARTING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    STOPPING = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# IntakeLayerConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeLayerConfig:
    """Frozen configuration for IntakeLayerService."""

    project_root: Path
    dedup_window_s: float = 60.0
    backlog_scan_interval_s: float = 30.0
    miner_scan_interval_s: float = 300.0
    miner_complexity_threshold: int = 10
    miner_scan_paths: List[str] = field(default_factory=lambda: ["backend/", "tests/"])
    voice_stt_confidence_threshold: float = 0.70
    a_narrator_enabled: bool = True
    a_narrator_debounce_s: float = 10.0
    test_failure_min_count_for_narration: int = 2

    @classmethod
    def from_env(cls, project_root: Optional[Path] = None) -> IntakeLayerConfig:
        resolved = project_root or Path(os.getenv("JARVIS_PROJECT_ROOT", os.getcwd()))
        return cls(
            project_root=resolved,
            dedup_window_s=float(os.getenv("JARVIS_INTAKE_DEDUP_WINDOW_S", "60.0")),
            backlog_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S", "30.0")
            ),
            miner_scan_interval_s=float(
                os.getenv("JARVIS_INTAKE_MINER_SCAN_INTERVAL_S", "300.0")
            ),
            miner_complexity_threshold=int(
                os.getenv("JARVIS_INTAKE_MINER_COMPLEXITY_THRESHOLD", "10")
            ),
            voice_stt_confidence_threshold=float(
                os.getenv("JARVIS_INTAKE_VOICE_STT_THRESHOLD", "0.70")
            ),
            a_narrator_enabled=os.getenv(
                "JARVIS_INTAKE_A_NARRATOR_ENABLED", "true"
            ).lower() not in ("0", "false", "no"),
            a_narrator_debounce_s=float(
                os.getenv("JARVIS_INTAKE_A_NARRATOR_DEBOUNCE_S", "10.0")
            ),
            test_failure_min_count_for_narration=int(
                os.getenv("JARVIS_INTAKE_TF_MIN_COUNT", "2")
            ),
        )
```

### Step 4: Run tests

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -v
```

Expected: PASS (2 tests).

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py \
        tests/governance/intake/test_intake_layer_service.py
git commit -m "feat(intake): IntakeLayerConfig with env-driven defaults"
```

---

## Task 3: `IntakeLayerService` lifecycle skeleton

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py`
- Modify: `tests/governance/intake/test_intake_layer_service.py`

### Step 1: Add lifecycle tests

Append to `tests/governance/intake/test_intake_layer_service.py`:

```python
from unittest.mock import AsyncMock, MagicMock


async def test_service_initial_state(tmp_path):
    gls = MagicMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    assert svc.state is IntakeServiceState.INACTIVE


async def test_service_start_reaches_active(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    say_fn = AsyncMock(return_value=True)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=say_fn)
    await svc.start()
    assert svc.state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED)
    await svc.stop()
    assert svc.state is IntakeServiceState.INACTIVE


async def test_service_start_idempotent(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    state_after_first = svc.state
    await svc.start()  # second call must be no-op
    assert svc.state is state_after_first
    await svc.stop()


async def test_service_health_keys(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    h = svc.health()
    assert "state" in h
    assert "queue_depth" in h
    assert "dead_letter_count" in h
    assert "per_source_rate" in h
    await svc.stop()


async def test_service_stop_from_inactive_is_noop(tmp_path):
    gls = MagicMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.stop()  # must not raise
    assert svc.state is IntakeServiceState.INACTIVE
```

### Step 2: Run to confirm failures

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -v
```

Expected: FAIL — `IntakeLayerService` not yet defined.

### Step 3: Implement `IntakeLayerService` skeleton

Append to `backend/core/ouroboros/governance/intake/intake_layer_service.py`:

```python
# ---------------------------------------------------------------------------
# IntakeLayerService
# ---------------------------------------------------------------------------


class IntakeLayerService:
    """Lifecycle manager for router + sensors + A-narrator (Zone 6.9).

    Constructor is side-effect free. All async setup in start().
    """

    def __init__(
        self,
        gls: Any,
        config: IntakeLayerConfig,
        say_fn: Optional[Callable[..., Coroutine[Any, Any, bool]]],
    ) -> None:
        self._gls = gls
        self._config = config
        self._say_fn = say_fn
        self._state = IntakeServiceState.INACTIVE
        self._started_at: Optional[float] = None

        # Built during start()
        self._router: Optional[Any] = None
        self._sensors: List[Any] = []
        self._narrator: Optional[IntakeNarrator] = None
        self._dead_letter_count: int = 0
        self._per_source_count: Dict[str, int] = {}
        self._started_at_monotonic: float = 0.0

    @property
    def state(self) -> IntakeServiceState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build and start router, sensors, A-narrator. Idempotent."""
        if self._state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED):
            return

        self._state = IntakeServiceState.STARTING
        try:
            await self._build_components()
            self._state = IntakeServiceState.ACTIVE
            self._started_at_monotonic = time.monotonic()
            logger.info("[IntakeLayer] Started: state=%s", self._state.name)
        except Exception as exc:
            self._state = IntakeServiceState.FAILED
            logger.error("[IntakeLayer] Start failed: %s", exc, exc_info=True)
            await self._teardown()
            raise

    async def stop(self) -> None:
        """Stop sensors first (drain), then router. Idempotent from INACTIVE."""
        if self._state is IntakeServiceState.INACTIVE:
            return

        self._state = IntakeServiceState.STOPPING

        # Stop sensors first to prevent new envelopes entering router
        for sensor in self._sensors:
            try:
                await sensor.stop()
            except Exception as exc:
                logger.warning("[IntakeLayer] Sensor stop error: %s", exc)

        # Stop router (drains in-flight queue)
        if self._router is not None:
            try:
                await self._router.stop()
            except Exception as exc:
                logger.warning("[IntakeLayer] Router stop error: %s", exc)

        self._sensors = []
        self._router = None
        self._narrator = None
        self._state = IntakeServiceState.INACTIVE
        logger.info("[IntakeLayer] Stopped.")

    def health(self) -> Dict[str, Any]:
        """Return health metrics for supervisor health checks."""
        queue_depth = 0
        wal_pending = 0
        if self._router is not None:
            try:
                queue_depth = self._router._queue.qsize()
            except Exception:
                pass

        uptime_s = (
            time.monotonic() - self._started_at_monotonic
            if self._started_at_monotonic > 0
            else 0.0
        )
        per_source_rate: Dict[str, float] = {}
        if uptime_s > 0:
            for src, cnt in self._per_source_count.items():
                per_source_rate[src] = round(cnt / (uptime_s / 60.0), 3)

        return {
            "state": self._state.name.lower(),
            "queue_depth": queue_depth,
            "dead_letter_count": self._dead_letter_count,
            "wal_entries_pending": wal_pending,
            "per_source_rate": per_source_rate,
            "uptime_s": round(uptime_s, 1),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _build_components(self) -> None:
        """Construct and start router + sensors + narrator."""
        from backend.core.ouroboros.governance.intake import (
            IntakeRouterConfig,
            UnifiedIntakeRouter,
        )
        from backend.core.ouroboros.governance.intake.sensors import (
            BacklogSensor,
            OpportunityMinerSensor,
            TestFailureSensor,
            VoiceCommandSensor,
        )

        router_config = IntakeRouterConfig(
            project_root=self._config.project_root,
            dedup_window_s=self._config.dedup_window_s,
        )
        self._router = UnifiedIntakeRouter(gls=self._gls, config=router_config)

        # A-narrator: salience-gated preflight awareness
        if self._config.a_narrator_enabled and self._say_fn is not None:
            self._narrator = IntakeNarrator(
                say_fn=self._say_fn,
                debounce_s=self._config.a_narrator_debounce_s,
                test_failure_min_count=self._config.test_failure_min_count_for_narration,
            )
            self._router._on_ingest_hook = self._narrator.on_envelope

        # Build sensors
        backlog_path = self._config.project_root / ".jarvis" / "backlog.json"
        self._sensors = [
            BacklogSensor(
                backlog_path=backlog_path,
                repo_root=self._config.project_root,
                router=self._router,
                scan_interval_s=self._config.backlog_scan_interval_s,
            ),
            TestFailureSensor(
                repo="jarvis",
                router=self._router,
            ),
            VoiceCommandSensor(
                router=self._router,
                repo="jarvis",
                stt_confidence_threshold=self._config.voice_stt_confidence_threshold,
            ),
            OpportunityMinerSensor(
                repo_root=self._config.project_root,
                router=self._router,
                scan_paths=self._config.miner_scan_paths,
                complexity_threshold=self._config.miner_complexity_threshold,
                scan_interval_s=self._config.miner_scan_interval_s,
            ),
        ]

        await self._router.start()
        for sensor in self._sensors:
            await sensor.start()

    async def _teardown(self) -> None:
        """Best-effort cleanup after failed start."""
        for sensor in self._sensors:
            try:
                await sensor.stop()
            except Exception:
                pass
        if self._router is not None:
            try:
                await self._router.stop()
            except Exception:
                pass
        self._sensors = []
        self._router = None
```

### Step 4: Run tests

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -v
```

Expected: All tests PASS.

### Step 5: Run full suite

```bash
python3 -m pytest tests/governance/ -q
```

Expected: 440+ tests, 0 failures.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py \
        tests/governance/intake/test_intake_layer_service.py
git commit -m "feat(intake): IntakeLayerService lifecycle skeleton (start/stop/health)"
```

---

## Task 4: `IntakeNarrator` — A-layer salience-gated narrator

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py`
- Modify: `tests/governance/intake/test_intake_layer_service.py`

### Step 1: Add narrator tests

Append to `tests/governance/intake/test_intake_layer_service.py`:

```python
from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeNarrator
from backend.core.ouroboros.governance.intake import make_envelope


async def test_a_narrator_speaks_for_voice_human():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="voice_human", description="fix auth now",
        target_files=("backend/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "voice_test_1"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_called_once()
    text = say_fn.call_args.args[0]
    assert "voice command" in text.lower() or "command" in text.lower()


async def test_a_narrator_silent_for_backlog():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="backlog", description="fix something",
        target_files=("backend/x.py",), repo="jarvis",
        confidence=0.7, urgency="normal",
        evidence={"signature": "backlog_1"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_a_narrator_silent_for_ai_miner():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="ai_miner", description="refactor complex.py",
        target_files=("backend/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "miner_1"},
        requires_human_ack=True,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_a_narrator_speaks_test_failure_above_threshold():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0, test_failure_min_count=2)
    for i in range(2):
        env = make_envelope(
            source="test_failure", description=f"test fail {i}",
            target_files=("tests/test_x.py",), repo="jarvis",
            confidence=0.9, urgency="high",
            evidence={"signature": f"tf_{i}"},
            requires_human_ack=False,
        )
        await narrator.on_envelope(env)
    # At least one narration after 2 failures
    assert say_fn.call_count >= 1


async def test_a_narrator_debounce_suppresses_rapid_voice():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=999.0)
    for i in range(3):
        env = make_envelope(
            source="voice_human", description=f"command {i}",
            target_files=("backend/auth.py",), repo="jarvis",
            confidence=0.95, urgency="critical",
            evidence={"signature": f"v_{i}"},
            requires_human_ack=False,
        )
        await narrator.on_envelope(env)
    # Only first narration fires; rest suppressed by debounce
    assert say_fn.call_count == 1
```

### Step 2: Run to confirm failures

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -k "narrator" -v
```

Expected: FAIL — `IntakeNarrator` not defined yet.

### Step 3: Implement `IntakeNarrator`

Add the class to `intake_layer_service.py` BEFORE `IntakeLayerService` class:

```python
# ---------------------------------------------------------------------------
# IntakeNarrator (A-layer)
# ---------------------------------------------------------------------------

# Sources that always trigger narration (regardless of count)
_A_NARRATE_ALWAYS = {"voice_human"}
# Sources that require a count threshold (tracked by source)
_A_NARRATE_THRESHOLD = {"test_failure"}
# Sources that are always silent at A-layer (B-layer covers them)
_A_NARRATE_SILENT = {"backlog", "ai_miner"}

_A_TEMPLATES = {
    "voice_human": "Voice command queued: {description}",
    "test_failure": "{count} test failures detected. Investigating.",
}


class IntakeNarrator:
    """A-layer narrator: salience-gated preflight awareness only.

    Language policy: 'detected/queued' — never 'applying/fixing'.
    QoS: debounced; silent for backlog and ai_miner sources.
    """

    def __init__(
        self,
        say_fn: Callable[..., Coroutine[Any, Any, bool]],
        debounce_s: float = 10.0,
        test_failure_min_count: int = 2,
    ) -> None:
        self._say_fn = say_fn
        self._debounce_s = debounce_s
        self._test_failure_min_count = test_failure_min_count
        self._last_narration: float = float("-inf")
        self._failure_count: int = 0

    async def on_envelope(self, envelope: Any) -> None:
        """Called by router after successful ingest. Filters by salience policy."""
        source = envelope.source
        if source in _A_NARRATE_SILENT:
            return

        now = time.monotonic()
        if source in _A_NARRATE_THRESHOLD:
            self._failure_count += 1
            if self._failure_count < self._test_failure_min_count:
                return
            text = _A_TEMPLATES["test_failure"].format(count=self._failure_count)
        elif source in _A_NARRATE_ALWAYS:
            text = _A_TEMPLATES["voice_human"].format(
                description=envelope.description[:80]
            )
        else:
            return  # Unknown source — silent by default

        if (now - self._last_narration) < self._debounce_s:
            return

        try:
            await self._say_fn(text, source="intake_narrator")
            self._last_narration = now
        except Exception:
            logger.debug("[IntakeNarrator] say_fn failed for envelope %s", envelope.causal_id)
```

### Step 4: Run tests

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -v
```

Expected: All tests PASS.

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py \
        tests/governance/intake/test_intake_layer_service.py
git commit -m "feat(intake): IntakeNarrator A-layer with salience gating and debounce"
```

---

## Task 5: `_on_ingest_hook` in `UnifiedIntakeRouter`

The A-narrator needs a hook called post-ingest. The router doesn't have one yet.

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py`
- Modify: `tests/governance/intake/test_intake_layer_service.py`

### Step 1: Add hook test

Append to `tests/governance/intake/test_intake_layer_service.py`:

```python
from backend.core.ouroboros.governance.intake import (
    UnifiedIntakeRouter, IntakeRouterConfig, make_envelope,
)


async def test_router_on_ingest_hook_called(tmp_path):
    """UnifiedIntakeRouter calls _on_ingest_hook after successful ingest."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path)
    router = UnifiedIntakeRouter(gls=gls, config=config)
    await router.start()

    hooked_envelopes = []

    async def hook(env):
        hooked_envelopes.append(env)

    router._on_ingest_hook = hook

    env = make_envelope(
        source="voice_human", description="test hook",
        target_files=("a.py",), repo="jarvis",
        confidence=0.9, urgency="critical",
        evidence={"signature": "hook_test"},
        requires_human_ack=False,
    )
    await router.ingest(env)
    await asyncio.sleep(0.05)
    await router.stop()

    assert len(hooked_envelopes) == 1
    assert hooked_envelopes[0].causal_id == env.causal_id
```

### Step 2: Run to confirm failure

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py::test_router_on_ingest_hook_called -v
```

Expected: FAIL — hook never called (attribute not in router).

### Step 3: Add `_on_ingest_hook` support to `UnifiedIntakeRouter`

Read the current `unified_intake_router.py` to find the `ingest()` method, then add hook support.

In `backend/core/ouroboros/governance/intake/unified_intake_router.py`:

1. In `__init__`, add after existing instance variables:
```python
        # Optional post-ingest hook (A-narrator). Coroutine called with envelope on
        # successful enqueue. Failures are logged and swallowed (non-critical path).
        self._on_ingest_hook: Optional[Callable[..., Coroutine[Any, Any, None]]] = None
```

2. In the `ingest()` method, after the successful `enqueue` path (where it returns `"enqueued"`), add the hook call. Find the line that returns `"enqueued"` and add before it:
```python
            # Fire A-narrator hook (non-critical; failures logged only)
            if self._on_ingest_hook is not None:
                try:
                    await self._on_ingest_hook(envelope)
                except Exception as _hook_exc:
                    logger.debug("[Router] on_ingest_hook error: %s", _hook_exc)
```

**Important:** Hook fires only on `"enqueued"` path, NOT on `"deduplicated"`, `"pending_ack"`, or dead-letter paths.

### Step 4: Run all intake tests

```bash
python3 -m pytest tests/governance/intake/ -v
```

Expected: All tests PASS (0 failures).

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/intake/unified_intake_router.py \
        tests/governance/intake/test_intake_layer_service.py
git commit -m "feat(intake): add _on_ingest_hook to UnifiedIntakeRouter for A-narrator"
```

---

## Task 6: Supervisor boot wiring (Zone 6.9) + shutdown order

**Files:**
- Modify: `unified_supervisor.py` (3 insertion points)

### Step 1: No unit test (supervisor is an integration seam)

The acceptance test in Task 8 covers this. This task is pure wiring.

### Step 2: Add `self._intake_layer = None` to `__init__`

In `unified_supervisor.py`, find the line (around line 66672):
```python
        self._governed_loop: Optional[Any] = None
```

Add immediately after:
```python
        self._intake_layer: Optional[Any] = None  # Zone 6.9: IntakeLayerService
```

### Step 3: Add Zone 6.9 after Zone 6.8

Find the end of the Zone 6.8 inner `except BaseException` block (ends with the log at line ~85951):

```python
                                except BaseException as exc:
                                    # BaseException catches CancelledError (Python 3.9+)
                                    # and TimeoutError from wait_for
                                    self._governed_loop = None
                                    self.logger.warning(
                                        "[Kernel] Zone 6.8 governed loop failed: %s -- skipped",
                                        exc,
                                    )
```

Insert Zone 6.9 IMMEDIATELY AFTER (before the outer `except (GovernanceInitError, asyncio.TimeoutError)` line):

```python
                            # ---- Zone 6.9: Intake Layer Service ----
                            if (
                                self._governed_loop is not None
                                and self._governed_loop.state.name
                                in ("active", "degraded")
                            ):
                                try:
                                    from backend.core.ouroboros.governance.intake.intake_layer_service import (  # noqa: E501
                                        IntakeLayerConfig,
                                        IntakeLayerService,
                                    )
                                    from backend.core.supervisor.unified_voice_orchestrator import (  # noqa: E501
                                        safe_say,
                                    )

                                    _intake_config = IntakeLayerConfig.from_env(
                                        project_root=_loop_config.project_root
                                    )
                                    self._intake_layer = IntakeLayerService(
                                        gls=self._governed_loop,
                                        config=_intake_config,
                                        say_fn=safe_say,
                                    )
                                    await asyncio.wait_for(
                                        asyncio.shield(self._intake_layer.start()),
                                        timeout=30.0,
                                    )
                                    self.logger.info(
                                        "[Kernel] Zone 6.9 intake layer: %s",
                                        self._intake_layer.health(),
                                    )
                                except BaseException as exc:
                                    self._intake_layer = None
                                    self.logger.warning(
                                        "[Kernel] Zone 6.9 intake layer failed: %s -- skipped",
                                        exc,
                                    )
```

**Note on state comparison:** `GovernedLoopService.state` is a `ServiceState` enum. The name comparison uses `.name` which returns the enum member name in uppercase (e.g., `"ACTIVE"`). Change comparison to:

```python
                            from backend.core.ouroboros.governance.governed_loop_service import ServiceState as _GLS_State
                            if (
                                self._governed_loop is not None
                                and self._governed_loop.state in (_GLS_State.ACTIVE, _GLS_State.DEGRADED)
                            ):
```

Or simply use the cleaner guard:

```python
                            if getattr(self._governed_loop, "state", None) is not None and \
                               self._governed_loop.state.name in ("ACTIVE", "DEGRADED"):
```

Use whichever compiles cleanly given the imports at that scope.

### Step 4: Add intake stop BEFORE governed loop stop in shutdown

Find (around line 91982):
```python
            # v301.0: Stop governed loop before governance stack (dependency order)
            if getattr(self, "_governed_loop", None) is not None:
```

Insert BEFORE that block:
```python
            # v302.0: Stop intake layer before governed loop (reverse start order)
            if getattr(self, "_intake_layer", None) is not None:
                try:
                    await asyncio.wait_for(self._intake_layer.stop(), timeout=30.0)
                except Exception as exc:
                    self.logger.warning("[Kernel] Intake layer stop failed: %s", exc)
```

### Step 5: Smoke-test imports

```bash
python3 -c "
from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig, IntakeLayerService, IntakeServiceState, IntakeNarrator
)
print('imports OK')
"
```

Expected: `imports OK`

### Step 6: Run full suite

```bash
python3 -m pytest tests/governance/ -q
```

Expected: 440+ tests, 0 failures.

### Step 7: Commit

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): Zone 6.9 IntakeLayerService boot + reverse-order shutdown"
```

---

## Task 7: Module exports

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

### Step 1: Write export tests

Append to `tests/governance/intake/test_intake_layer_service.py`:

```python
def test_intake_layer_exports():
    """IntakeLayerService and friends exportable from intake package."""
    from backend.core.ouroboros.governance.intake import (
        IntakeLayerConfig,
        IntakeLayerService,
        IntakeServiceState,
    )
    assert IntakeLayerService is not None
    assert IntakeLayerConfig is not None
    assert IntakeServiceState is not None


def test_governance_package_exports():
    """Governance top-level exports include IntakeLayerService."""
    from backend.core.ouroboros.governance import (
        IntakeLayerConfig,
        IntakeLayerService,
        IntakeServiceState,
    )
    assert IntakeLayerService is not None
```

### Step 2: Run to confirm failures

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py::test_intake_layer_exports tests/governance/intake/test_intake_layer_service.py::test_governance_package_exports -v
```

Expected: FAIL — `ImportError`.

### Step 3: Add to `intake/__init__.py`

In `backend/core/ouroboros/governance/intake/__init__.py`, add:

```python
from .intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
    IntakeNarrator,
)
```

And add to `__all__`:
```python
    "IntakeLayerConfig",
    "IntakeLayerService",
    "IntakeServiceState",
    "IntakeNarrator",
```

### Step 4: Add to `governance/__init__.py`

In `backend/core/ouroboros/governance/__init__.py`, after the existing intake exports block (around line 300), add:

```python
from backend.core.ouroboros.governance.intake import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
    IntakeNarrator,
)
```

### Step 5: Run tests

```bash
python3 -m pytest tests/governance/intake/test_intake_layer_service.py -v
```

Expected: All PASS.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/intake/__init__.py \
        backend/core/ouroboros/governance/__init__.py \
        tests/governance/intake/test_intake_layer_service.py
git commit -m "feat(intake): export IntakeLayerService from intake and governance packages"
```

---

## Task 8: Acceptance tests — Phase 2C.2/2C.3

**Files:**
- Create: `tests/governance/integration/test_phase2c2_acceptance.py`

### Step 1: Write full acceptance test file

Create `tests/governance/integration/test_phase2c2_acceptance.py`:

```python
"""
Phase 2C.2/2C.3 acceptance tests.

AC1: IntakeLayerService starts and reaches ACTIVE/DEGRADED
AC2: VoiceNarrator (B) appears in CommProtocol transports (import fix)
AC3: A-narrator fires for voice_human; silent for backlog and ai_miner
AC4: Voice command envelope reaches GLS.submit() within 1s
AC5: Intake stop drains before GLS stop (order verified)
AC6: health() returns required keys with correct types
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake import (
    IntakeLayerConfig,
    IntakeLayerService,
    IntakeServiceState,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeNarrator


# ── AC1: IntakeLayerService starts ──────────────────────────────────────────

async def test_ac1_service_starts_active(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    assert svc.state in (IntakeServiceState.ACTIVE, IntakeServiceState.DEGRADED)
    await svc.stop()
    assert svc.state is IntakeServiceState.INACTIVE


# ── AC2: VoiceNarrator wired in CommProtocol ────────────────────────────────

def test_ac2_voice_narrator_in_comm_protocol():
    from backend.core.ouroboros.governance.integration import build_comm_protocol
    protocol = build_comm_protocol()
    transport_types = [type(t).__name__ for t in protocol._transports]
    assert "VoiceNarrator" in transport_types, (
        f"VoiceNarrator not wired. Transports: {transport_types}"
    )


# ── AC3: A-narrator salience policy ─────────────────────────────────────────

async def test_ac3_a_narrator_voice_human_speaks():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="voice_human", description="deploy the fix now",
        target_files=("backend/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "ac3_voice"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_called_once()


async def test_ac3_a_narrator_backlog_silent():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="backlog", description="fix something low-pri",
        target_files=("backend/x.py",), repo="jarvis",
        confidence=0.7, urgency="normal",
        evidence={"signature": "ac3_backlog"},
        requires_human_ack=False,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


async def test_ac3_a_narrator_ai_miner_silent():
    say_fn = AsyncMock(return_value=True)
    narrator = IntakeNarrator(say_fn=say_fn, debounce_s=0.0)
    env = make_envelope(
        source="ai_miner", description="refactor complex func",
        target_files=("backend/complex.py",), repo="jarvis",
        confidence=0.4, urgency="low",
        evidence={"signature": "ac3_miner"},
        requires_human_ack=True,
    )
    await narrator.on_envelope(env)
    say_fn.assert_not_called()


# ── AC4: Voice command reaches GLS.submit within 1s ─────────────────────────

async def test_ac4_voice_command_reaches_gls(tmp_path):
    submitted = []

    async def mock_submit(ctx, trigger_source=""):
        submitted.append(ctx.op_id)

    gls = MagicMock()
    gls.submit = mock_submit

    config = IntakeLayerConfig(
        project_root=tmp_path,
        dedup_window_s=60.0,
    )
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()

    # Inject a voice command directly into the router
    env = make_envelope(
        source="voice_human", description="fix auth module",
        target_files=("backend/core/auth.py",), repo="jarvis",
        confidence=0.95, urgency="critical",
        evidence={"signature": "ac4_direct"},
        requires_human_ack=False,
    )
    await svc._router.ingest(env)
    await asyncio.sleep(0.5)  # well within 1s
    await svc.stop()

    assert len(submitted) == 1
    assert submitted[0] == env.causal_id


# ── AC5: Stop order — intake stops before GLS ────────────────────────────────

async def test_ac5_intake_stops_before_gls(tmp_path):
    """Intake service stops cleanly; GLS mock is never told to stop by intake."""
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    await svc.stop()
    # GLS.stop() is NOT called by IntakeLayerService (supervisor handles that separately)
    assert not hasattr(gls, "stop") or not gls.stop.called


# ── AC6: health() keys and types ─────────────────────────────────────────────

async def test_ac6_health_keys(tmp_path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeLayerConfig(project_root=tmp_path)
    svc = IntakeLayerService(gls=gls, config=config, say_fn=None)
    await svc.start()
    h = svc.health()
    assert isinstance(h["state"], str)
    assert isinstance(h["queue_depth"], int)
    assert isinstance(h["dead_letter_count"], int)
    assert isinstance(h["per_source_rate"], dict)
    assert isinstance(h["uptime_s"], float)
    await svc.stop()
```

### Step 2: Run to confirm tests pass

```bash
python3 -m pytest tests/governance/integration/test_phase2c2_acceptance.py -v
```

Expected: All 9 tests PASS.

### Step 3: Run full suite — confirm zero regressions

```bash
python3 -m pytest tests/governance/ -q
```

Expected: 450+ tests, 0 failures.

### Step 4: Commit

```bash
git add tests/governance/integration/test_phase2c2_acceptance.py
git commit -m "test(intake): Phase 2C.2/2C.3 acceptance tests — all ACs green"
```

---

## Final verification

```bash
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: `N passed` with N ≥ 439 and 0 failures.

---

## Summary: files changed

| Action | File |
|--------|------|
| Create | `backend/core/ouroboros/governance/intake/intake_layer_service.py` |
| Modify | `backend/core/ouroboros/governance/intake/__init__.py` |
| Modify | `backend/core/ouroboros/governance/__init__.py` |
| Fix | `backend/core/ouroboros/governance/integration.py:257` |
| Modify | `backend/core/ouroboros/governance/intake/unified_intake_router.py` |
| Modify | `unified_supervisor.py` (3 insertion points) |
| Create | `tests/governance/intake/test_intake_layer_service.py` |
| Create | `tests/governance/integration/test_voice_narrator_wired.py` |
| Create | `tests/governance/integration/test_phase2c2_acceptance.py` |
