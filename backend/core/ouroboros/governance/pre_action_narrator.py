"""
PreActionNarrator — Real-time voice narration of WHAT and WHY before each action.

Like Claude Code showing "Reading file X... Editing line 42..." in real-time,
but spoken aloud. JARVIS explains what it's about to do BEFORE doing it.

Hooks into the orchestrator at each phase transition to voice the intent:
  "I'm classifying this operation. The target is voice_unlock/core/verify.py."
  "Routing to the 397B model — this is a complex operation."
  "Generating a fix for the cosine similarity threshold."
  "Running tests on the modified file."
  "The fix passed validation. Applying to disk now."

Boundary Principle:
  Deterministic: Phase → narration template mapping. No model inference
  for the narration itself — just template + context variables.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_PRE_ACTION_NARRATION_ENABLED", "true"
).lower() in ("true", "1", "yes")

# Phase narration templates with {placeholders}
_PHASE_TEMPLATES: Dict[str, str] = {
    "CLASSIFY": (
        "I'm analyzing this operation. Target: {target_file}. "
        "Checking risk level and complexity."
    ),
    "ROUTE": (
        "Routing to {provider}. {reason}"
    ),
    "CONTEXT_EXPANSION": (
        "Gathering context. Searching the codebase, checking documentation, "
        "and analyzing dependencies for {target_file}."
    ),
    "GENERATE": (
        "Generating the fix now using {provider}. "
        "{thinking_mode} thinking mode."
    ),
    "VALIDATE": (
        "Running tests on the modified code. "
        "Checking {test_count} test files."
    ),
    "GATE": (
        "Security review. Checking for vulnerabilities and policy compliance."
    ),
    "APPROVE": (
        "The fix looks good. Requesting approval to apply."
    ),
    "APPLY": (
        "Applying the fix to {target_file}. Writing to disk now."
    ),
    "VERIFY": (
        "Verifying the change. Running benchmarks and integrity checks."
    ),
    "COMPLETE": (
        "Done. The operation completed successfully in {duration}."
    ),
    "POSTMORTEM": (
        "The operation failed. Analyzing the root cause. {reason}"
    ),
}

# Shortened templates for fast-path narration (less verbose)
_SHORT_TEMPLATES: Dict[str, str] = {
    "CLASSIFY": "Classifying: {target_file}.",
    "ROUTE": "Routing to {provider}.",
    "CONTEXT_EXPANSION": "Gathering context.",
    "GENERATE": "Generating fix.",
    "VALIDATE": "Testing.",
    "GATE": "Security check.",
    "APPLY": "Applying.",
    "COMPLETE": "Done.",
}


class PreActionNarrator:
    """Voices intent before each pipeline action.

    Unlike ReasoningNarrator (explains WHY after), this explains
    WHAT is about to happen BEFORE it happens — in real-time.

    Integrates with safe_say() for voice output and with the
    serpent animation for visual + audio synchronization.
    """

    def __init__(
        self,
        say_fn: Optional[Callable[..., Coroutine]] = None,
        verbose: bool = True,
    ) -> None:
        self._say_fn = say_fn
        self._verbose = verbose and _ENABLED
        self._last_narration: float = 0.0
        self._min_interval_s = float(
            os.environ.get("JARVIS_PRE_ACTION_MIN_INTERVAL_S", "2.0")
        )

    async def narrate_phase(
        self,
        phase: str,
        context: Dict[str, str] = None,
    ) -> None:
        """Voice the intent for an upcoming phase.

        Skips narration if:
        - Disabled via env var
        - Too soon since last narration (debounce)
        - No say_fn provided
        """
        if not self._verbose or not self._say_fn:
            return

        # Debounce — don't narrate too rapidly
        now = time.time()
        if (now - self._last_narration) < self._min_interval_s:
            return

        context = context or {}
        templates = _PHASE_TEMPLATES if self._verbose else _SHORT_TEMPLATES
        template = templates.get(phase, "")
        if not template:
            return

        # Fill placeholders with context (missing keys get "unknown")
        try:
            text = template.format_map(
                _DefaultDict(context, default="unknown")
            )
        except Exception:
            text = template  # Use raw template on format error

        try:
            await self._say_fn(text)
            self._last_narration = time.time()
            logger.debug("[PreAction] %s: %s", phase, text[:60])
        except Exception:
            pass  # Voice failure should never block the pipeline

    async def narrate_file_creation(self, file_path: str, reason: str = "") -> None:
        """Narrate that a new file is about to be created."""
        if not self._verbose or not self._say_fn:
            return
        name = Path(file_path).name
        text = f"Creating new file: {name}. {reason}" if reason else f"Creating: {name}."
        try:
            await self._say_fn(text)
        except Exception:
            pass

    async def narrate_file_edit(self, file_path: str, lines: str = "") -> None:
        """Narrate that a file is about to be edited."""
        if not self._verbose or not self._say_fn:
            return
        name = Path(file_path).name
        text = f"Editing {name}"
        if lines:
            text += f" at lines {lines}"
        text += "."
        try:
            await self._say_fn(text)
        except Exception:
            pass

    async def narrate_test_run(self, test_count: int = 0) -> None:
        """Narrate that tests are about to run."""
        if not self._verbose or not self._say_fn:
            return
        if test_count:
            text = f"Running {test_count} tests."
        else:
            text = "Running tests."
        try:
            await self._say_fn(text)
        except Exception:
            pass

    async def narrate_custom(self, text: str) -> None:
        """Narrate a custom message."""
        if not self._verbose or not self._say_fn:
            return
        try:
            await self._say_fn(text)
        except Exception:
            pass


class _DefaultDict(dict):
    """Dict that returns a default for missing keys (for template formatting)."""

    def __init__(self, data: Dict[str, str], default: str = "unknown") -> None:
        super().__init__(data)
        self._default = default

    def __missing__(self, key: str) -> str:
        return self._default
