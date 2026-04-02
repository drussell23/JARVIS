# Sub-project E: HUD Governance Boot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot the full Ouroboros governance pipeline (GovernanceStack → GovernedLoopService → IntakeLayerService) in HUD mode so all Sub-project A-D improvements are active when JARVIS runs as a HUD.

**Architecture:** A focused `hud_governance_boot.py` module encapsulates the Zone 6.8/6.9 boot sequence. Called from `backend/main.py`'s lifespan handlers (both `parallel_lifespan` and `lifespan`). Fault-isolated: governance failure never blocks the HUD API or CU tasks.

**Tech Stack:** Python 3.12, asyncio, FastAPI lifespan, pytest

**Spec:** `docs/superpowers/specs/2026-04-02-sub-e-hud-governance-boot-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/ouroboros/governance/hud_governance_boot.py` | Create | Boot/shutdown functions + HudGovernanceContext |
| `backend/main.py` | Modify | Lifespan integration (both handlers) + health endpoint |
| `tests/governance/test_hud_governance_boot.py` | Create | Unit tests for boot/shutdown/context |

---

### Task 1: HudGovernanceContext + boot/shutdown functions

**Files:**
- Create: `backend/core/ouroboros/governance/hud_governance_boot.py`
- Create: `tests/governance/test_hud_governance_boot.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_hud_governance_boot.py`:

```python
"""Tests for HUD governance boot (Sub-project E)."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HudGovernanceContext
# ---------------------------------------------------------------------------

def test_hud_gov_context_is_active_when_gls_active():
    """is_active returns True when GLS state is ACTIVE."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "ACTIVE"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=MagicMock())
    assert ctx.is_active is True


def test_hud_gov_context_is_active_when_degraded():
    """is_active returns True when GLS state is DEGRADED."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "DEGRADED"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=MagicMock())
    assert ctx.is_active is True


def test_hud_gov_context_inactive_when_gls_none():
    """is_active returns False when GLS is None."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    ctx = HudGovernanceContext(stack=None, gls=None, intake=None)
    assert ctx.is_active is False


def test_hud_gov_context_inactive_when_gls_failed():
    """is_active returns False when GLS state is FAILED."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "FAILED"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=None)
    assert ctx.is_active is False


# ---------------------------------------------------------------------------
# stop_hud_governance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_hud_governance_handles_none_components():
    """Shutdown with all-None components does not raise."""
    from backend.core.ouroboros.governance.hud_governance_boot import (
        HudGovernanceContext, stop_hud_governance,
    )
    ctx = HudGovernanceContext(stack=None, gls=None, intake=None)
    await stop_hud_governance(ctx)  # must not raise


@pytest.mark.asyncio
async def test_stop_hud_governance_calls_stop_reverse_order():
    """Shutdown calls stop in reverse order: intake → gls → stack."""
    from backend.core.ouroboros.governance.hud_governance_boot import (
        HudGovernanceContext, stop_hud_governance,
    )
    call_order = []
    intake = MagicMock()
    intake.stop = AsyncMock(side_effect=lambda: call_order.append("intake"))
    gls = MagicMock()
    gls.stop = AsyncMock(side_effect=lambda: call_order.append("gls"))
    stack = MagicMock()
    stack.stop = AsyncMock(side_effect=lambda: call_order.append("stack"))
    stack.governed_loop_service = gls

    ctx = HudGovernanceContext(stack=stack, gls=gls, intake=intake)
    await stop_hud_governance(ctx)
    assert call_order == ["intake", "gls", "stack"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_hud_governance_boot.py -v`
Expected: FAIL — `HudGovernanceContext` does not exist

- [ ] **Step 3: Implement `hud_governance_boot.py`**

Create `backend/core/ouroboros/governance/hud_governance_boot.py`:

