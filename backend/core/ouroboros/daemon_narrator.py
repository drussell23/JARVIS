"""backend/core/ouroboros/daemon_narrator.py

DaemonNarrator — rate-limited voice for Ouroboros autonomous events.

Maps well-known daemon event types to human-readable speech templates and
voices them via an injected say_fn, enforcing a per-category rate limit to
avoid flooding the user with announcements during rapid autonomous activity.

Design contracts
----------------
* ``on_event`` never raises — TTS failures are caught and logged.
* Rate limiting is per event *category* (e.g. "rem", "saga"), not per
  event type, so that high-frequency epochs within the same category are
  naturally throttled together.
* Templating is done with ``str.format(**payload)``; a missing key falls
  back to the raw template string rather than raising.
* No model inference involved — purely deterministic string formatting.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.DaemonNarrator")

# ---------------------------------------------------------------------------
# Event registry
# Each entry maps event_type → (category, speech_template).
# Templates use {key} placeholders drawn from the event payload.
# ---------------------------------------------------------------------------

_EVENT_TEMPLATES: Dict[str, Tuple[str, str]] = {
    "rem.epoch_start": (
        "rem",
        "Entering REM Sleep. Scanning the organism.",
    ),
    "rem.epoch_complete": (
        "rem",
        "REM complete. Found {findings_count} issues. {envelopes_submitted} patches submitted.",
    ),
    "synthesis.complete": (
        "synthesis",
        "Roadmap analysis complete. {hypothesis_count} capability gaps identified.",
    ),
    "saga.started": (
        "saga",
        "Designing {title}. {step_count} implementation steps.",
    ),
    "saga.complete": (
        "saga",
        "Feature implemented: {title}. PR ready for review.",
    ),
    "saga.aborted": (
        "saga",
        "Saga aborted: {reason}.",
    ),
    "governance.patch_applied": (
        "patch",
        "Patch applied: {description}.",
    ),
    "vital.warn": (
        "vital",
        "Boot scan: {warning_count} warnings. REM will address them.",
    ),
}


# ---------------------------------------------------------------------------
# DaemonNarrator
# ---------------------------------------------------------------------------


class DaemonNarrator:
    """Rate-limited voice for Ouroboros autonomous daemon events.

    Parameters
    ----------
    say_fn:
        Async callable with signature ``(message: str, *, source: str,
        skip_dedup: bool) -> bool``. Typically ``safe_say`` from the voice
        orchestrator.  When *None* the narrator is effectively silent.
    rate_limit_s:
        Minimum seconds that must elapse between two spoken messages in the
        *same category*.  Set to ``0.0`` to disable rate limiting.
    enabled:
        Master switch.  When ``False``, ``on_event`` returns immediately
        without any speech or side effects.
    """

    def __init__(
        self,
        say_fn: Optional[Callable[..., Any]] = None,
        rate_limit_s: float = 60.0,
        enabled: bool = True,
    ) -> None:
        self._say_fn = say_fn
        self.rate_limit_s = rate_limit_s
        self.enabled = enabled
        # Maps category → monotonic timestamp of last spoken message.
        self._last_spoken_at: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Handle a daemon lifecycle event.

        Parameters
        ----------
        event_type:
            Dot-namespaced event identifier (e.g. ``"rem.epoch_start"``).
            Unknown types are silently ignored.
        payload:
            Arbitrary event data dictionary used to format the speech
            template.
        """
        if not self.enabled or self._say_fn is None:
            return

        entry = _EVENT_TEMPLATES.get(event_type)
        if entry is None:
            logger.debug("DaemonNarrator: unknown event_type %r — ignored", event_type)
            return

        category, template = entry

        # Per-category rate limiting.
        if self.rate_limit_s > 0.0:
            now = time.monotonic()
            last = self._last_spoken_at.get(category, float("-inf"))
            if (now - last) < self.rate_limit_s:
                logger.debug(
                    "DaemonNarrator: rate-limited category=%r event=%r",
                    category,
                    event_type,
                )
                return

        message = self._format(template, payload)

        try:
            await self._say_fn(message, source="ouroboros_narrator", skip_dedup=True)
            self._last_spoken_at[category] = time.monotonic()
        except Exception:
            logger.debug(
                "DaemonNarrator: say_fn failed for event %r", event_type, exc_info=True
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format(template: str, payload: Dict[str, Any]) -> str:
        """Format *template* with *payload*, falling back to raw template on error."""
        try:
            return template.format(**payload)
        except KeyError as exc:
            logger.debug(
                "DaemonNarrator: missing payload key %s — using raw template", exc
            )
            return template
