#!/usr/bin/env python3
"""
Simple Unlock Handler
====================

Direct unlock functionality without complex state management.
Just unlock the screen when asked.

Now integrated with AdvancedAsyncPipeline for non-blocking operations.
"""

import asyncio
import json
import logging
import subprocess
from typing import Any, Dict, List, Tuple

# Import async pipeline
from core.async_pipeline import get_async_pipeline
from core.transport_handlers import (
    applescript_handler,
    http_rest_handler,
    system_api_handler,
    unified_websocket_handler,
)

# Import transport layer
from core.transport_manager import TransportMethod, get_transport_manager

logger = logging.getLogger(__name__)

# Global instances
_pipeline = None
_transport_manager = None
_owner_name_cache = None  # Cache for owner's name


async def _get_owner_name():
    """
    Get the device owner's name dynamically from the database.
    Caches the result for performance.

    Returns:
        str: Owner's name (first name only for natural speech)
    """
    global _owner_name_cache

    if _owner_name_cache is not None:
        return _owner_name_cache

    try:
        from intelligence.learning_database import get_learning_database

        db = await get_learning_database()
        profiles = await db.get_all_speaker_profiles()

        # Find the primary user (owner)
        for profile in profiles:
            if profile.get('is_primary_user'):
                full_name = profile.get('speaker_name', 'User')
                # Extract first name for natural speech
                first_name = full_name.split()[0] if ' ' in full_name else full_name
                _owner_name_cache = first_name
                logger.info(f"✅ Retrieved owner name from database: {first_name}")
                return first_name

        # No owner found - return generic
        logger.warning("⚠️ No primary user found in database")
        return "User"

    except Exception as e:
        logger.error(f"Error retrieving owner name: {e}")
        return "User"


async def _preload_owner_name():
    """
    Pre-load owner name cache at startup for faster first unlock.
    Fire-and-forget - does not block startup.
    """
    global _owner_name_cache
    if _owner_name_cache is None:
        await _get_owner_name()


# Pre-cached speaker verification service for fast path
_speaker_service_cache = None
_speaker_service_lock = asyncio.Lock()


async def _get_cached_speaker_service():
    """
    Get or initialize speaker verification service with caching.
    Uses lock to prevent multiple concurrent initializations.
    """
    global _speaker_service_cache

    if _speaker_service_cache is not None:
        return _speaker_service_cache

    async with _speaker_service_lock:
        # Double-check after acquiring lock
        if _speaker_service_cache is not None:
            return _speaker_service_cache

        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            _speaker_service_cache = await get_speaker_verification_service()
            logger.info("✅ Speaker verification service cached for fast path")
            return _speaker_service_cache
        except Exception as e:
            logger.error(f"Failed to cache speaker service: {e}")
            return None


async def preload_unlock_dependencies():
    """
    Pre-load all unlock dependencies at startup for faster first unlock.
    Call this from main.py during startup.
    """
    logger.info("🚀 Pre-loading unlock dependencies...")

    # Run all preloads in parallel
    await asyncio.gather(
        _preload_owner_name(),
        _get_cached_speaker_service(),
        _get_transport_manager(),
        return_exceptions=True  # Don't fail if one preload fails
    )

    logger.info("✅ Unlock dependencies pre-loaded")


async def _get_transport_manager():
    """Get or create and initialize the transport manager"""
    global _transport_manager
    if _transport_manager is None:
        _transport_manager = get_transport_manager()

        # Register all transport handlers
        _transport_manager.register_handler(TransportMethod.APPLESCRIPT, applescript_handler)
        _transport_manager.register_handler(TransportMethod.HTTP_REST, http_rest_handler)
        _transport_manager.register_handler(
            TransportMethod.UNIFIED_WEBSOCKET, unified_websocket_handler
        )
        _transport_manager.register_handler(TransportMethod.SYSTEM_API, system_api_handler)

        # Initialize (starts health monitoring)
        await _transport_manager.initialize()

        logger.info("[TRANSPORT] ✅ Transport manager initialized with all handlers")

    return _transport_manager


def _get_pipeline():
    """Get or create the async pipeline instance"""
    global _pipeline
    if _pipeline is None:
        _pipeline = get_async_pipeline()
        _register_pipeline_stages()
    return _pipeline


