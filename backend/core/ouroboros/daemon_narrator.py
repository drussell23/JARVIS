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

# NARRATOR HONESTY POLICY: Templates must only state verified facts.
# Never claim success without confirmation. Use "attempting", "proposed",
# "detected" — not "implemented", "applied", "fixed".
# The narrator reports what happened, not what it hopes happened.

_EVENT_TEMPLATES: Dict[str, Tuple[str, str]] = {
    "rem.epoch_start": (
        "rem",
        "Starting system review.",
    ),
    "rem.epoch_complete": (
        "rem",
        "Review complete. {findings_count} findings logged.",
    ),
    "synthesis.complete": (
        "synthesis",
        "Analysis complete. {hypothesis_count} areas flagged for review.",
    ),
    "saga.started": (
        "saga",
        "Planning changes for {title}.",
    ),
    "saga.complete": (
        "saga",
        "Changes for {title} proposed. Awaiting your review.",
    ),
    "saga.aborted": (
        "saga",
        "Stopped work on {title}. Reason: {reason}.",
    ),
    "governance.patch_applied": (
        "patch",
        "Proposed change: {description}. Pending validation.",
    ),
    "governance.patch_verified": (
        "patch",
        "Change verified: {description}. Tests passed.",
    ),
    "governance.patch_failed": (
        "patch",
        "Change failed validation: {description}.",
    ),
    "vital.warn": (
        "vital",
        "Boot check: {warning_count} issues detected.",
    ),
    # --- Slice 109: cognitive-bus lifecycle narration -------------------
    # High-severity cognitive events bound to the bus via
    # cognitive_observability. Dynamically formatted from the lifecycle
    # payload; the voice gate (JARVIS_KAREN_VOICE_ENABLED) + mute state
    # are enforced UPSTREAM before on_event is ever called.
    "cognitive.containment_breach": (
        "cognitive_breach",
        "Containment breach detected on operation {op_id}. The candidate "
        "was quarantined; no code escaped the cage.",
    ),
    "cognitive.graduation_threshold_met": (
        "cognitive_graduation",
        "A graduation threshold was met for {flag}. Receipt accrued in "
        "shadow mode; the flag was not flipped. Your decision.",
    ),
    "cognitive.load_shedding_active": (
        "cognitive_loadshed",
        "Load shedding is active. Cognitive fan-out throttled to protect "
        "the host.",
    ),
    "cognitive.post_failure": (
        "cognitive_failure",
        "Operation {op_id} failed at {phase}. Belief revised; the lesson "
        "was recorded.",
    ),
    "cognitive.post_apply": (
        "cognitive_apply",
        "Applied {op_id} with confidence {confidence}.",
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
        voice: str = "Samantha",
    ) -> None:
        self._say_fn = say_fn
        self.rate_limit_s = rate_limit_s
        self.enabled = enabled
        self._voice = voice
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
            await self._say_fn(message, voice=self._voice, source="ouroboros_narrator", skip_dedup=True)
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
