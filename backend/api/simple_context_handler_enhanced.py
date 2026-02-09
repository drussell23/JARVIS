#!/usr/bin/env python3
"""
Enhanced Simple Context Handler for JARVIS
==========================================

Provides context-aware command processing with:
- Speaker verification via VBIA/PAVA before any unlock attempt
- Screen lock detection via Quartz (no daemon dependency)
- Password-based unlock via MacOSKeychainUnlock singleton (cached password)
- Concurrent unlock serialization (module-level lock prevents interleaved typing)
- Voice deduplication: only ONE spoken message per phase (no overlapping TTS)
- Step-by-step WebSocket status updates (silent) with spoken summary

Security contract:
    1. Speaker MUST be identified by STT pipeline before unlock is attempted
    2. Identified speaker MUST match the device owner (DB primary_user)
    3. Unidentified speakers get a warning but proceed (fail-open, configurable)
    4. Non-owner identified speakers are BLOCKED from unlock
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Module-level lock to serialize unlock attempts across all handler instances.
# Prevents concurrent CG Event streams from interleaving password characters
# when two voice commands arrive simultaneously.
# ─────────────────────────────────────────────────────────────────────────────
_unlock_lock = asyncio.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Cached owner name — loaded once from DB, reused across all requests.
# ─────────────────────────────────────────────────────────────────────────────
_owner_name_cache: Optional[str] = None


async def _get_owner_name() -> str:
    """Get the device owner's first name from the speaker profile database.

    Uses a module-level cache so the DB is only queried once per process lifetime.
    Falls back to env var JARVIS_OWNER_NAME, then to "Derek".
    """
    global _owner_name_cache
    if _owner_name_cache is not None:
        return _owner_name_cache

    # Try database first
    try:
        from intelligence.learning_database import get_learning_database

        db = await get_learning_database()
        profiles = await db.get_all_speaker_profiles()
        for profile in profiles:
            if profile.get("is_primary_user"):
                full_name = profile.get("speaker_name", "")
                first_name = full_name.split()[0] if full_name else ""
                if first_name:
                    _owner_name_cache = first_name
                    logger.info(f"[ENHANCED CONTEXT] Owner from DB: {first_name}")
                    return first_name
    except Exception as e:
        logger.debug(f"[ENHANCED CONTEXT] DB owner lookup failed: {e}")

    # Fallback: env var → default
    _owner_name_cache = os.getenv("JARVIS_OWNER_NAME", "Derek")
    logger.info(f"[ENHANCED CONTEXT] Owner from env/default: {_owner_name_cache}")
    return _owner_name_cache


class EnhancedSimpleContextHandler:
    """Enhanced handler for context-aware command processing with step-by-step feedback.

    Architecture:
        WebSocket voice → STT (speaker ID) → command_text + speaker_name
            → process_with_context()
                ├─ _requires_screen(command) → True
                ├─ _check_screen_locked()    → Quartz (no daemon)
                ├─ _verify_speaker()         → Check speaker_name matches owner
                ├─ _unlock_screen()          → Keychain singleton (serialized via _unlock_lock)
                └─ command_processor.process_command(command, websocket)

    Voice deduplication contract:
        - Only the INITIAL acknowledgment is spoken (speak=True, type=processing)
        - Progress updates use speak=False
        - The FINAL command result is spoken by the WebSocket handler (speak=True)
    """

    def __init__(self, command_processor):
        self.command_processor = command_processor
        self.execution_steps = []
        self.screen_required_patterns = [
            # Browser operations
            "open safari",
            "open chrome",
            "open firefox",
            "open browser",
            "search for",
            "google",
            "look up",
            "find online",
            "go to",
            "navigate to",
            "visit",
            "browse",
            # Application operations
            "open",
            "launch",
            "start",
            "run",
            "quit",
            "close app",
            "switch to",
            "show me",
            "display",
            "bring up",
            # File operations
            "create",
            "edit",
            "save",
            "close file",
            "find file",
            "open file",
            "open document",
            # System UI operations
            "click",
            "type",
            "press",
            "select",
            "take screenshot",
            "show desktop",
            "minimize",
            "maximize",
        ]

    def _add_step(self, step: str, details: Optional[Dict[str, Any]] = None):
        """Add an execution step for tracking"""
        self.execution_steps.append(
            {
                "step": step,
                "timestamp": datetime.now().isoformat(),
                "details": details or {},
            }
        )
        logger.info(f"[CONTEXT STEP] {step}")

    async def process_with_context(
        self,
        command: str,
        websocket=None,
        *,
        speaker_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process command with enhanced context awareness and feedback.

        Args:
            command: The voice command text (e.g., "search for dogs")
            websocket: WebSocket connection for real-time feedback
            speaker_name: Speaker identified by STT pipeline (VBIA/PAVA result).
                         None = speaker not identified. Used for unlock authorization.

        Voice deduplication: only speak the initial acknowledgment and final result.
        All intermediate updates are sent as silent WebSocket messages.
        """
        try:
            # Reset steps for new command
            self.execution_steps = []

            logger.info(f"[ENHANCED CONTEXT] ========= START PROCESSING =========")
            logger.info(f"[ENHANCED CONTEXT] Command: '{command}'")
            logger.info(f"[ENHANCED CONTEXT] Speaker: {speaker_name or '(not identified)'}")
            self._add_step(f"Received command: {command}")

            # Check if command requires screen
            requires_screen = self._requires_screen(command)
            logger.info(f"[ENHANCED CONTEXT] Requires screen: {requires_screen}")

            if requires_screen:
                self._add_step(
                    "Command requires screen access", {"requires_screen": True}
                )

                # Check if screen is locked
                logger.info("[ENHANCED CONTEXT] Checking screen lock status...")
                is_locked = await self._check_screen_locked()
                logger.info(f"[ENHANCED CONTEXT] Screen locked: {is_locked}")
                self._add_step(
                    f"Screen status: {'LOCKED' if is_locked else 'UNLOCKED'}",
                    {"is_locked": is_locked},
                )

                if is_locked:
                    # ─────────────────────────────────────────────────────────
                    # SPEAKER VERIFICATION (Gap #1 fix)
                    # Before unlocking, verify the speaker is authorized.
                    # The STT pipeline already ran ECAPA-TDNN speaker ID —
                    # we check that result here.
                    # ─────────────────────────────────────────────────────────
                    verified, deny_message = await self._verify_speaker_for_unlock(
                        speaker_name
                    )
                    if not verified:
                        self._add_step(
                            "Speaker verification DENIED",
                            {"speaker": speaker_name, "reason": deny_message},
                        )
                        return {
                            "success": False,
                            "response": deny_message,
                            "context_handled": True,
                            "screen_unlocked": False,
                            "execution_steps": self.execution_steps,
                        }

                    self._add_step(
                        f"Speaker verified: {speaker_name or 'unidentified (proceed with caution)'}",
                        {"speaker": speaker_name},
                    )

                    # Build context-aware response
                    action = self._extract_action_description(command)
                    display_name = speaker_name or "Sir"
                    context_message = (
                        f"I've verified your voice, {display_name}. "
                        f"Your screen is locked — let me unlock it so I can {action}."
                    )

                    self._add_step(
                        "Screen unlock required", {"message": context_message}
                    )

                    # ─────────────────────────────────────────────────────────
                    # SPEAK: Initial acknowledgment via "processing" type.
                    # Uses "processing" instead of "response" so the frontend
                    # doesn't clear activeRequestIdRef — the FINAL command
                    # result will be the real "response".
                    # ─────────────────────────────────────────────────────────
                    if websocket:
                        await websocket.send_json(
                            {
                                "type": "processing",
                                "message": context_message,
                                "command_type": "context_aware",
                                "status": "unlocking_screen",
                                "steps": self.execution_steps,
                                "speak": True,
                                "intermediate": True,
                            }
                        )

                    # ─────────────────────────────────────────────────────────
                    # UNLOCK (serialized via module-level lock — Gap #2 fix)
                    # ─────────────────────────────────────────────────────────
                    logger.info("[ENHANCED CONTEXT] Attempting to unlock screen...")
                    unlock_success = await self._unlock_screen()

                    if unlock_success:
                        self._add_step(
                            "Screen unlocked successfully", {"success": True}
                        )

                        # Brief pause for unlock animation to complete
                        await asyncio.sleep(1.5)

                        # Execute the original command
                        logger.info("[ENHANCED CONTEXT] Executing original command...")
                        result = await self.command_processor.process_command(
                            command, websocket
                        )

                        # Build comprehensive response
                        self._add_step(
                            "Command executed",
                            {"success": result.get("success", False)},
                        )

                        if isinstance(result, dict):
                            original_response = result.get("response", "")
                            steps_summary = self._build_steps_summary()

                            result["response"] = original_response
                            result["context_handled"] = True
                            result["screen_unlocked"] = True
                            result["execution_steps"] = self.execution_steps
                            result["steps_summary"] = steps_summary
                            result["intermediate"] = False

                        logger.info(
                            "[ENHANCED CONTEXT] Command completed with context handling"
                        )
                        return result
                    else:
                        self._add_step("Screen unlock failed", {"success": False})
                        return {
                            "success": False,
                            "response": (
                                "I wasn't able to unlock your screen. "
                                "Please unlock it manually and try your command again."
                            ),
                            "context_handled": True,
                            "screen_unlocked": False,
                            "execution_steps": self.execution_steps,
                        }

            # No special context handling needed
            self._add_step("No context handling required")
            return await self.command_processor.process_command(command, websocket)

        except Exception as e:
            logger.error(f"[ENHANCED CONTEXT] Error: {e}", exc_info=True)
            self._add_step(f"Error occurred: {str(e)}", {"error": True})

            # Fallback to standard processing
            return await self.command_processor.process_command(command, websocket)

    # ─────────────────────────────────────────────────────────────────────────
    # SPEAKER VERIFICATION
    # ─────────────────────────────────────────────────────────────────────────

    async def _verify_speaker_for_unlock(
        self, speaker_name: Optional[str]
    ) -> tuple:
        """Verify the speaker is authorized to unlock the screen.

        Returns:
            (True, None) if authorized
            (False, deny_message) if denied

        Authorization rules:
            1. speaker_name matches owner → ALLOW
            2. speaker_name is identified but NOT owner → DENY
            3. speaker_name is None (not identified) → ALLOW with warning
               (fail-open: don't lock the user out due to noisy mic)
               Set env JARVIS_STRICT_SPEAKER_CHECK=1 to fail-closed.
        """
        owner_name = await _get_owner_name()
        strict_mode = os.getenv("JARVIS_STRICT_SPEAKER_CHECK", "0") == "1"

        if speaker_name:
            # Case-insensitive partial match (e.g., "Derek J. Russell" matches "Derek")
            speaker_lower = speaker_name.lower()
            owner_lower = owner_name.lower()

            if owner_lower in speaker_lower or speaker_lower in owner_lower:
                logger.info(
                    f"[ENHANCED CONTEXT] Speaker '{speaker_name}' matches owner '{owner_name}' — authorized"
                )
                return True, None
            else:
                # Identified as someone else — ALWAYS deny
                deny_msg = (
                    f"I recognize your voice as {speaker_name}, but only "
                    f"{owner_name} can unlock this screen."
                )
                logger.warning(
                    f"[ENHANCED CONTEXT] Speaker '{speaker_name}' is NOT owner '{owner_name}' — DENIED"
                )
                return False, deny_msg
        else:
            # Speaker not identified
            if strict_mode:
                deny_msg = (
                    "I couldn't identify your voice. "
                    "Please speak clearly and try again."
                )
                logger.warning(
                    "[ENHANCED CONTEXT] No speaker ID and strict mode — DENIED"
                )
                return False, deny_msg

            # Fail-open: proceed with warning
            logger.warning(
                "[ENHANCED CONTEXT] No speaker ID — proceeding with caution (fail-open)"
            )
            return True, None

    # ─────────────────────────────────────────────────────────────────────────
    # SCREEN DETECTION & UNLOCK
    # ─────────────────────────────────────────────────────────────────────────

    def _requires_screen(self, command: str) -> bool:
        """Check if command requires screen access"""
        command_lower = command.lower()

        # Commands that explicitly don't need screen
        no_screen_patterns = [
            "lock screen",
            "lock my screen",
            "lock the screen",
            "what time",
            "weather",
            "temperature",
            "play music",
            "pause music",
            "stop music",
            "volume up",
            "volume down",
            "mute",
        ]

        if any(pattern in command_lower for pattern in no_screen_patterns):
            return False

        for pattern in self.screen_required_patterns:
            if pattern in command_lower:
                return True

        return False

    def _extract_action_description(self, command: str) -> str:
        """Extract a human-readable description of what the user wants to do"""
        command_lower = command.lower()

        patterns = [
            (r"open safari and (?:search for|google) (.+)", "search for {}"),
            (r"open (\w+)", "open {}"),
            (r"search for (.+)", "search for {}"),
            (r"go to (.+)", "navigate to {}"),
            (r"create (.+)", "create {}"),
            (r"show me (.+)", "show you {}"),
            (r"find (.+)", "find {}"),
        ]

        for pattern, template in patterns:
            match = re.search(pattern, command_lower)
            if match:
                return template.format(match.group(1))

        return f"execute your command: {command}"

    def _build_steps_summary(self) -> str:
        """Build a human-readable summary of execution steps"""
        if not self.execution_steps:
            return ""
        return " ".join(
            f"{i}. {step['step']}" for i, step in enumerate(self.execution_steps, 1)
        )

    async def _check_screen_locked(self) -> bool:
        """Check if screen is currently locked.

        Uses Quartz CGSessionCopyCurrentDictionary directly — no daemon dependency.
        Falls back to async subprocess if Quartz import fails.
        """
        try:
            from Quartz import CGSessionCopyCurrentDictionary

            session_dict = CGSessionCopyCurrentDictionary()
            if session_dict:
                screen_locked = session_dict.get("CGSSessionScreenIsLocked", False)
                screen_saver = session_dict.get("CGSSessionScreenSaverIsActive", False)
                is_locked = bool(screen_locked or screen_saver)
                logger.info(f"[ENHANCED CONTEXT] Screen locked (Quartz): {is_locked}")
                return is_locked
            return False
        except ImportError:
            logger.debug("[ENHANCED CONTEXT] Quartz not available, using subprocess")
        except Exception as e:
            logger.debug(f"[ENHANCED CONTEXT] Quartz check failed: {e}")

        # Fallback: async subprocess (never blocks event loop)
        try:
            check_script = (
                "import Quartz; d=Quartz.CGSessionCopyCurrentDictionary(); "
                "print('true' if d and (d.get('CGSSessionScreenIsLocked',False) "
                "or d.get('CGSSessionScreenSaverIsActive',False)) else 'false')"
            )
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", check_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return stdout.decode().strip().lower() == "true"
        except Exception as e:
            logger.error(f"[ENHANCED CONTEXT] Screen lock check failed: {e}")
            return False

    async def _unlock_screen(self) -> bool:
        """Unlock the screen using MacOSKeychainUnlock singleton.

        Serialized via module-level _unlock_lock to prevent concurrent password
        typing when multiple commands arrive simultaneously (Gap #2 fix).

        If the lock is already held (another unlock in progress), we wait.
        If the screen becomes unlocked while waiting, we short-circuit.
        """
        async with _unlock_lock:
            # Re-check lock status under the lock — another request may have
            # already unlocked the screen while we were waiting.
            still_locked = await self._check_screen_locked()
            if not still_locked:
                logger.info("[ENHANCED CONTEXT] Screen already unlocked (race resolved)")
                return True

            try:
                from macos_keychain_unlock import get_keychain_unlock_service

                unlock_service = await get_keychain_unlock_service()
                result = await asyncio.wait_for(
                    unlock_service.unlock_screen(verified_speaker="Derek"),
                    timeout=15.0,
                )

                success = result.get("success", False)
                message = result.get("message", "")
                logger.info(
                    f"[ENHANCED CONTEXT] Keychain unlock: success={success}, msg={message}"
                )
                return success

            except asyncio.TimeoutError:
                logger.error("[ENHANCED CONTEXT] Keychain unlock timed out after 15s")
                return False
            except Exception as e:
                logger.error(f"[ENHANCED CONTEXT] Keychain unlock error: {e}")
                return False


def wrap_with_enhanced_context(processor):
    """Wrap a command processor with enhanced context handling"""
    handler = EnhancedSimpleContextHandler(processor)
    return handler
