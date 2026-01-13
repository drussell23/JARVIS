"""
PROJECT TRINITY: Command Handlers for JARVIS Body

This module registers handlers for Trinity commands received from J-Prime (Mind)
through Reactor Core (Nerves). These handlers execute the actual operations
in JARVIS.

ARCHITECTURE:
┌────────────┐    Commands    ┌──────────────┐    Execute    ┌────────────┐
│  J-PRIME   │ ──────────────│ REACTOR CORE │ ─────────────│   JARVIS   │
│   (Mind)   │               │   (Nerves)   │              │   (Body)   │
└────────────┘    Results    └──────────────┘              └────────────┘
                                                                  ↓
                                                           This Module

USAGE:
    from backend.system.trinity_handlers import register_trinity_handlers

    bridge = get_reactor_bridge()
    await bridge.connect_async()

    # Register all handlers
    register_trinity_handlers(bridge)
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Import Trinity types
try:
    from backend.system.reactor_bridge import (
        get_reactor_bridge,
        TrinityCommand,
        TrinityIntent,
        TrinitySource,
    )
    TRINITY_AVAILABLE = True
except ImportError:
    try:
        from system.reactor_bridge import (
            get_reactor_bridge,
            TrinityCommand,
            TrinityIntent,
            TrinitySource,
        )
        TRINITY_AVAILABLE = True
    except ImportError:
        TRINITY_AVAILABLE = False
        logger.warning("[Trinity] ReactorBridge not available")

# Import JARVIS components
VisualMonitorAgent = None
VISUAL_MONITOR_AVAILABLE = False
try:
    from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent
    VISUAL_MONITOR_AVAILABLE = True
except ImportError:
    try:
        from neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent
        VISUAL_MONITOR_AVAILABLE = True
    except ImportError:
        pass

get_yabai_detector = None
YABAI_AVAILABLE = False
try:
    from backend.vision.yabai_space_detector import get_yabai_detector
    YABAI_AVAILABLE = True
except ImportError:
    try:
        from vision.yabai_space_detector import get_yabai_detector
        YABAI_AVAILABLE = True
    except ImportError:
        pass

get_cryostasis_manager = None
CRYOSTASIS_AVAILABLE = False
try:
    from backend.system.cryostasis_manager import get_cryostasis_manager
    CRYOSTASIS_AVAILABLE = True
except ImportError:
    try:
        from system.cryostasis_manager import get_cryostasis_manager
        CRYOSTASIS_AVAILABLE = True
    except ImportError:
        pass

get_phantom_manager = None
PHANTOM_AVAILABLE = False
try:
    from backend.system.phantom_hardware_manager import get_phantom_manager
    PHANTOM_AVAILABLE = True
except ImportError:
    try:
        from system.phantom_hardware_manager import get_phantom_manager
        PHANTOM_AVAILABLE = True
    except ImportError:
        pass

get_persistence_manager = None
PERSISTENCE_AVAILABLE = False
try:
    from backend.vision.ghost_persistence_manager import get_persistence_manager
    PERSISTENCE_AVAILABLE = True
except ImportError:
    try:
        from vision.ghost_persistence_manager import get_persistence_manager
        PERSISTENCE_AVAILABLE = True
    except ImportError:
        pass


# =============================================================================
# MODEL HOT-SWAP IMPORTS
# =============================================================================

get_model_manager = None
MODEL_MANAGER_AVAILABLE = False
try:
    from backend.core.model_manager import get_model_manager
    MODEL_MANAGER_AVAILABLE = True
except ImportError:
    try:
        from core.model_manager import get_model_manager
        MODEL_MANAGER_AVAILABLE = True
    except ImportError:
        pass

get_unified_model_serving = None
UNIFIED_MODEL_SERVING_AVAILABLE = False
try:
    from backend.core.unified_model_serving import get_unified_model_serving
    UNIFIED_MODEL_SERVING_AVAILABLE = True
except ImportError:
    try:
        from core.unified_model_serving import get_unified_model_serving
        UNIFIED_MODEL_SERVING_AVAILABLE = True
    except ImportError:
        pass


# =============================================================================
# GLOBAL STATE
# =============================================================================

_visual_monitor_agent: Optional[VisualMonitorAgent] = None
_handlers_registered = False
_active_model_path: Optional[str] = None  # v101.0: Track active model


# =============================================================================
# HANDLER IMPLEMENTATIONS
# =============================================================================

async def handle_start_surveillance(command: TrinityCommand) -> None:
    """
    Handle START_SURVEILLANCE command from J-Prime.

    Activates the Ghost Monitor Protocol to watch for visual triggers.
    """
    global _visual_monitor_agent

    payload = command.payload
    app_name = payload.get("app_name", "")
    trigger_text = payload.get("trigger_text", "")
    all_spaces = payload.get("all_spaces", True)
    max_duration = payload.get("max_duration")

    logger.info(
        f"[Trinity] Executing START_SURVEILLANCE: app={app_name}, "
        f"trigger='{trigger_text}', all_spaces={all_spaces}"
    )

    if not VISUAL_MONITOR_AVAILABLE or VisualMonitorAgent is None:
        logger.error("[Trinity] VisualMonitorAgent not available")
        await _send_nack(command, "VisualMonitorAgent not available")
        return

    try:
        # Get or create agent
        if _visual_monitor_agent is None:
            _visual_monitor_agent = VisualMonitorAgent()

        # Start surveillance
        result = await _visual_monitor_agent.watch(
            app_name=app_name,
            trigger_text=trigger_text,
            all_spaces=all_spaces,
            max_duration_seconds=max_duration,
        )

        if result.get("success"):
            logger.info(f"[Trinity] Surveillance started for {app_name}")
            await _send_ack(command, f"Surveillance started for {app_name}")
        else:
            error = result.get("error", "Unknown error")
            logger.error(f"[Trinity] Surveillance failed: {error}")
            await _send_nack(command, error)

    except Exception as e:
        logger.error(f"[Trinity] START_SURVEILLANCE error: {e}")
        await _send_nack(command, str(e))


async def handle_stop_surveillance(command: TrinityCommand) -> None:
    """Handle STOP_SURVEILLANCE command."""
    global _visual_monitor_agent

    app_name = command.payload.get("app_name")

    logger.info(f"[Trinity] Executing STOP_SURVEILLANCE: app={app_name or 'all'}")

    if _visual_monitor_agent is None:
        await _send_ack(command, "No surveillance active")
        return

    try:
        await _visual_monitor_agent.stop_watching(app_name)
        logger.info(f"[Trinity] Surveillance stopped for {app_name or 'all'}")
        await _send_ack(command, f"Surveillance stopped for {app_name or 'all'}")

    except Exception as e:
        logger.error(f"[Trinity] STOP_SURVEILLANCE error: {e}")
        await _send_nack(command, str(e))


async def handle_bring_back_window(command: TrinityCommand) -> None:
    """
    Handle BRING_BACK_WINDOW command from J-Prime.

    Restores windows from Ghost Display to main display.
    """
    payload = command.payload
    app_name = payload.get("app_name")

    logger.info(f"[Trinity] Executing BRING_BACK_WINDOW: app={app_name or 'all'}")

    # First thaw any frozen apps
    if CRYOSTASIS_AVAILABLE and get_cryostasis_manager:
        try:
            cryo = get_cryostasis_manager()
            if app_name:
                if cryo.is_frozen(app_name):
                    await cryo.thaw_app_async(app_name, wait_after_thaw=True)
            else:
                # Thaw all frozen apps
                for frozen_app in cryo.get_frozen_app_names():
                    await cryo.thaw_app_async(frozen_app, wait_after_thaw=True)
        except Exception as e:
            logger.warning(f"[Trinity] Thaw error (continuing): {e}")

    # Use persistence manager to bring back windows
    if PERSISTENCE_AVAILABLE and get_persistence_manager:
        try:
            pm = get_persistence_manager()
            result = await pm.restore_windows_async(app_filter=app_name)

            if result.get("success"):
                count = result.get("restored_count", 0)
                logger.info(f"[Trinity] Restored {count} windows for {app_name or 'all'}")
                await _send_ack(command, f"Restored {count} windows")
            else:
                error = result.get("error", "Unknown error")
                logger.error(f"[Trinity] Bring back failed: {error}")
                await _send_nack(command, error)

        except Exception as e:
            logger.error(f"[Trinity] BRING_BACK_WINDOW error: {e}")
            await _send_nack(command, str(e))
    else:
        await _send_nack(command, "GhostPersistenceManager not available")


async def handle_exile_window(command: TrinityCommand) -> None:
    """Handle EXILE_WINDOW command - teleport window to Ghost Display."""
    payload = command.payload
    app_name = payload.get("app_name", "")
    window_title = payload.get("window_title")

    logger.info(f"[Trinity] Executing EXILE_WINDOW: app={app_name}")

    if not YABAI_AVAILABLE or get_yabai_detector is None:
        await _send_nack(command, "Yabai detector not available")
        return

    try:
        yabai = get_yabai_detector()
        result = await yabai.teleport_window_to_ghost_async(
            app_name=app_name,
            window_title=window_title,
            auto_create_display=True,
        )

        if result.get("success"):
            logger.info(f"[Trinity] Window exiled: {app_name}")
            await _send_ack(command, f"Window exiled: {app_name}")
        else:
            error = result.get("error", "Unknown error")
            await _send_nack(command, error)

    except Exception as e:
        logger.error(f"[Trinity] EXILE_WINDOW error: {e}")
        await _send_nack(command, str(e))


async def handle_freeze_app(command: TrinityCommand) -> None:
    """Handle FREEZE_APP command - SIGSTOP an app."""
    payload = command.payload
    app_name = payload.get("app_name", "")
    reason = payload.get("reason", "trinity_command")

    logger.info(f"[Trinity] Executing FREEZE_APP: app={app_name}")

    if not CRYOSTASIS_AVAILABLE or get_cryostasis_manager is None:
        await _send_nack(command, "CryostasisManager not available")
        return

    try:
        cryo = get_cryostasis_manager()
        result = await cryo.freeze_app_async(app_name, reason=reason)

        if result.get("success"):
            count = result.get("frozen_count", 0)
            logger.info(f"[Trinity] Froze {count} processes for {app_name}")
            await _send_ack(command, f"Froze {count} processes")
        else:
            error = result.get("error", "Unknown error")
            await _send_nack(command, error)

    except Exception as e:
        logger.error(f"[Trinity] FREEZE_APP error: {e}")
        await _send_nack(command, str(e))


async def handle_thaw_app(command: TrinityCommand) -> None:
    """Handle THAW_APP command - SIGCONT a frozen app."""
    payload = command.payload
    app_name = payload.get("app_name", "")

    logger.info(f"[Trinity] Executing THAW_APP: app={app_name}")

    if not CRYOSTASIS_AVAILABLE or get_cryostasis_manager is None:
        await _send_nack(command, "CryostasisManager not available")
        return

    try:
        cryo = get_cryostasis_manager()
        result = await cryo.thaw_app_async(app_name, wait_after_thaw=True)

        if result.get("success"):
            duration = result.get("freeze_duration_seconds", 0)
            logger.info(f"[Trinity] Thawed {app_name} after {duration:.1f}s")
            await _send_ack(command, f"Thawed after {duration:.1f}s")
        else:
            error = result.get("error", "Unknown error")
            await _send_nack(command, error)

    except Exception as e:
        logger.error(f"[Trinity] THAW_APP error: {e}")
        await _send_nack(command, str(e))


async def handle_create_ghost_display(command: TrinityCommand) -> None:
    """Handle CREATE_GHOST_DISPLAY command - create virtual display."""
    logger.info("[Trinity] Executing CREATE_GHOST_DISPLAY")

    if not PHANTOM_AVAILABLE or get_phantom_manager is None:
        await _send_nack(command, "PhantomHardwareManager not available")
        return

    try:
        phantom = get_phantom_manager()
        success, error = await phantom.ensure_ghost_display_exists_async()

        if success:
            logger.info("[Trinity] Ghost Display created/verified")
            await _send_ack(command, "Ghost Display ready")
        else:
            await _send_nack(command, error or "Failed to create Ghost Display")

    except Exception as e:
        logger.error(f"[Trinity] CREATE_GHOST_DISPLAY error: {e}")
        await _send_nack(command, str(e))


async def handle_ping(command: TrinityCommand) -> None:
    """Handle PING command - respond with PONG."""
    logger.debug("[Trinity] Received PING, sending PONG")
    await _send_ack(command, "pong")


async def handle_execute_plan(command: TrinityCommand) -> None:
    """
    Handle EXECUTE_PLAN command from J-Prime.

    This is the primary cognitive output from J-Prime - a multi-step
    plan of actions for JARVIS to execute sequentially.
    """
    payload = command.payload
    plan_id = payload.get("plan_id", "unknown")
    steps = payload.get("steps", [])
    context = payload.get("context", {})

    logger.info(f"[Trinity] Executing PLAN: id={plan_id}, steps={len(steps)}")

    if not steps:
        await _send_nack(command, "No steps in plan")
        return

    try:
        results = []

        for i, step in enumerate(steps):
            step_intent = step.get("intent")
            step_payload = step.get("payload", {})

            logger.info(f"[Trinity] Plan step {i+1}/{len(steps)}: {step_intent}")

            # Create a sub-command for this step
            step_command = TrinityCommand(
                source=TrinitySource.J_PRIME,
                intent=TrinityIntent(step_intent),
                payload=step_payload,
                requires_ack=False,  # Don't send individual ACKs
            )

            # Dispatch to appropriate handler
            handler = _get_handler_for_intent(step_intent)
            if handler:
                await handler(step_command)
                results.append({"step": i, "intent": step_intent, "success": True})
            else:
                results.append({
                    "step": i,
                    "intent": step_intent,
                    "success": False,
                    "error": f"No handler for {step_intent}",
                })

        # Send final ACK with results
        success_count = sum(1 for r in results if r.get("success"))
        await _send_ack(
            command,
            f"Plan {plan_id} complete: {success_count}/{len(steps)} steps succeeded"
        )

    except Exception as e:
        logger.error(f"[Trinity] EXECUTE_PLAN error: {e}")
        await _send_nack(command, str(e))


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_handler_for_intent(intent: str) -> Optional[Callable]:
    """Get handler function for a given intent string."""
    handlers = {
        "start_surveillance": handle_start_surveillance,
        "stop_surveillance": handle_stop_surveillance,
        "bring_back_window": handle_bring_back_window,
        "exile_window": handle_exile_window,
        "freeze_app": handle_freeze_app,
        "thaw_app": handle_thaw_app,
        "create_ghost_display": handle_create_ghost_display,
        "ping": handle_ping,
        "execute_plan": handle_execute_plan,
        # v77.2: Coding Council evolution handlers
        "evolve_code": _get_evolution_handler("evolve_code"),
        "evolution_status": _get_evolution_handler("evolution_status"),
        "evolution_rollback": _get_evolution_handler("evolution_rollback"),
    }
    return handlers.get(intent)


def _get_evolution_handler(handler_name: str) -> Optional[Callable]:
    """Lazily get evolution handler to avoid circular imports."""
    try:
        from backend.core.coding_council.integration import get_trinity_evolution_handler
        handler = get_trinity_evolution_handler()
        return getattr(handler, f"handle_{handler_name}", None)
    except ImportError:
        try:
            from core.coding_council.integration import get_trinity_evolution_handler
            handler = get_trinity_evolution_handler()
            return getattr(handler, f"handle_{handler_name}", None)
        except ImportError:
            return None


async def _send_ack(command: TrinityCommand, message: str = "") -> None:
    """Send ACK response for a command."""
    if not TRINITY_AVAILABLE:
        return

    try:
        bridge = get_reactor_bridge()
        if bridge.is_connected():
            await bridge.send_ack_async(command, success=True, message=message)
    except Exception as e:
        logger.debug(f"[Trinity] ACK send failed: {e}")


async def _send_nack(command: TrinityCommand, error: str) -> None:
    """Send NACK response for a command."""
    if not TRINITY_AVAILABLE:
        return

    try:
        bridge = get_reactor_bridge()
        if bridge.is_connected():
            await bridge.send_ack_async(command, success=False, message=error)
    except Exception as e:
        logger.debug(f"[Trinity] NACK send failed: {e}")


# =============================================================================
# v101.0: MODEL LIFECYCLE HANDLERS (Trinity Loop - The Key Events!)
# =============================================================================

async def handle_model_ready(command: TrinityCommand) -> None:
    """
    Handle MODEL_READY event from Reactor Core.

    THIS IS THE KEY EVENT THAT CLOSES THE TRINITY LOOP:
    JARVIS → Experiences → Reactor Core → Training → MODEL_READY → JARVIS
    """
    global _active_model_path

    payload = command.payload
    model_name = payload.get("model_name", "unknown")
    model_path = payload.get("model_path", "")
    model_type = payload.get("model_type", "llm")
    capabilities = payload.get("capabilities", [])
    gguf_path = payload.get("gguf_path")  # For llama.cpp models
    adapter_path = payload.get("adapter_path")  # For LoRA adapters
    training_stats = payload.get("training_stats", {})

    logger.info(
        f"[Trinity] MODEL_READY received: {model_name} ({model_type})"
    )
    logger.info(f"[Trinity] Model path: {model_path or gguf_path or adapter_path}")

    try:
        # Track previous model for potential rollback
        previous_model = _active_model_path

        # Step 1: Validate model exists
        from pathlib import Path
        effective_path = gguf_path or model_path or adapter_path
        if effective_path and not Path(effective_path).exists():
            logger.error(f"[Trinity] Model path does not exist: {effective_path}")
            await _send_nack(command, f"Model path does not exist: {effective_path}")
            return

        # Step 2: Hot-swap in model serving layer
        hot_swap_success = False

        if UNIFIED_MODEL_SERVING_AVAILABLE and get_unified_model_serving:
            try:
                serving = await get_unified_model_serving()
                if serving:
                    # Register new model endpoint
                    await serving.register_model(
                        model_id=model_name,
                        model_path=effective_path,
                        model_type=model_type,
                        capabilities=capabilities,
                        metadata={
                            "source": "reactor_core",
                            "training_stats": training_stats,
                            "adapter_path": adapter_path,
                        },
                    )
                    logger.info(f"[Trinity] Registered model in UnifiedModelServing: {model_name}")
                    hot_swap_success = True
            except Exception as e:
                logger.error(f"[Trinity] UnifiedModelServing registration failed: {e}")

        if MODEL_MANAGER_AVAILABLE and get_model_manager:
            try:
                manager = get_model_manager()
                if manager:
                    # Notify model manager of new model
                    await manager.on_model_ready(
                        model_name=model_name,
                        model_path=effective_path,
                        model_type=model_type,
                    )
                    logger.info(f"[Trinity] Notified ModelManager: {model_name}")
                    hot_swap_success = True
            except AttributeError:
                # on_model_ready not implemented
                logger.debug("[Trinity] ModelManager.on_model_ready not available")
            except Exception as e:
                logger.error(f"[Trinity] ModelManager notification failed: {e}")

        # Step 3: Update active model tracking
        _active_model_path = effective_path

        # Step 4: Publish acknowledgment
        if hot_swap_success:
            logger.info(f"[Trinity] MODEL HOT-SWAP COMPLETE: {model_name}")
            await _send_ack(command, f"Model hot-swap complete: {model_name}")

            # Publish MODEL_VALIDATED event
            try:
                bridge = get_reactor_bridge()
                if bridge.is_connected():
                    await bridge.send_event_async(
                        TrinityIntent.MODEL_VALIDATED,
                        payload={
                            "model_name": model_name,
                            "model_path": effective_path,
                            "previous_model": previous_model,
                            "hot_swap_success": True,
                        },
                    )
            except Exception as e:
                logger.debug(f"[Trinity] MODEL_VALIDATED publish failed: {e}")
        else:
            logger.warning(f"[Trinity] Model registered but hot-swap may be incomplete")
            await _send_ack(command, f"Model registered (hot-swap partial): {model_name}")

    except Exception as e:
        logger.error(f"[Trinity] MODEL_READY error: {e}")
        import traceback
        traceback.print_exc()
        await _send_nack(command, str(e))


async def handle_model_rollback(command: TrinityCommand) -> None:
    """
    Handle MODEL_ROLLBACK command to revert to previous model.
    """
    global _active_model_path

    payload = command.payload
    target_model = payload.get("model_path") or payload.get("model_name")
    reason = payload.get("reason", "Manual rollback requested")

    logger.info(f"[Trinity] MODEL_ROLLBACK: {reason}")

    try:
        if UNIFIED_MODEL_SERVING_AVAILABLE and get_unified_model_serving:
            serving = await get_unified_model_serving()
            if serving and hasattr(serving, "rollback_model"):
                await serving.rollback_model(target_model)
                logger.info(f"[Trinity] Model rolled back to: {target_model}")
                await _send_ack(command, f"Rolled back to: {target_model}")
                return

        # Fallback: just update tracking
        _active_model_path = target_model
        await _send_ack(command, f"Rollback recorded (manual switch required): {target_model}")

    except Exception as e:
        logger.error(f"[Trinity] MODEL_ROLLBACK error: {e}")
        await _send_nack(command, str(e))


async def handle_update_model_routing(command: TrinityCommand) -> None:
    """
    Handle UPDATE_MODEL_ROUTING command to change which model handles requests.
    """
    payload = command.payload
    model_id = payload.get("model_id")
    route_patterns = payload.get("route_patterns", [])  # e.g., ["conversation/*", "code/*"]

    logger.info(f"[Trinity] UPDATE_MODEL_ROUTING: {model_id} -> {route_patterns}")

    try:
        if UNIFIED_MODEL_SERVING_AVAILABLE and get_unified_model_serving:
            serving = await get_unified_model_serving()
            if serving and hasattr(serving, "update_routing"):
                await serving.update_routing(model_id, route_patterns)
                await _send_ack(command, f"Routing updated for: {model_id}")
                return

        await _send_ack(command, f"Routing update recorded: {model_id}")

    except Exception as e:
        logger.error(f"[Trinity] UPDATE_MODEL_ROUTING error: {e}")
        await _send_nack(command, str(e))


# =============================================================================
# REGISTRATION
# =============================================================================

def register_trinity_handlers(bridge=None) -> bool:
    """
    Register all Trinity command handlers with the ReactorCoreBridge.

    Args:
        bridge: Optional ReactorCoreBridge instance. If None, uses singleton.

    Returns:
        True if handlers registered successfully
    """
    global _handlers_registered

    if _handlers_registered:
        logger.debug("[Trinity] Handlers already registered")
        return True

    if not TRINITY_AVAILABLE:
        logger.warning("[Trinity] Cannot register handlers - Trinity not available")
        return False

    try:
        if bridge is None:
            bridge = get_reactor_bridge()

        # Register handlers for each intent
        bridge.register_handler(handle_start_surveillance, [TrinityIntent.START_SURVEILLANCE])
        bridge.register_handler(handle_stop_surveillance, [TrinityIntent.STOP_SURVEILLANCE])
        bridge.register_handler(handle_bring_back_window, [TrinityIntent.BRING_BACK_WINDOW])
        bridge.register_handler(handle_exile_window, [TrinityIntent.EXILE_WINDOW])
        bridge.register_handler(handle_freeze_app, [TrinityIntent.FREEZE_APP])
        bridge.register_handler(handle_thaw_app, [TrinityIntent.THAW_APP])
        bridge.register_handler(handle_create_ghost_display, [TrinityIntent.CREATE_GHOST_DISPLAY])
        bridge.register_handler(handle_ping, [TrinityIntent.PING])
        bridge.register_handler(handle_execute_plan, [TrinityIntent.EXECUTE_PLAN])

        # v101.0: Model lifecycle handlers (Trinity Loop - closes the loop!)
        bridge.register_handler(handle_model_ready, [TrinityIntent.MODEL_READY])
        bridge.register_handler(handle_model_rollback, [TrinityIntent.MODEL_ROLLBACK])
        bridge.register_handler(handle_update_model_routing, [TrinityIntent.UPDATE_MODEL_ROUTING])
        logger.info("[Trinity] Model lifecycle handlers registered (MODEL_READY, MODEL_ROLLBACK, UPDATE_MODEL_ROUTING)")

        # v77.2: Register Coding Council evolution handlers
        try:
            from backend.core.coding_council.integration import register_evolution_handlers
            register_evolution_handlers(bridge)
            logger.info("[Trinity] Coding Council evolution handlers registered")
        except ImportError:
            try:
                from core.coding_council.integration import register_evolution_handlers
                register_evolution_handlers(bridge)
                logger.info("[Trinity] Coding Council evolution handlers registered")
            except ImportError:
                logger.debug("[Trinity] Coding Council integration not available")

        _handlers_registered = True
        logger.info("[Trinity] All command handlers registered")
        return True

    except Exception as e:
        logger.error(f"[Trinity] Failed to register handlers: {e}")
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "register_trinity_handlers",
    "handle_start_surveillance",
    "handle_stop_surveillance",
    "handle_bring_back_window",
    "handle_exile_window",
    "handle_freeze_app",
    "handle_thaw_app",
    "handle_create_ghost_display",
    "handle_ping",
    "handle_execute_plan",
    # v101.0: Model lifecycle handlers
    "handle_model_ready",
    "handle_model_rollback",
    "handle_update_model_routing",
]