def _register_pipeline_stages():
    """Register simple unlock handler pipeline stages"""
    global _pipeline

    _pipeline.register_stage(
        "unlock_caffeinate", _caffeinate_async, timeout=3.0, retry_count=1, required=False
    )
    _pipeline.register_stage(
        "unlock_applescript", _applescript_unlock_async, timeout=15.0, retry_count=1, required=True
    )
    logger.info("✅ Simple unlock handler pipeline stages registered")


async def _caffeinate_async(context):
    """Async pipeline handler for waking display"""
    from api.jarvis_voice_api import async_subprocess_run

    try:
        # Ensure command is properly formatted
        caffeinate_cmd = ["caffeinate", "-u", "-t", "1"]
        stdout, stderr, returncode = await async_subprocess_run(caffeinate_cmd, timeout=2.0)
        context.metadata["caffeinate_success"] = returncode == 0
        context.metadata["success"] = True
        logger.debug(f"Caffeinate result: returncode={returncode}")
    except Exception as e:
        logger.warning(f"Caffeinate failed: {e}")
        context.metadata["caffeinate_success"] = False
        context.metadata["error"] = str(e)
        # Don't fail the entire pipeline just because caffeinate failed
        context.metadata["success"] = True  # Allow continuation


async def _applescript_unlock_async(context):
    """Async pipeline handler for AppleScript unlock"""
    from api.jarvis_voice_api import async_osascript

    script = context.metadata.get("script", "")
    timeout = context.metadata.get("timeout", 10.0)

    try:
        stdout, stderr, returncode = await async_osascript(script, timeout=timeout)
        context.metadata["returncode"] = returncode
        context.metadata["stdout"] = stdout.decode() if stdout else ""
        context.metadata["stderr"] = stderr.decode() if stderr else ""
        context.metadata["success"] = returncode == 0
    except Exception as e:
        context.metadata["success"] = False
        context.metadata["error"] = str(e)


def _escape_password_for_applescript(password: str) -> str:
    """Escape special characters in password for AppleScript"""
    escaped = password.replace("\\", "\\\\")  # Escape backslashes
    escaped = escaped.replace('"', '\\"')  # Escape double quotes
    return escaped


