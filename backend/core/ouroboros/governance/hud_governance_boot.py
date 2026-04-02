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
