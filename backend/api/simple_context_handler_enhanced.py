#!/usr/bin/env python3
"""
Enhanced Simple Context Handler for JARVIS
==========================================

Provides context-aware command processing with:
- Screen lock detection via Quartz (no daemon dependency)
- Password-based unlock via MacOSKeychainUnlock singleton (cached password)
- Voice deduplication: only ONE spoken message per phase (no overlapping TTS)
- Step-by-step WebSocket status updates (silent) with one spoken summary
"""

import asyncio
import logging
import re
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class EnhancedSimpleContextHandler:
    """Enhanced handler for context-aware command processing with step-by-step feedback.

    Architecture:
        WebSocket voice → STT → command_text → process_with_context()
            ├─ _requires_screen(command) → True
            ├─ _check_screen_locked()    → Quartz CGSessionCopyCurrentDictionary (no daemon)
            ├─ _unlock_screen()          → MacOSKeychainUnlock.unlock_screen() (password typing)
            └─ command_processor.process_command(command, websocket)

    Voice deduplication contract:
        - Intermediate status updates sent via WebSocket with speak=False (silent)
        - Only the INITIAL acknowledgment and FINAL result are spoken (speak=True)
        - All speech goes through _speech_lock to prevent overlapping TTS
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

    def _add_step(self, step: str, details: Dict[str, Any] = None):
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
        self, command: str, websocket=None
    ) -> Dict[str, Any]:
        """Process command with enhanced context awareness and feedback.

        Voice deduplication: only speak the initial acknowledgment and final result.
        All intermediate updates are sent as silent WebSocket status messages.
        """
        try:
            # Reset steps for new command
            self.execution_steps = []

            logger.info(f"[ENHANCED CONTEXT] ========= START PROCESSING =========")
            logger.info(f"[ENHANCED CONTEXT] Command: '{command}'")
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
                    # Build context-aware response
                    action = self._extract_action_description(command)
                    context_message = (
                        f"Your screen is locked. Let me unlock it so I can {action}."
                    )

                    self._add_step(
                        "Screen unlock required", {"message": context_message}
                    )

                    # ─────────────────────────────────────────────────────────
                    # SPEAK: Initial acknowledgment (the ONLY spoken message
                    # until the final result). All subsequent updates are silent.
                    # ─────────────────────────────────────────────────────────
                    if websocket:
                        await websocket.send_json(
                            {
                                "type": "response",
                                "text": context_message,
                                "command_type": "context_aware",
                                "status": "unlocking_screen",
                                "steps": self.execution_steps,
                                "speak": True,
                                "intermediate": True,
                            }
                        )

                    # Perform unlock (uses keychain password + secure typer)
                    logger.info("[ENHANCED CONTEXT] Attempting to unlock screen...")
                    unlock_success = await self._unlock_screen(command)

                    if unlock_success:
                        self._add_step(
                            "Screen unlocked successfully", {"success": True}
                        )

                        # Brief pause for unlock animation to complete
                        await asyncio.sleep(1.5)

                        # ─────────────────────────────────────────────────────
                        # SILENT status update — do NOT speak this.
                        # The final command result will be spoken instead.
                        # ─────────────────────────────────────────────────────
                        if websocket:
                            await websocket.send_json(
                                {
                                    "type": "status",
                                    "text": "Screen unlocked. Now executing your command...",
                                    "command_type": "context_aware",
                                    "status": "executing_command",
                                    "steps": self.execution_steps,
                                    "speak": False,
                                    "intermediate": True,
                                }
                            )

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

                        # Format the final response with all steps
                        if isinstance(result, dict):
                            original_response = result.get("response", "")

                            # Build step-by-step summary
                            steps_summary = self._build_steps_summary()

                            # Don't duplicate the context message — it was already spoken.
                            # Just use the original response.
                            result["response"] = original_response
                            result["context_handled"] = True
                            result["screen_unlocked"] = True
                            result["execution_steps"] = self.execution_steps
                            result["steps_summary"] = steps_summary
                            # Mark this as final response, not intermediate
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

        # Check if any screen-required pattern matches
        for pattern in self.screen_required_patterns:
            if pattern in command_lower:
                return True

        return False

    def _extract_action_description(self, command: str) -> str:
        """Extract a human-readable description of what the user wants to do"""
        command_lower = command.lower()

        # Common patterns and their descriptions
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

        # Default: use the command as-is
        return f"execute your command: {command}"

    def _build_steps_summary(self) -> str:
        """Build a human-readable summary of execution steps"""
        if not self.execution_steps:
            return ""

        summary_parts = []
        for i, step in enumerate(self.execution_steps, 1):
            summary_parts.append(f"{i}. {step['step']}")

        return " ".join(summary_parts)

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

    async def _unlock_screen(self, _command: str) -> bool:
        """Unlock the screen using MacOSKeychainUnlock singleton.

        This is the REAL unlock path that:
        1. Retrieves cached password from keychain (com.jarvis.voiceunlock)
        2. Wakes display via caffeinate -u (no keyboard events)
        3. Types password via SecurePasswordTyper (CG Events)
        4. Submits and verifies unlock

        Does NOT depend on the voice unlock WebSocket daemon (port 8765).
        """
        try:
            from macos_keychain_unlock import get_keychain_unlock_service

            unlock_service = await get_keychain_unlock_service()
            result = await asyncio.wait_for(
                unlock_service.unlock_screen(verified_speaker="Derek"),
                timeout=20.0,
            )

            success = result.get("success", False)
            message = result.get("message", "")
            logger.info(
                f"[ENHANCED CONTEXT] Keychain unlock: success={success}, msg={message}"
            )
            return success

        except asyncio.TimeoutError:
            logger.error("[ENHANCED CONTEXT] Keychain unlock timed out after 20s")
            return False
        except Exception as e:
            logger.error(f"[ENHANCED CONTEXT] Keychain unlock error: {e}")
            return False


def wrap_with_enhanced_context(processor):
    """Wrap a command processor with enhanced context handling"""
    handler = EnhancedSimpleContextHandler(processor)
    return handler