```python
"""HUD Governance Boot — boots the Ouroboros pipeline for HUD mode.

Encapsulates the Zone 6.8/6.9 boot sequence from unified_supervisor.py
into a focused, fault-isolated function for the HUD FastAPI lifespan.

Progressive Readiness: each step is independently fault-isolated.
If governance fails, HUD still serves API and CU tasks.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class HudGovernanceContext:
    """Holds references to governance components booted in HUD mode."""
    stack: Optional[Any]
    gls: Optional[Any]
    intake: Optional[Any]

    @property
    def is_active(self) -> bool:
        """True if GovernedLoopService reached ACTIVE or DEGRADED state."""
        if self.gls is None:
            return False
        state = getattr(self.gls, "state", None)
        return state is not None and state.name in ("ACTIVE", "DEGRADED")


async def start_hud_governance(project_root: Path) -> HudGovernanceContext:
    """Boot the Ouroboros governance stack for HUD mode.

    Mirrors unified_supervisor.py Zones 6.8/6.9 but fault-isolated:
    partial failure returns a degraded context, never raises.
    """
    stack = None
    gls = None
    intake = None

    # safe_say for narrator injection
    _say_fn = None
    try:
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        _say_fn = safe_say
    except ImportError:
        logger.debug("[HUD-Gov] safe_say not available — narrators will be silent")

    # Step 1: GovernanceStack
    try:
        from backend.core.ouroboros.governance.integration import (
            GovernanceConfig,
            create_governance_stack,
        )
        _gov_config = GovernanceConfig.from_env_and_args(None)
        stack = await asyncio.wait_for(
            create_governance_stack(_gov_config),
            timeout=15.0,
        )
        await stack.start()
        # Fix ChangeEngine project_root: default ledger under ~/.jarvis resolves
        # parent chain to home dir, not repo. Override to actual project root.
        if hasattr(stack, "change_engine") and stack.change_engine is not None:
            stack.change_engine._project_root = Path(project_root)
        logger.info("[HUD-Gov] GovernanceStack started")
    except Exception as exc:
        logger.warning("[HUD-Gov] GovernanceStack failed (governance disabled): %s", exc)
        return HudGovernanceContext(stack=None, gls=None, intake=None)

    # Step 2: GovernedLoopService (Zone 6.8)
    try:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )
        _loop_config = GovernedLoopConfig.from_env(project_root=project_root)

        # Optional PrimeClient
        _prime_client = None
        try:
            from backend.core.prime_client import get_prime_client
            _prime_client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
        except Exception:
            logger.debug("[HUD-Gov] PrimeClient not available — J-Prime features degraded")

        gls = GovernedLoopService(
            stack=stack,
            prime_client=_prime_client,
            config=_loop_config,
            active_brain_set=frozenset(),  # No handshake — gate disabled
            say_fn=_say_fn,
        )
        await asyncio.wait_for(asyncio.shield(gls.start()), timeout=30.0)
        stack.governed_loop_service = gls
        logger.info("[HUD-Gov] GovernedLoopService started (state=%s)", gls.state.name)
    except Exception as exc:
        logger.warning("[HUD-Gov] GovernedLoopService failed: %s", exc)
        return HudGovernanceContext(stack=stack, gls=None, intake=None)

    # Step 3: IntakeLayerService (Zone 6.9)
    try:
        from backend.core.ouroboros.governance.intake.intake_layer_service import (
            IntakeLayerConfig,
            IntakeLayerService,
        )
        _intake_config = IntakeLayerConfig.from_env(project_root=project_root)
        intake = IntakeLayerService(gls=gls, config=_intake_config, say_fn=_say_fn)
        await asyncio.wait_for(asyncio.shield(intake.start()), timeout=30.0)
        logger.info("[HUD-Gov] IntakeLayerService started (state=%s)", intake.state.name)
    except Exception as exc:
        logger.warning("[HUD-Gov] IntakeLayerService failed: %s", exc)

    return HudGovernanceContext(stack=stack, gls=gls, intake=intake)


async def stop_hud_governance(ctx: HudGovernanceContext) -> None:
    """Shutdown governance in reverse order. Fault-isolated — never raises."""
    # 1. IntakeLayerService
    if ctx.intake is not None:
        try:
            await asyncio.wait_for(ctx.intake.stop(), timeout=10.0)
            logger.info("[HUD-Gov] IntakeLayerService stopped")
        except Exception as exc:
            logger.debug("[HUD-Gov] IntakeLayerService stop error: %s", exc)

    # 2. Clear stack back-reference
    if ctx.stack is not None:
        ctx.stack.governed_loop_service = None

    # 3. GovernedLoopService
    if ctx.gls is not None:
        try:
            await asyncio.wait_for(ctx.gls.stop(), timeout=10.0)
            logger.info("[HUD-Gov] GovernedLoopService stopped")
        except Exception as exc:
            logger.debug("[HUD-Gov] GovernedLoopService stop error: %s", exc)

    # 4. GovernanceStack
    if ctx.stack is not None:
        try:
            await asyncio.wait_for(ctx.stack.stop(), timeout=10.0)
            logger.info("[HUD-Gov] GovernanceStack stopped")
        except Exception as exc:
            logger.debug("[HUD-Gov] GovernanceStack stop error: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_hud_governance_boot.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/hud_governance_boot.py tests/governance/test_hud_governance_boot.py
git commit -m "feat(governance): add HUD governance boot module

HudGovernanceContext dataclass + start_hud_governance/stop_hud_governance
functions. Mirrors supervisor Zones 6.8/6.9 with fault isolation.
Fixes ChangeEngine project_root for HUD deployments.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire governance boot into parallel_lifespan

**Files:**
- Modify: `backend/main.py` (inside `parallel_lifespan`, before yield at line 2204)

- [ ] **Step 1: Add governance boot before yield**

In `backend/main.py`, find `async def parallel_lifespan(app: FastAPI)`. Search for the block that ends just before `yield` (around line 2203). The current code before yield ends with:

```python
            except Exception as e:
                logger.warning("[HUD] SSE consumer failed: %s", e)

        yield
