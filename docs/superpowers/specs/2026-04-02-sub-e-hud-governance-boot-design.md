# Sub-project E: HUD Governance Boot

**Date:** 2026-04-02
**Parent:** [Ouroboros HUD Pipeline Program](2026-04-02-ouroboros-hud-pipeline-program.md)
**Status:** Approved
**Depends on:** Sub-projects A, B, C, D

## Problem

HUD mode (`JARVIS_MODE=hud`) boots via `brainstem/__main__.py` → uvicorn → `backend/main.py:app`. This path never creates GovernanceStack, GovernedLoopService, or IntakeLayerService. All four sub-projects (A-D) improve the governance pipeline, but the pipeline doesn't run in HUD mode.

The CUExecutionSensor feeds telemetry (backend/main.py:2131), but without IntakeLayerService the sensor has no router. Without GovernedLoopService the orchestrator doesn't run. Without GovernanceStack the CommProtocol/VoiceNarrator/ChangeEngine don't exist.

Result: CU failures are counted but never flow to Ouroboros for self-improvement in HUD mode.

## Changes

### 1. HUD Governance Boot Module

**File:** `backend/core/ouroboros/governance/hud_governance_boot.py` (new)

A focused module encapsulating the HUD governance boot sequence. Not inline in the 5000-line `main.py`.

#### `HudGovernanceContext` dataclass

```python
@dataclass
class HudGovernanceContext:
    stack: Optional[GovernanceStack]
    gls: Optional[GovernedLoopService]
    intake: Optional[IntakeLayerService]

    @property
    def is_active(self) -> bool:
        if self.gls is None:
            return False
        state = getattr(self.gls, "state", None)
        return state is not None and state.name in ("ACTIVE", "DEGRADED")
```

#### `start_hud_governance(project_root: Path) -> HudGovernanceContext`

Boot sequence (mirrors supervisor Zones 6.8/6.9):

1. `GovernanceConfig.from_env_and_args(None)` — reads env vars, no CLI args
2. `create_governance_stack(config)` — builds governance components. No `event_bus`, `oracle`, or `learning_memory` in HUD v1 (reduced cross-repo/oracle capabilities accepted)
3. `stack.start()`
4. `GovernedLoopConfig.from_env(project_root=project_root)`
5. `GovernedLoopService(stack=stack, config=loop_config, say_fn=safe_say, active_brain_set=frozenset())` — gate disabled (no J-Prime handshake in HUD mode)
6. `gls.start()` — 30s timeout with `asyncio.shield`
7. `IntakeLayerConfig.from_env(project_root=project_root)`
8. `IntakeLayerService(gls=gls, config=intake_config, say_fn=safe_say)`
9. `intake.start()` — 30s timeout with `asyncio.shield`

**Fault isolation (Manifesto §2 Progressive Readiness):**
- Each major step wrapped in `try/except`. 
- If GovernanceStack fails → return `HudGovernanceContext(stack=None, gls=None, intake=None)`
- If GLS fails → return context with stack but no gls/intake
- If IntakeLayerService fails → return context with stack+gls but no intake
- HUD API and CU tasks always come up regardless of governance state

**`safe_say` injection:**
```python
_say_fn = None
try:
    from backend.core.supervisor.unified_voice_orchestrator import safe_say
    _say_fn = safe_say
except ImportError:
    pass
```
Passed to both GLS and IntakeLayerService. Silent narrators if unavailable.

#### `stop_hud_governance(ctx: HudGovernanceContext) -> None`

Reverse order shutdown with fault isolation:
1. `intake.stop()` — 10s timeout
2. `gls.stop()` — 10s timeout  
3. Stack cleanup (if stack has stop/cleanup method)

Each step wrapped in `try/except` — never raises.

### 2. Lifespan Integration in backend/main.py

**File:** `backend/main.py` — modify lifespan handler

Inside the existing lifespan (either `parallel_lifespan` or `lifespan`), add HUD-gated governance boot after core components initialize:

```python
_hud_gov_ctx = None
if os.environ.get("JARVIS_MODE") == "hud" and \
   os.environ.get("JARVIS_HUD_GOVERNANCE_ENABLED", "1").strip().lower() not in ("0", "false", "no"):
    try:
        from backend.core.ouroboros.governance.hud_governance_boot import (
            start_hud_governance, stop_hud_governance,
        )
        _hud_gov_ctx = await start_hud_governance(project_root=Path.cwd())
        if _hud_gov_ctx.is_active:
            logger.info("[HUD] Ouroboros governance ACTIVE — full pipeline operational")
        else:
            logger.warning("[HUD] Ouroboros governance DEGRADED — partial pipeline")
    except Exception as exc:
        logger.warning("[HUD] Governance boot failed (CU still operational): %s", exc)
```

Shutdown in the lifespan's cleanup section:
```python
if _hud_gov_ctx is not None:
    await stop_hud_governance(_hud_gov_ctx)
```

**Env-gated opt-out:** `JARVIS_HUD_GOVERNANCE_ENABLED` (default `"1"`). Set to `"0"` to disable governance in HUD mode for debugging or lightweight deployments.

### 3. Health Endpoint Update

**File:** `backend/main.py` — modify `/health/readiness-tier`

The endpoint already has `"governance_ready": pr.is_fully_operational`. Wire it to check actual governance state:

```python
"governance_ready": (_hud_gov_ctx is not None and _hud_gov_ctx.is_active) if os.environ.get("JARVIS_MODE") == "hud" else pr.is_fully_operational,
```

### 4. Ledger Isolation for Dual-Process Safety

When supervisor (port 8010) and HUD (port 8011) run simultaneously on the same machine, they must not share ledger paths or governance locks.

**Default behavior:** `OUROBOROS_LEDGER_DIR` defaults to `~/.jarvis/ouroboros/ledger/`. When running both processes, set a separate dir for HUD:

```bash
# HUD mode with separate ledger
OUROBOROS_LEDGER_DIR=~/.jarvis/ouroboros/ledger-hud/ python3 -m brainstem
```

**Spec note:** This is an operational concern, not a code change. Document in `brainstem/__main__.py` comments that dual-process operation requires separate ledger dirs.

## Testing Strategy

| Test | File | What it verifies |
|------|------|-----------------|
| `test_start_hud_governance_success` | `test_hud_governance_boot.py` | Full boot returns active context |
| `test_start_hud_governance_stack_failure` | `test_hud_governance_boot.py` | Stack failure → degraded context, no raise |
| `test_start_hud_governance_gls_failure` | `test_hud_governance_boot.py` | GLS failure → stack alive, gls/intake None |
| `test_stop_hud_governance_reverse_order` | `test_hud_governance_boot.py` | Shutdown calls stop in reverse order |
| `test_stop_hud_governance_partial` | `test_hud_governance_boot.py` | Shutdown handles None components gracefully |
| `test_hud_gov_context_is_active` | `test_hud_governance_boot.py` | is_active property reflects GLS state |
| `test_hud_gov_context_inactive_when_none` | `test_hud_governance_boot.py` | is_active returns False when gls is None |

## Files Created/Modified

| File | Action |
|------|--------|
| `backend/core/ouroboros/governance/hud_governance_boot.py` | Create |
| `backend/main.py` | Modify (lifespan handler + health endpoint) |
| `tests/governance/test_hud_governance_boot.py` | Create |

## Out of Scope

- Cross-repo EventBridge wiring (HUD v1 has no multi-repo support)
- Oracle/CodebaseKnowledgeGraph integration (requires heavy infra)
- LearningMemory bridge (future enhancement)
- J-Prime boot handshake (HUD uses `active_brain_set=frozenset()` — gate disabled)
- Refactoring unified_supervisor.py to use hud_governance_boot.py (future DRY)
- Dual-process lock contention prevention (operational — separate ledger dirs)