async def _perform_direct_unlock(password: str) -> bool:
    """
    Perform direct screen unlock using SecurePasswordTyper with voice biometric integration

    Args:
        password: The user's Mac password from keychain

    Returns:
        bool: True if unlock succeeded, False otherwise
    """
    caffeinate_process = None
    try:
        logger.info("[DIRECT UNLOCK] Starting secure unlock sequence with biometric integration")

        # CRITICAL: Keep screen awake during entire unlock process
        # This prevents screen from going black while JARVIS processes audio
        logger.info("[DIRECT UNLOCK] Starting caffeinate to prevent screen sleep...")
        caffeinate_process = await asyncio.create_subprocess_exec(
            "caffeinate", "-d", "-u",  # -d = prevent display sleep, -u = wake display
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        # Give caffeinate a moment to activate
        await asyncio.sleep(0.3)

        # Import secure password typer
        from voice_unlock.secure_password_typer import type_password_securely

        # Type password using secure, native Core Graphics method
        # This method:
        # - Uses CGEventCreateKeyboardEvent (native macOS API)
        # - Never exposes password in logs or process list
        # - Implements adaptive timing based on system load
        # - Has AppleScript fallback if Core Graphics fails
        # - Automatically wakes screen and submits password
        logger.info(f"[DIRECT UNLOCK] Using SecurePasswordTyper ({len(password)} characters)")

        success = await type_password_securely(
            password=password,
            submit=True,  # Automatically press Return
            randomize_timing=True  # Human-like typing with adaptive timing
        )

        if success:
            # Wait for unlock to complete
            await asyncio.sleep(1.5)

            # Verify unlock by checking screen state
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked

                is_locked = is_screen_locked()

                if not is_locked:
                    logger.info("[DIRECT UNLOCK] ✅ Unlock verified successful with biometric authentication")
                    return True
                else:
                    logger.warning("[DIRECT UNLOCK] ⚠️ Screen still locked after attempt")
                    return False
            except Exception as verify_error:
                # If we can't verify, assume success since typing succeeded
                logger.info(f"[DIRECT UNLOCK] ✅ Unlock completed (verification unavailable: {verify_error})")
                return True
        else:
            logger.error("[DIRECT UNLOCK] ❌ SecurePasswordTyper failed")
            return False

    except Exception as e:
        logger.error(f"[DIRECT UNLOCK] ❌ Error during unlock: {e}", exc_info=True)
        return False
    finally:
        # Always terminate caffeinate process to restore normal power management
        if caffeinate_process:
            try:
                caffeinate_process.terminate()
                await asyncio.sleep(0.1)  # Give it a moment to terminate gracefully
                if caffeinate_process.returncode is None:
                    caffeinate_process.kill()  # Force kill if still running
                logger.info("[DIRECT UNLOCK] Caffeinate process terminated")
            except Exception as cleanup_error:
                logger.warning(f"[DIRECT UNLOCK] Failed to cleanup caffeinate: {cleanup_error}")


async def handle_unlock_command(command: str, jarvis_instance=None) -> Dict[str, Any]:
    """
    Enhanced unlock/lock screen command handler with dynamic response generation,
    advanced command parsing, and intelligent fallback mechanisms.

    Features:
    - Dynamic response generation using AI
    - Advanced command parsing with context awareness
    - Multiple unlock/lock methods with intelligent fallback
    - Context-aware error handling and user guidance
    - Performance monitoring and adaptive behavior
    """

    # Initialize response generator for dynamic responses
    try:
        from voice.dynamic_response_generator import get_response_generator

        response_gen = get_response_generator()
    except ImportError:
        response_gen = None
        logger.warning("Dynamic response generator not available, using fallback responses")

    # Advanced command parsing and intent detection
    command_analysis = await _analyze_unlock_command(command, jarvis_instance)

    if not command_analysis["is_valid"]:
        return await _generate_command_error_response(command_analysis, response_gen)

    action = command_analysis["action"]
    context = command_analysis["context"]

    # Execute the action with multiple fallback methods FIRST
    # This will set the verified_speaker_name in the context
    result = await _execute_screen_action(action, context, jarvis_instance)

    # Generate dynamic response AFTER verification with speaker name from context
    response_text = await _generate_contextual_response(action, context, response_gen)

    # Enhance result with dynamic response and context
    return await _enhance_result_with_context(result, response_text, action, context, response_gen)


async def _analyze_unlock_command(command: str, jarvis_instance=None) -> Dict[str, Any]:
    """
    Advanced command analysis - SPEED OPTIMIZED.
    Fast pattern matching without heavy regex.
    """

    command_lower = command.lower().strip()

    # FAST PATH: Simple keyword matching (no regex overhead)
    is_unlock = "unlock" in command_lower
    is_lock = "lock" in command_lower and not is_unlock

    # Determine action
    if is_unlock:
        action = "unlock_screen"
    elif is_lock:
        action = "lock_screen"
    else:
        return {
            "is_valid": False,
            "action": None,
            "context": {
                "confidence": 0.0,
                "reason": "no_valid_intent_detected",
                "suggestions": ["Try 'unlock my screen' or 'lock my screen'"],
            },
        }

    # Fast context analysis
    context = await _analyze_command_context(command, action, jarvis_instance)

    return {"is_valid": True, "action": action, "context": context}


async def _analyze_command_context(
    command: str, action: str, jarvis_instance=None
) -> Dict[str, Any]:
    """Analyze command context for urgency, politeness, and user state - SPEED OPTIMIZED."""

    command_lower = command.lower()

    # FAST PATH: Quick single-pass analysis
    is_urgent = "now" in command_lower or "quickly" in command_lower or "fast" in command_lower
    is_polite = "please" in command_lower

    # Minimal context for speed
    return {
        "urgency": "high" if is_urgent else "normal",
        "politeness": "polite" if is_polite else "direct",
        "user_state": "normal",  # Skip complex state detection for speed
        "time_context": "day",  # Skip time calculation for speed
        "screen_state": "unknown",  # Skip screen check for speed
        "command_original": command,
        "confidence": 0.95,
    }


async def _generate_contextual_response(action: str, context: Dict[str, Any], response_gen) -> str:
    """Generate dynamic, contextual response with speaker name if verified - SPEED OPTIMIZED."""

    # Get speaker name if verified
    speaker_name = context.get("verified_speaker_name", "Sir")

    # FAST PATH: Use pre-generated responses for speed (no dynamic generation overhead)
    # This eliminates the response_gen overhead while keeping contextual awareness

    if action == "unlock_screen" and context.get("screen_state") == "unlocked":
        return f"Your screen is already unlocked, {speaker_name}."

    # Use fast fallback responses with speaker name
    if context.get("urgency") == "high":
        return f"Right away, {speaker_name}! " + (
            "Unlocking your screen now."
            if action == "unlock_screen"
            else "Locking your screen now."
        )

    # Default fast responses with speaker name
    return f"Of course, {speaker_name}. " + (
        "Unlocking for you." if action == "unlock_screen" else "Locking for you."
    )


def _generate_fallback_response(action: str, context: Dict[str, Any]) -> str:
    """Generate intelligent fallback responses based on context."""

    responses = {
        "unlock_screen": {
            "unlocked": [
                "Your screen is already unlocked.",
                "The screen is already accessible.",
                "No need to unlock - you're already in.",
            ],
            "locked": {
                "urgent": [
                    "Right away! Unlocking your screen now.",
                    "Immediately unlocking for you.",
                    "Quickly accessing your screen.",
                ],
                "polite": [
                    "Of course, I'll unlock that for you.",
                    "Certainly, unlocking your screen.",
                    "I'd be happy to unlock that for you.",
                ],
                "tired": [
                    "I'll unlock your screen so you can rest.",
                    "Let me get that unlocked for you.",
                    "Unlocking now - you can relax.",
                ],
                "normal": [
                    "Unlocking your screen.",
                    "I'll unlock that for you.",
                    "Accessing your screen now.",
                ],
            },
        },
        "lock_screen": {
            "urgent": [
                "Securing your screen immediately.",
                "Right away - locking now.",
                "Quickly securing your screen.",
            ],
            "polite": [
                "Of course, I'll lock your screen.",
                "Certainly, securing that for you.",
                "I'd be happy to lock that for you.",
            ],
            "leaving": [
                "I'll lock your screen before you go.",
                "Securing your screen for your departure.",
                "Locking up as you requested.",
            ],
            "normal": [
                "Locking your screen.",
                "I'll secure that for you.",
                "Protecting your screen now.",
            ],
        },
    }

    # Select appropriate response category
    if action == "unlock_screen":
        if context["screen_state"] == "unlocked":
            category = "unlocked"
        else:
            if context["urgency"] == "high":
                category = "urgent"
            elif context["politeness"] == "polite":
                category = "polite"
            elif context["user_state"] == "tired":
                category = "tired"
            else:
                category = "normal"
    else:  # lock_screen
        if context["urgency"] == "high":
            category = "urgent"
        elif context["politeness"] == "polite":
            category = "polite"
        elif context["user_state"] == "leaving":
            category = "leaving"
        else:
            category = "normal"

    # Get response options and select one
    import random

    response_options = responses[action][category]
    base_response = random.choice(response_options)  # nosec B311 # UI responses, not cryptographic

    # Add time-based context occasionally
    if random.random() < 0.3:  # nosec B311 # UI variation, not cryptographic
        time_additions = {
            "morning": "Good morning! ",
            "afternoon": "Good afternoon! ",
            "evening": "Good evening! ",
            "night": "Good evening! ",
        }
        base_response = time_additions.get(context["time_context"], "") + base_response

    return base_response


def _generate_command_suggestions(command: str) -> List[str]:
    """Generate helpful suggestions for unclear commands."""

    suggestions = [
        "Try saying 'unlock my screen' or 'lock my screen'",
        "You can say 'unlock' or 'lock' followed by 'screen'",
        "Commands like 'unlock my screen' or 'lock my screen' work well",
        "Try 'unlock screen' or 'lock screen' for screen control",
    ]

    # Check if command contains screen-related words
    if "screen" in command.lower():
        suggestions.extend(
            [
                "For screen control, try 'unlock screen' or 'lock screen'",
                "Screen commands: 'unlock my screen' or 'lock my screen'",
            ]
        )

    return suggestions[:3]  # Return top 3 suggestions


async def _execute_screen_action(
    action: str, context: Dict[str, Any], jarvis_instance=None
) -> Dict[str, Any]:
    """
    Execute screen action using advanced transport manager.

    Automatically selects the best transport method based on:
    - Real-time health monitoring
    - Success rate history
    - Latency metrics
    - Current availability
    """

    logger.info(f"[TRANSPORT-EXEC] Executing {action}")
    logger.debug(f"[TRANSPORT-EXEC] Context: {context}")

    # Pass jarvis_instance to context for audio/speaker extraction
    if jarvis_instance:
        context["jarvis_instance"] = jarvis_instance

    # VOICE BIOMETRIC VERIFICATION (for unlock actions only)
    if action == "unlock_screen":
        # Extract audio data from jarvis_instance if available
        audio_data = None
        speaker_name = None

        if jarvis_instance:
            if hasattr(jarvis_instance, "last_audio_data"):
                audio_data = jarvis_instance.last_audio_data
                logger.debug(
                    f"✅ Audio data extracted: {len(audio_data) if audio_data else 0} bytes"
                )

            if hasattr(jarvis_instance, "last_speaker_name"):
                speaker_name = jarvis_instance.last_speaker_name
                logger.debug(f"✅ Speaker name extracted: {speaker_name}")

        logger.info(
            f"🎤 Audio data status: {len(audio_data) if audio_data else 0} bytes, speaker: {speaker_name}"
        )

        if audio_data:
            # Verify speaker using voice biometrics with TIMEOUT PROTECTION
            try:
                # Step 1: Announce verification start
                logger.info("🎤 Starting voice biometric verification...")
                context["status_message"] = "Verifying your voice biometrics..."

                # FAST PATH: Use cached speaker service (pre-loaded at startup)
                speaker_service = await _get_cached_speaker_service()
                if speaker_service is None:
                    # Fallback: Initialize service if cache failed
                    try:
                        from voice.speaker_verification_service import get_speaker_verification_service
                        speaker_service = await asyncio.wait_for(
                            get_speaker_verification_service(),
                            timeout=10.0  # 10 second timeout for service initialization
                        )
                    except asyncio.TimeoutError:
                        logger.error("⏱️ Speaker service initialization timed out")
                        return {
                            "success": False,
                            "error": "verification_timeout",
                            "message": "Voice verification service initialization timed out. Please try again.",
                        }

                sample_count = sum(
                    profile.get("total_samples", 0)
                    for profile in speaker_service.speaker_profiles.values()
                )
                logger.info(
                    f"🔐 Speaker service ready: {len(speaker_service.speaker_profiles)} profiles, "
                    f"{sample_count} voice samples loaded"
                )

                # Step 2: Verify speaker using voice biometrics WITH TIMEOUT
                logger.info("🔐 Analyzing voice pattern against biometric database...")
                context["status_message"] = "Analyzing voice pattern..."

                try:
                    verification_result = await asyncio.wait_for(
                        speaker_service.verify_speaker(audio_data, speaker_name),
                        timeout=30.0  # 30 second timeout for verification (includes embedding extraction)
                    )
                except asyncio.TimeoutError:
                    logger.error("⏱️ Voice verification timed out after 30 seconds")
                    return {
                        "success": False,
                        "error": "verification_timeout",
                        "message": "Voice verification took too long. Please try again with clearer audio.",
                    }

                logger.info(f"🎤 Verification result: {verification_result}")

                speaker_name = verification_result["speaker_name"]
                is_verified = verification_result["verified"]
                confidence = verification_result["confidence"]
                is_owner = verification_result["is_owner"]

                # Step 3: Announce verification result
                logger.info(
                    f"🔐 Speaker verification: {speaker_name} - "
                    f"{'✅ VERIFIED' if is_verified else '❌ FAILED'} "
                    f"(confidence: {confidence:.1%}, owner: {is_owner})"
                )

                if not is_verified:
                    logger.warning(
                        f"🚫 Voice verification failed for {speaker_name} - unlock denied"
                    )
                    context["status_message"] = "Voice verification failed - access denied"
                    return {
                        "success": False,
                        "error": "voice_verification_failed",
                        "message": (
                            f"I'm sorry, I couldn't verify your voice biometrics. "
                            f"Confidence was {confidence:.0%}, but I need at least 75% to unlock your screen for security."
                        ),
                    }

                # Check if speaker is the device owner
                if not is_owner:
                    logger.warning(f"🚫 Non-owner {speaker_name} attempted unlock - denied")
                    context["status_message"] = "Non-owner detected - access denied"
                    # Get owner name dynamically
                    owner_name = await _get_owner_name()
                    return {
                        "success": False,
                        "error": "not_owner",
                        "message": f"Voice verified as {speaker_name}, but only the device owner {owner_name} can unlock the screen.",
                    }

                # Store verified speaker name in context for personalized response
                context["verified_speaker_name"] = speaker_name
                context["verification_confidence"] = confidence

                logger.info(
                    f"✅ Voice verification passed for owner: {speaker_name} ({confidence:.1%})"
                )
                context["status_message"] = (
                    f"Identity verified: {speaker_name} ({confidence:.0%} confidence)"
                )
                # Store intermediate message for JARVIS to speak
                context["verification_message"] = (
                    f"Identity confirmed, {speaker_name}. "
                    f"Initiating screen unlock sequence now."
                )

            except Exception as e:
                logger.error(f"Voice verification error: {e}", exc_info=True)
                # Voice verification failed - deny unlock
                return {
                    "success": False,
                    "error": "voice_verification_error",
                    "message": f"Voice verification system error: {str(e)}",
                }
        else:
            logger.info("📝 No audio data - bypassing voice verification (text command)")

    # Get transport manager
    transport = await _get_transport_manager()

    # Execute using smart transport selection
    result = await transport.execute(action, context)

    # Log result with metrics
    if result.get("success"):
        logger.info(
            f"[TRANSPORT-EXEC] ✅ {action} succeeded via {result.get('transport_method')} "
            f"({result.get('latency_ms', 0):.1f}ms)"
        )
    else:
        logger.warning(
            f"[TRANSPORT-EXEC] ❌ {action} failed: {result.get('error')} "
            f"(attempted: {result.get('attempted_methods', [])})"
        )

    return result


async def _try_websocket_method(action: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Try WebSocket daemon method with enhanced error handling."""

    async with websockets.connect(VOICE_UNLOCK_WS_URL, ping_interval=20) as ws:
        cmd_msg = {
            "type": "command",
            "command": action,
            "parameters": {
                "source": "jarvis_enhanced_command",
                "authenticated": True,
                "context": context,
            },
        }

        await ws.send(json.dumps(cmd_msg))
        logger.info(f"[ENHANCED UNLOCK] Sent {action} command via WebSocket")

        # Dynamic timeout based on urgency
        timeout = 15.0 if context["urgency"] == "high" else 30.0

        try:
            response = await asyncio.wait_for(ws.recv(), timeout=timeout)
            result = json.loads(response)

            return {
                "success": result.get("success", False),
                "message": result.get("message", ""),
                "data": result.get("data", {}),
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": f"Operation timed out after {timeout} seconds",
                "error_type": "timeout",
            }


async def _try_lock_methods(context: Dict[str, Any]) -> Dict[str, Any]:
    """Try multiple lock methods with intelligent fallback."""

    methods = [
        ("macos_controller", _try_macos_controller_lock),
        ("screensaver", _try_screensaver_lock),
        ("system_command", _try_system_lock_command),
    ]

    for method_name, method_func in methods:
        try:
            success, message = await method_func(context)
            if success:
                return {"success": True, "message": message, "method": method_name}
        except Exception as e:
            logger.debug(f"Lock method {method_name} failed: {e}")

    return {
        "success": False,
        "message": "All lock methods failed",
        "error_type": "all_methods_failed",
    }


async def _try_unlock_methods(context: Dict[str, Any]) -> Dict[str, Any]:
    """Try multiple unlock methods with intelligent fallback."""

    # Check if screen is already unlocked
    try:
        from voice_unlock.objc.server.screen_lock_detector import is_screen_locked

        if not is_screen_locked():
            return {
                "success": True,
                "message": "Screen was already unlocked",
                "method": "already_unlocked",
            }
    except:
        pass

    # Extract audio data from jarvis_instance if available
    # This enables voice verification for unlock commands
    jarvis_instance = context.get("jarvis_instance")
    if jarvis_instance:
        # Try to get audio data from recent voice interaction
        if hasattr(jarvis_instance, "last_audio_data"):
            context["audio_data"] = jarvis_instance.last_audio_data
            logger.debug("✅ Audio data extracted for voice verification")

        # Try to get speaker name from recent identification
        if hasattr(jarvis_instance, "last_speaker_name"):
            context["speaker_name"] = jarvis_instance.last_speaker_name
            logger.debug(f"✅ Speaker name extracted: {jarvis_instance.last_speaker_name}")

    # If no audio data (text command), bypass voice verification
    # User is already authenticated by being logged into the system
    if not context.get("audio_data"):
        context["bypass_voice_verification"] = True
        logger.info(
            "📝 Text-based unlock command - bypassing voice verification (user already authenticated)"
        )

    methods = [
        ("keychain_direct", _try_keychain_unlock),
        ("manual_unlock", _try_manual_unlock_fallback),
    ]

    for method_name, method_func in methods:
        try:
            success, message = await method_func(context)
            if success:
                return {"success": True, "message": message, "method": method_name}
        except Exception as e:
            logger.debug(f"Unlock method {method_name} failed: {e}")

    return {
        "success": False,
        "message": "All unlock methods failed",
        "error_type": "all_methods_failed",
    }


async def _try_macos_controller_lock(context: Dict[str, Any]) -> Tuple[bool, str]:
    """Try AppleScript Command+Control+Q lock method (fastest and most reliable)."""
    try:
        script = """
        tell application "System Events"
            keystroke "q" using {command down, control down}
        end tell
        """

        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await process.communicate()

        if process.returncode == 0:
            logger.info("[LOCK] ✅ Screen locked via AppleScript Command+Control+Q")
            return True, "Screen locked"
        else:
            return False, "AppleScript lock failed"

    except Exception as e:
        logger.error(f"[LOCK] AppleScript lock error: {e}")
        return False, f"Lock error: {str(e)}"


async def _try_screensaver_lock(context: Dict[str, Any]) -> Tuple[bool, str]:
    """Try screensaver lock method (fallback)."""
    try:
        subprocess.run(["open", "-a", "ScreenSaverEngine"], check=True)
        return True, "Screensaver started"
    except Exception as e:
        return False, f"Screensaver failed: {str(e)}"


async def _try_system_lock_command(context: Dict[str, Any]) -> Tuple[bool, str]:
    """Try system command lock method (fallback)."""
    try:
        subprocess.run(["pmset", "displaysleepnow"], check=True)
        return True, "Display sleep activated"
    except Exception as e:
        return False, f"System lock failed: {str(e)}"


async def _try_keychain_unlock(context: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Try keychain-based unlock method.

    NOTE: Voice verification is now handled in _execute_screen_action().
    This function focuses solely on password retrieval and unlock execution.

    Security Flow:
    1. Get verified speaker from context (already verified upstream)
    2. Retrieve password from keychain
    3. Perform unlock
    """
    # FAST PATH: Speaker already verified in _execute_screen_action()
    # Check if verification was already done
    verified_speaker = context.get("verified_speaker_name")

    if not verified_speaker:
        # No verification done yet - get owner name for text commands
        logger.info("📝 No prior verification - using database owner for text command")
        verified_speaker = await _get_owner_name()
        context["verified_speaker_name"] = verified_speaker

    # Use enhanced Keychain integration for actual unlock
    try:
        from macos_keychain_unlock import MacOSKeychainUnlock

        unlock_service = MacOSKeychainUnlock()
        # Get verified speaker name from context, or fall back to database owner
        verified_speaker = context.get("verified_speaker_name")
        if not verified_speaker:
            verified_speaker = await _get_owner_name()
        confidence = context.get("verification_confidence", 0.0)

        # Announce unlock initiation
        logger.info(f"🔓 Initiating screen unlock for {verified_speaker}...")
        context["status_message"] = "Retrieving secure credentials..."

        # Perform actual screen unlock with Keychain password
        unlock_result = await unlock_service.unlock_screen(verified_speaker=verified_speaker)

        if unlock_result["success"]:
            confidence_msg = f" (verified at {confidence:.0%} confidence)" if confidence > 0 else ""
            logger.info(f"🔓 Screen unlocked successfully for {verified_speaker}{confidence_msg}")
            context["status_message"] = f"Screen unlocked for {verified_speaker}"

            # Build complete response with verification message
            verification_msg = context.get("verification_message", "")
            if verification_msg:
                full_message = f"{verification_msg} Welcome back, {verified_speaker}. Your screen is now unlocked."
            else:
                full_message = f"Welcome back, {verified_speaker}. Your screen is now unlocked{confidence_msg}."

            return (True, full_message)
        else:
            # Check if setup is required
            if unlock_result.get("action") == "setup_required":
                logger.warning("⚠️ Keychain password not configured")
                context["status_message"] = "Setup required - keychain not configured"
                return (
                    False,
                    "I couldn't find your screen unlock password in the keychain. "
                    "Please run the setup script to configure secure unlock.",
                )
            else:
                logger.error(f"❌ Unlock failed: {unlock_result['message']}")
                context["status_message"] = f"Unlock failed: {unlock_result['message']}"
                return (
                    False,
                    f"I verified your identity, but couldn't unlock the screen: {unlock_result['message']}",
                )

    except ImportError:
        logger.error("MacOS Keychain integration not available")
        # Fallback to old method
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "com.jarvis.voiceunlock",
                "-a",
                "unlock_token",
                "-w",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            password = result.stdout.strip()
            unlock_result = await _perform_direct_unlock(password)

            if unlock_result:
                return True, f"Screen unlocked by {verified_speaker or 'verified user'}"

        return False, "Password not found in keychain"


async def _try_manual_unlock_fallback(context: Dict[str, Any]) -> Tuple[bool, str]:
    """Try manual unlock fallback method."""
    # This would trigger a manual unlock process
    return False, "Manual unlock not implemented"


async def _generate_command_error_response(
    analysis: Dict[str, Any], response_gen
) -> Dict[str, Any]:
    """Generate helpful error response for invalid commands."""

    if response_gen:
        try:
            error_response = response_gen.get_error_message(
                "invalid_command", "I didn't understand that screen command"
            )
        except:
            error_response = "I didn't understand that screen command."
    else:
        error_response = "I didn't understand that screen command."

    return {
        "success": False,
        "response": error_response,
        "type": "command_error",
        "suggestions": analysis["context"]["suggestions"],
        "confidence": analysis["context"]["confidence"],
    }


async def _enhance_result_with_context(
    result: Dict[str, Any], response_text: str, action: str, context: Dict[str, Any], response_gen
) -> Dict[str, Any]:
    """Enhance result with dynamic response and contextual information."""

    # Add dynamic response with voice verification message if available
    if action == "unlock_screen" and context.get("verification_message") and result.get("success"):
        # Include voice verification transparency message
        verified_speaker = context.get("verified_speaker_name")
        if not verified_speaker:
            verified_speaker = await _get_owner_name()
        context.get("verification_confidence", 0)

        # Build complete response with verification message
        verification_msg = context["verification_message"]
        full_message = f"{verification_msg} {response_text}"
        result["response"] = full_message

        logger.info(f"[RESPONSE] Including verification message for {verified_speaker}")
    else:
        result["response"] = response_text

    # CRITICAL: Add action field for fast path compatibility
    result["action"] = action

    # Add type for voice_unlock compatibility
    if "type" not in result:
        result["type"] = "voice_unlock" if action == "unlock_screen" else "screen_lock"

    # Add contextual metadata
    result["enhanced_context"] = {
        "action": action,
        "urgency": context["urgency"],
        "politeness": context["politeness"],
        "user_state": context["user_state"],
        "time_context": context["time_context"],
        "execution_method": result.get("method", "unknown"),
        "voice_verified": bool(context.get("verified_speaker_name")),
        "verification_confidence": context.get("verification_confidence", 0),
    }

    # Add performance metrics
    result["performance"] = {
        "command_confidence": context["confidence"],
        "response_type": "dynamic" if response_gen else "fallback",
    }

    # Add helpful information for failures
    if not result["success"]:
        result["troubleshooting"] = await _generate_troubleshooting_info(action, result, context)

    return result


async def _generate_troubleshooting_info(
    action: str, result: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, Any]:
    """Generate helpful troubleshooting information for failed operations."""

    troubleshooting = {
        "action": action,
        "error_type": result.get("error_type", "unknown"),
        "suggestions": [],
    }

    if action == "unlock_screen":
        troubleshooting["suggestions"] = [
            "Make sure your password is stored in the keychain",
            "Try running: ./backend/voice_unlock/enable_screen_unlock.sh",
            "Check if the Voice Unlock daemon is running",
            "Verify your screen is actually locked",
        ]
    else:  # lock_screen
        troubleshooting["suggestions"] = [
            "Try using Control+Command+Q manually",
            "Check if Screen Time restrictions are enabled",
            "Verify you have permission to lock the screen",
            "Try restarting the system control services",
        ]

    # Add context-specific suggestions
    if context["urgency"] == "high":
        troubleshooting["suggestions"].insert(0, "For urgent requests, try manual methods first")

    return troubleshooting