```

Insert the governance boot block BETWEEN the SSE consumer block and `yield`:

```python
            except Exception as e:
                logger.warning("[HUD] SSE consumer failed: %s", e)

        # ── HUD Governance Boot (Sub-project E) ──────────────────────────
        if HUD_MODE and os.environ.get("JARVIS_HUD_GOVERNANCE_ENABLED", "1").strip().lower() not in ("0", "false", "no"):
            try:
                from backend.core.ouroboros.governance.hud_governance_boot import (
                    start_hud_governance,
                )
                app.state.hud_gov_ctx = await start_hud_governance(
                    project_root=Path.cwd(),
                )
                if app.state.hud_gov_ctx.is_active:
                    logger.info("[HUD] Ouroboros governance ACTIVE — full pipeline operational")
                else:
                    logger.warning("[HUD] Ouroboros governance DEGRADED — partial pipeline")
            except Exception as exc:
                logger.warning("[HUD] Governance boot failed (CU still operational): %s", exc)
                app.state.hud_gov_ctx = None

        yield
```

- [ ] **Step 2: Add governance shutdown after yield's shutdown section**

In the same `parallel_lifespan`, find the shutdown section. Search for `"Parallel startup shutdown complete"` (around line 2318). BEFORE that line, insert:

```python
        # ── HUD Governance Shutdown (Sub-project E) ──────────────────────
        _gov_ctx = getattr(app.state, "hud_gov_ctx", None)
        if _gov_ctx is not None:
            try:
                from backend.core.ouroboros.governance.hud_governance_boot import (
                    stop_hud_governance,
                )
                await stop_hud_governance(_gov_ctx)
                logger.info("[HUD] Governance shutdown complete")
            except Exception as exc:
                logger.debug("[HUD] Governance shutdown error: %s", exc)

```

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat(hud): wire governance boot into parallel_lifespan

Boots GovernanceStack → GovernedLoopService → IntakeLayerService
in HUD mode during FastAPI parallel_lifespan startup. Env-gated
via JARVIS_HUD_GOVERNANCE_ENABLED. Stored on app.state for health checks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire governance boot into standard lifespan

**Files:**
- Modify: `backend/main.py` (inside `lifespan`, before yield at line 4515)

- [ ] **Step 1: Add governance boot before yield**

In `backend/main.py`, find `async def lifespan(app: FastAPI)`. Search for the yield at line 4515. Find the block just before it. Insert the SAME governance boot block:

```python
    # ── HUD Governance Boot (Sub-project E) ──────────────────────────
    if HUD_MODE and os.environ.get("JARVIS_HUD_GOVERNANCE_ENABLED", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from backend.core.ouroboros.governance.hud_governance_boot import (
                start_hud_governance,
            )
            app.state.hud_gov_ctx = await start_hud_governance(
                project_root=Path.cwd(),
            )
            if app.state.hud_gov_ctx.is_active:
                logger.info("[HUD] Ouroboros governance ACTIVE — full pipeline operational")
            else:
                logger.warning("[HUD] Ouroboros governance DEGRADED — partial pipeline")
        except Exception as exc:
            logger.warning("[HUD] Governance boot failed (CU still operational): %s", exc)
            app.state.hud_gov_ctx = None

    yield
```

- [ ] **Step 2: Add governance shutdown in cleanup section**

Search for `"Shutting down JARVIS backend"` in the `lifespan` function (around line 4518). BEFORE the final cleanup, insert:

```python
    # ── HUD Governance Shutdown (Sub-project E) ──────────────────────
    _gov_ctx = getattr(app.state, "hud_gov_ctx", None)
    if _gov_ctx is not None:
        try:
            from backend.core.ouroboros.governance.hud_governance_boot import (
                stop_hud_governance,
            )
            await stop_hud_governance(_gov_ctx)
            logger.info("[HUD] Governance shutdown complete")
        except Exception as exc:
            logger.debug("[HUD] Governance shutdown error: %s", exc)

```

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat(hud): wire governance boot into standard lifespan

Same governance boot as parallel_lifespan, ensuring both code paths
start the Ouroboros pipeline in HUD mode.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Health endpoint update

**Files:**
- Modify: `backend/main.py` (health endpoint)

- [ ] **Step 1: Find and update the health endpoint**

Search `backend/main.py` for `"governance_ready"`. It's in the `/health/readiness-tier` route (around line 6546). The current code:

```python
            "governance_ready": pr.is_fully_operational,
```

Replace with:

```python
            "governance_ready": (
                getattr(getattr(request.app.state, "hud_gov_ctx", None), "is_active", False)
                if HUD_MODE
                else pr.is_fully_operational
            ),
```

Note: The route handler has access to `request` (FastAPI injects it). If the route doesn't have `request` as a parameter, check the function signature — it may use a different mechanism. If `request` is not available, use the module-level `HUD_MODE` flag and import `app` directly.

- [ ] **Step 2: Commit**

```bash
git add backend/main.py
git commit -m "feat(hud): wire governance_ready health check to actual HUD state

In HUD mode, governance_ready now reflects HudGovernanceContext.is_active
instead of always returning pr.is_fully_operational (which doesn't know
about HUD-booted governance).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full regression and verify

**Files:** None (verification only)

- [ ] **Step 1: Run HUD governance boot tests**

Run: `python3 -m pytest tests/governance/test_hud_governance_boot.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run all Sub-project A-D tests**

Run: `python3 -m pytest tests/governance/ -v --timeout=30 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 3: Run intake tests**

Run: `python3 -m pytest tests/governance/intake/ -v --timeout=30 2>&1 | tail -10`
Expected: All pass

- [ ] **Step 4: Verify HUD mode comment in brainstem**

Add a comment to `brainstem/__main__.py` about dual-process ledger isolation:

In `brainstem/__main__.py`, after the existing "Ports:" comment block (around line 15), add:

```python
# Dual-process: when running alongside supervisor (8010), set separate
# ledger dirs to avoid lock contention:
#   OUROBOROS_LEDGER_DIR=~/.jarvis/ouroboros/ledger-hud/ python3 -m brainstem
```

- [ ] **Step 5: Commit**

```bash
git add brainstem/__main__.py
git commit -m "docs(brainstem): add dual-process ledger isolation note

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
