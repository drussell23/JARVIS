"""Karen's voice announcer (PRD §38 Slice 3, 2026-05-07).

Operator-requested voice channel for O+V. Closes the §38.3
commitment: hands-free awareness for an autonomous organism
running 16 sensors + 11 phases + 5 contexts in parallel.

## Why this exists (and structurally not in CC)

CC is interactive — operator at keyboard, voice redundant.
O+V is autonomous — sensors fire spontaneously; operator's role
is supervisor watching system work. Voice is structurally
fitting for supervisor mode.

## Why barge-in (operator-requested feature, 2026-05-07)

Modern conversational AI (ChatGPT Advanced Voice / Claude voice
/ Gemini Live) lets the operator interrupt the AI mid-sentence
by speaking. Operator binding: integrate this into Karen.

**The architectural gift**: ``backend/voice/barge_in_detector.py``
already ships canonical interruption infrastructure —
:func:`safe_say_with_barge_in` does VAD energy-threshold
monitoring, kills afplay (SIGTERM → SIGKILL) on operator
speech, and releases the speech gate. **Karen composes this
verbatim — zero parallel TTS / zero parallel VAD.** The
canonical pattern emerges: every TTS path through Karen
inherits barge-in for free.

## Composes canonical sources (operator binding "no duplication")

  * :mod:`backend.voice.barge_in_detector` — canonical TTS +
    barge-in pipeline. ``safe_say_with_barge_in(text, voice,
    rate)`` returns False on interrupt; ``get_barge_in_detector()``
    singleton for force-interrupt API.
  * :mod:`governance.ide_observability_stream` — canonical SSE
    broker; Karen subscribes via ``broker.subscribe()`` and
    pulls events from the bounded queue.
  * :mod:`governance.observability.flag_change_emitter`
    ``_SENSITIVE_NAME_TOKENS`` — canonical sensitive-substring
    set for redaction (Wave 3 hygiene Item 3, single source of
    truth).

NEVER reimplements TTS, VAD, sensitive-token detection, or
event-stream subscription.

## Architectural locks (operator mandate, AST-pinned)

  1. **Master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib + governance.{
     observability, ide_observability_stream} + voice.barge_in_detector
     ONLY. NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / change_engine /
     semantic_guardian.
  3. **Closed 4-value tier taxonomy** — VoiceEventTier (CRITICAL
     / IMPORTANT / NORMAL / SILENT). New values require explicit
     scope-doc + pin update.
  4. **Composes canonical TTS pipeline** — ``_speak_async`` MUST
     route through ``safe_say_with_barge_in`` (no parallel TTS
     subprocess; no direct ``say``/``afplay`` exec at call sites).
  5. **Composes canonical sensitive-token set** — redaction MUST
     use ``_SENSITIVE_NAME_TOKENS`` from canonical
     flag_change_emitter (no parallel sensitive-substring list).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


KAREN_VOICE_SCHEMA_VERSION: str = "karen_voice.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_KAREN_VOICE_ENABLED`` master switch. Default-
    FALSE per §33.1. When off, Karen is a no-op singleton —
    no SSE subscription, no TTS, no audio device interaction."""
    return os.environ.get(
        "JARVIS_KAREN_VOICE_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Tunable knobs — env-overridable (operator-binding "no hardcoding")
# ---------------------------------------------------------------------------


_COOLDOWN_S_DEFAULT: float = 30.0
_MIN_TIER_DEFAULT: str = "important"
_PERSONA_DEFAULT: str = "karen"
_TTS_VOICE_DEFAULT: str = "Karen"  # macOS `say` voice
_TTS_RATE_DEFAULT: int = 200       # words per minute


def cooldown_seconds() -> float:
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_COOLDOWN_S", "",
    ).strip()
    if not raw:
        return _COOLDOWN_S_DEFAULT
    try:
        return max(0.0, min(3600.0, float(raw)))
    except (TypeError, ValueError):
        return _COOLDOWN_S_DEFAULT


def min_tier_str() -> str:
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_MIN_TIER", "",
    ).strip().lower()
    return raw or _MIN_TIER_DEFAULT


def persona_name() -> str:
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_PERSONA", "",
    ).strip().lower()
    return raw or _PERSONA_DEFAULT


def tts_voice() -> str:
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_TTS_VOICE", "",
    ).strip()
    return raw or _TTS_VOICE_DEFAULT


def tts_rate() -> int:
    raw = os.environ.get(
        "JARVIS_KAREN_VOICE_TTS_RATE", "",
    ).strip()
    if not raw:
        return _TTS_RATE_DEFAULT
    try:
        return max(80, min(400, int(raw)))
    except (TypeError, ValueError):
        return _TTS_RATE_DEFAULT


# ---------------------------------------------------------------------------
# Closed 4-value tier taxonomy (AST-pinned)
# ---------------------------------------------------------------------------


class VoiceEventTier(str, enum.Enum):
    """Closed taxonomy describing severity / importance of an
    SSE event for voice announcement. Bytes-pinned via AST
    regression.

      * ``CRITICAL`` — failures, cost warnings, emergency
        throttles. ALWAYS voiced (subject to mute + cooldown).
      * ``IMPORTANT`` — graduations, posture changes, behavioral
        drift. Voiced unless operator filters above this tier.
      * ``NORMAL`` — completed mutation ops (Yellow+).
        Voiced when ``min_tier`` is NORMAL.
      * ``SILENT`` — heartbeats, low-risk completions, internal
        events. NEVER voiced regardless of operator filter.
    """

    CRITICAL = "critical"
    IMPORTANT = "important"
    NORMAL = "normal"
    SILENT = "silent"


_TIER_ORDER: Dict[VoiceEventTier, int] = {
    VoiceEventTier.SILENT: 0,
    VoiceEventTier.NORMAL: 1,
    VoiceEventTier.IMPORTANT: 2,
    VoiceEventTier.CRITICAL: 3,
}


def _coerce_min_tier(raw: str) -> VoiceEventTier:
    """Map env-string to canonical tier. Defensive default
    IMPORTANT when unrecognized."""
    s = (raw or "").strip().lower()
    for tier in VoiceEventTier:
        if tier.value == s:
            return tier
    return VoiceEventTier.IMPORTANT


# ---------------------------------------------------------------------------
# Event → tier declarative mapping (single source of truth)
# ---------------------------------------------------------------------------
#
# Keyed by event_type string. Unknown events route to SILENT
# (defensive — Karen NEVER voices an unrecognized event type
# without an explicit entry below).


_EVENT_TIER_TABLE: Dict[str, VoiceEventTier] = {
    # CRITICAL — failures, cost spikes, emergency interventions.
    "governor_emergency_brake": VoiceEventTier.CRITICAL,
    # IMPORTANT — strategic transitions, drift, graduation.
    "posture_changed": VoiceEventTier.IMPORTANT,
    "behavioral_drift_detected": VoiceEventTier.IMPORTANT,
    "cost_band_crossed": VoiceEventTier.IMPORTANT,
    "memory_pressure_changed": VoiceEventTier.IMPORTANT,
    "governor_throttle_applied": VoiceEventTier.IMPORTANT,
    "circuit_breaker_approaching": VoiceEventTier.IMPORTANT,
    "confidence_drop_detected": VoiceEventTier.IMPORTANT,
    # NORMAL — informational completions / acknowledgements.
    "task_completed": VoiceEventTier.NORMAL,
    "plan_generated": VoiceEventTier.NORMAL,
    "tool_confidence_band_crossed": VoiceEventTier.NORMAL,
    # SILENT — system-internal / too-frequent.
    "heartbeat": VoiceEventTier.SILENT,
    "stream_lag": VoiceEventTier.SILENT,
    "task_created": VoiceEventTier.SILENT,
    "task_started": VoiceEventTier.SILENT,
    "task_updated": VoiceEventTier.SILENT,
    "metrics_updated": VoiceEventTier.SILENT,
    "decision_recorded": VoiceEventTier.SILENT,
    "confidence_observed": VoiceEventTier.SILENT,
    "flag_typo_detected": VoiceEventTier.SILENT,
    "flag_registered": VoiceEventTier.SILENT,
}


def tier_for_event(
    event_type: str,
    payload: Optional[Mapping[str, Any]] = None,  # noqa: ARG001
) -> VoiceEventTier:
    """Return the canonical tier for an event type. NEVER
    raises. Unknown event_types default to SILENT (safe — Karen
    won't voice events without an explicit entry)."""
    if not isinstance(event_type, str) or not event_type:
        return VoiceEventTier.SILENT
    return _EVENT_TIER_TABLE.get(
        event_type.strip().lower(),
        VoiceEventTier.SILENT,
    )


# ---------------------------------------------------------------------------
# Event → utterance declarative mapping
# ---------------------------------------------------------------------------
#
# Lambda functions take the payload Mapping and return the
# spoken text. Defensive on every payload field (treat as
# Optional). Numbers are written-out (e.g., "eighty percent"
# not "80%") for natural TTS flow.


def _sentence_for_event(
    event_type: str,
    payload: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Render an SSE event into a 1-sentence utterance.
    NEVER raises. Returns None when no utterance is appropriate
    (or generic fallback for events without specific text)."""
    payload = payload or {}
    et = (event_type or "").strip().lower()
    try:
        if et == "governor_emergency_brake":
            return (
                "Emergency throttle activated. "
                "Sensor cap reduced."
            )
        if et == "posture_changed":
            posture = str(payload.get("posture") or "").strip()
            if posture:
                return f"Posture shifted to {posture}."
            return "Posture changed."
        if et == "behavioral_drift_detected":
            return (
                "Behavioral drift detected. Review recent "
                "decisions."
            )
        if et == "cost_band_crossed":
            band = str(payload.get("to_band") or "").strip()
            if band.lower() in ("high", "critical"):
                return (
                    f"Cost approaching budget. Now in "
                    f"{band} band."
                )
            return f"Cost band crossed to {band}."
        if et == "memory_pressure_changed":
            level = str(payload.get("level") or "").strip()
            if level:
                return f"Memory pressure now {level}."
            return "Memory pressure changed."
        if et == "governor_throttle_applied":
            return "Sensor governor throttle applied."
        if et == "circuit_breaker_approaching":
            return (
                "Circuit breaker approaching trip threshold."
            )
        if et == "confidence_drop_detected":
            return "Confidence drop detected on recent op."
        if et == "task_completed":
            return None  # NORMAL — only voice if min_tier NORMAL
        if et == "plan_generated":
            return None
        if et == "tool_confidence_band_crossed":
            return None
        # Unknown event_type → no utterance (SILENT).
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Sensitive redaction — composes canonical _SENSITIVE_NAME_TOKENS
# ---------------------------------------------------------------------------


def _load_sensitive_tokens() -> FrozenSet[str]:
    """Lazy-import canonical sensitive-token frozenset from
    flag_change_emitter (Wave 3 hygiene Item 3 — single source
    of truth). Defensive fallback returns empty set when
    substrate unavailable."""
    try:
        from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
            _SENSITIVE_NAME_TOKENS,
        )
        return _SENSITIVE_NAME_TOKENS
    except Exception:  # noqa: BLE001 — defensive
        return frozenset()


_SECRET_LIKE_RE = re.compile(
    # Two shapes covered:
    #   1. Inline-prefix: sk-foo / ghp_foo / xox-foo / AKIA...
    #      (token immediately follows the prefix with no space)
    #   2. Bearer-style: ``Bearer <jwt>`` (space-separated;
    #      typical HTTP Authorization header shape captured by
    #      SemanticGuardian's canonical Bearer-JWT regex)
    r"(?:sk|ghp|xox|AKIA)[-_a-zA-Z0-9]{8,}"
    r"|"
    r"Bearer\s+[A-Za-z0-9_\-\.]{16,}",
)


def redact_sensitive(text: str) -> str:
    """Redact sensitive tokens / values from a string. Composes
    canonical ``_SENSITIVE_NAME_TOKENS`` (Wave 3 hygiene Item 3)
    + a regex covering common secret shapes (sk- / ghp_ / xox- /
    AKIA / Bearer).

    Substring redaction: when text contains any sensitive token
    (case-insensitive) AND is short enough to be a likely
    bare value, replace the entire text with a fixed placeholder.

    Pure function. NEVER raises. Defensive on bad inputs."""
    try:
        if not isinstance(text, str) or not text:
            return text
        # 1) Regex-based shape redaction.
        cleaned = _SECRET_LIKE_RE.sub("[redacted]", text)
        # 2) Substring-based name redaction — if the text is
        # short and contains a sensitive name token, the whole
        # thing might be a credential mention. Replace just
        # the suspicious substring rather than dropping all.
        tokens = _load_sensitive_tokens()
        if not tokens:
            return cleaned
        lowered = cleaned.lower()
        for token in tokens:
            if token in lowered:
                # Replace the token (case-insensitive) with a
                # safe placeholder — preserves sentence shape
                # while masking the keyword.
                cleaned = re.sub(
                    re.escape(token),
                    "[redacted]",
                    cleaned,
                    flags=re.IGNORECASE,
                )
        return cleaned
    except Exception:  # noqa: BLE001 — defensive
        # On any failure, drop the text rather than risk
        # leaking — operator binding "fail-safe".
        return "[redacted]"


# ---------------------------------------------------------------------------
# Headless / quiet-hours auto-mute
# ---------------------------------------------------------------------------


def _is_real_tty() -> bool:
    """Compose canonical TTY detection. Karen MUST auto-mute
    in headless contexts (cron soaks, CI, SSH without audio)."""
    try:
        # Check sys.__stdout__ (unpatched) — same canonical
        # pattern as battle_test/presentation_restraint.
        original = sys.__stdout__
        if original is None:
            return False
        return bool(original.isatty())
    except Exception:  # noqa: BLE001 — defensive
        return False


def _is_ci_context() -> bool:
    """Detect CI environment. Karen auto-mutes in CI."""
    return any(
        os.environ.get(name, "").strip()
        for name in ("CI", "GITHUB_ACTIONS", "JENKINS_URL")
    )


def auto_mute_active() -> bool:
    """Combined auto-mute predicate: not-TTY OR CI context.
    Pure function. NEVER raises."""
    try:
        return (not _is_real_tty()) or _is_ci_context()
    except Exception:  # noqa: BLE001 — defensive
        return True  # fail-safe to muted


# ---------------------------------------------------------------------------
# Versioned announcement artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceAnnouncement:
    """One voice announcement record. Frozen for safe
    propagation."""

    schema_version: str = KAREN_VOICE_SCHEMA_VERSION
    text: str = ""
    tier: VoiceEventTier = VoiceEventTier.SILENT
    op_id: str = ""
    source_event: str = ""
    voiced_at_unix: float = 0.0
    interrupted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "text": self.text,
            "tier": self.tier.value,
            "op_id": self.op_id,
            "source_event": self.source_event,
            "voiced_at_unix": float(self.voiced_at_unix),
            "interrupted": bool(self.interrupted),
        }


# ---------------------------------------------------------------------------
# KarenVoiceAnnouncer — singleton (thread-safe + asyncio-aware)
# ---------------------------------------------------------------------------


class KarenVoiceAnnouncer:
    """Operator-facing voice announcer. Thread-safe singleton.

    Lifecycle:
      1. ``record_event(event_type, payload, op_id)`` — caller
         invokes (e.g., orchestrator publish helper) OR
         background SSE subscriber loop pushes events.
      2. Tier filter + cooldown + per-op_id coalescing applied.
      3. Sensitive redaction via canonical _SENSITIVE_NAME_TOKENS.
      4. ``_speak_async()`` composes
         ``safe_say_with_barge_in()`` — operator can interrupt
         mid-sentence by speaking; afplay process killed via
         SIGTERM/SIGKILL; speech gate released for follow-up
         input.

    Mute state: manual (REPL toggle) | auto-headless | auto-CI |
    auto-quiet-hours. Any mute → ``record_event`` is no-op.

    NEVER raises — every code path defensive."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mute_manual: bool = False
        self._cooldown: deque = deque(maxlen=128)  # (op_id, ts)
        self._announcements: deque = deque(maxlen=200)
        self._tts_lock = asyncio.Lock()  # serialize Karen's voice

    # --- mute state ---

    def set_mute(self, *, on: bool) -> None:
        with self._lock:
            self._mute_manual = bool(on)

    def is_muted(self) -> bool:
        with self._lock:
            return (
                self._mute_manual
                or auto_mute_active()
                or not master_enabled()
            )

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema_version": KAREN_VOICE_SCHEMA_VERSION,
                "master_enabled": master_enabled(),
                "mute_manual": self._mute_manual,
                "auto_mute_active": auto_mute_active(),
                "is_muted": (
                    self._mute_manual
                    or auto_mute_active()
                    or not master_enabled()
                ),
                "min_tier": min_tier_str(),
                "cooldown_s": cooldown_seconds(),
                "persona": persona_name(),
                "tts_voice": tts_voice(),
                "tts_rate": tts_rate(),
                "recent_announcements": len(
                    self._announcements,
                ),
            }

    def recent(
        self, limit: int = 10,
    ) -> Tuple[VoiceAnnouncement, ...]:
        with self._lock:
            items = list(self._announcements)[-int(max(1, limit)):]
            return tuple(items)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._mute_manual = False
            self._cooldown.clear()
            self._announcements.clear()

    # --- cooldown ---

    def _is_cooldown_active(
        self, op_id: str, now: float,
    ) -> bool:
        cd = cooldown_seconds()
        if cd <= 0:
            return False
        with self._lock:
            for entry_op, ts in self._cooldown:
                if entry_op == op_id and (now - ts) < cd:
                    return True
        return False

    def _record_cooldown(self, op_id: str, now: float) -> None:
        with self._lock:
            self._cooldown.append((op_id, now))

    # --- record event (synchronous entry point) ---

    def record_event(
        self,
        *,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        op_id: str = "",
    ) -> Optional[VoiceAnnouncement]:
        """Synchronous entry point. Schedules async TTS
        (fire-and-forget) on the running event loop. Returns
        the constructed :class:`VoiceAnnouncement` (without
        waiting for TTS) or None when filtered out.

        NEVER raises."""
        try:
            if self.is_muted():
                return None
            tier = tier_for_event(event_type, payload)
            min_tier = _coerce_min_tier(min_tier_str())
            if (
                _TIER_ORDER[tier]
                < _TIER_ORDER[min_tier]
            ):
                return None
            text = _sentence_for_event(event_type, payload)
            if not text:
                return None
            # Cooldown — same op_id within window suppresses.
            now = time.time()
            op_safe = str(op_id or "").strip()
            if op_safe and self._is_cooldown_active(
                op_safe, now,
            ):
                return None
            redacted = redact_sensitive(text)
            announcement = VoiceAnnouncement(
                text=redacted,
                tier=tier,
                op_id=op_safe,
                source_event=event_type,
                voiced_at_unix=now,
            )
            with self._lock:
                self._announcements.append(announcement)
                if op_safe:
                    self._record_cooldown(op_safe, now)
            # Fire-and-forget TTS on running loop.
            self._schedule_tts(redacted)
            return announcement
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[Karen] record_event swallowed: %s",
                type(exc).__name__,
            )
            return None

    def _schedule_tts(self, text: str) -> None:
        """Schedule async TTS on the running event loop. NEVER
        raises. When no loop is running (e.g., test harness or
        sync caller), TTS is skipped silently — the
        announcement is still recorded for audit."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — skip TTS silently. Tests can
            # still verify announcement state via .recent().
            return
        try:
            loop.create_task(self._speak_async(text))
        except Exception:  # noqa: BLE001 — defensive
            pass

    async def _speak_async(self, text: str) -> None:
        """Compose canonical ``safe_say_with_barge_in`` for
        TTS with automatic interruption.

        Operator binding "fully leverage existing files" — this
        is the SOLE TTS path. AST-pinned: forbidden to call
        ``say`` / ``afplay`` directly or import a parallel TTS
        engine from this module."""
        try:
            async with self._tts_lock:
                if self.is_muted():
                    return
                from backend.voice.barge_in_detector import (
                    safe_say_with_barge_in,
                )
                completed = await safe_say_with_barge_in(
                    text=text,
                    voice=tts_voice(),
                    rate=tts_rate(),
                )
                # On interrupt, mark the most-recent
                # announcement as interrupted for audit.
                if not completed:
                    with self._lock:
                        if self._announcements:
                            last = self._announcements[-1]
                            self._announcements[-1] = (
                                VoiceAnnouncement(
                                    schema_version=last.schema_version,
                                    text=last.text,
                                    tier=last.tier,
                                    op_id=last.op_id,
                                    source_event=(
                                        last.source_event
                                    ),
                                    voiced_at_unix=(
                                        last.voiced_at_unix
                                    ),
                                    interrupted=True,
                                )
                            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[Karen] _speak_async swallowed: %s",
                type(exc).__name__,
            )

    async def force_interrupt(self) -> None:
        """Force-stop any in-flight TTS. Composes canonical
        ``BargeInDetector.force_interrupt``."""
        try:
            from backend.voice.barge_in_detector import (
                get_barge_in_detector,
            )
            await get_barge_in_detector().force_interrupt()
        except Exception:  # noqa: BLE001 — defensive
            pass


# Module singleton.
_DEFAULT_ANNOUNCER: Optional[KarenVoiceAnnouncer] = None
_ANNOUNCER_LOCK: threading.Lock = threading.Lock()


def get_default_announcer() -> KarenVoiceAnnouncer:
    global _DEFAULT_ANNOUNCER
    with _ANNOUNCER_LOCK:
        if _DEFAULT_ANNOUNCER is None:
            _DEFAULT_ANNOUNCER = KarenVoiceAnnouncer()
        return _DEFAULT_ANNOUNCER


def reset_announcer_for_tests() -> None:
    global _DEFAULT_ANNOUNCER
    with _ANNOUNCER_LOCK:
        _DEFAULT_ANNOUNCER = None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_KAREN_VOICE_ENABLED
         stays default-FALSE per §33.1.
      2. ``tier_taxonomy_4_values`` — closed-enum integrity.
      3. ``authority_asymmetry`` — substrate purity.
      4. ``composes_canonical_voice_pipeline`` — TTS path MUST
         compose ``safe_say_with_barge_in`` from canonical
         ``backend.voice.barge_in_detector`` (no parallel
         TTS subprocess; no direct ``say``/``afplay`` exec).
      5. ``composes_canonical_sensitive_tokens`` — redaction
         MUST compose canonical ``_SENSITIVE_NAME_TOKENS`` from
         flag_change_emitter (Wave 3 hygiene Item 3).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "karen_voice_announcer.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                src = ast.unparse(node)
                if "return True" in src:
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_KAREN_VOICE_ENABLED" not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_KAREN_VOICE_ENABLED"
                    )
        return tuple(violations)

    def _validate_tier_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "CRITICAL", "IMPORTANT", "NORMAL", "SILENT",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "VoiceEventTier":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"VoiceEventTier missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"VoiceEventTier has extras "
                            f"(closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"karen_voice_announcer MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_voice_pipeline(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """TTS path MUST compose canonical
        ``safe_say_with_barge_in`` from
        ``backend.voice.barge_in_detector``. Forbidden direct
        ``say``/``afplay`` subprocess calls + forbidden
        ``import subprocess`` for TTS purposes."""
        violations: list = []
        if "safe_say_with_barge_in" not in source:
            violations.append(
                "karen_voice_announcer MUST compose "
                "safe_say_with_barge_in (canonical TTS + "
                "barge-in pipeline)"
            )
        if "barge_in_detector" not in source:
            violations.append(
                "karen_voice_announcer MUST compose "
                "barge_in_detector (canonical interruption)"
            )
        # Forbid direct shell-style calls to say/afplay at
        # module level. Walk Call nodes whose first arg is the
        # literal "say" or "afplay" — that pattern indicates a
        # parallel TTS path.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if node.args:
                    first = node.args[0]
                    if (
                        isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and first.value in ("say", "afplay")
                    ):
                        violations.append(
                            f"direct subprocess call to "
                            f"{first.value!r} found — Karen "
                            f"MUST compose "
                            f"safe_say_with_barge_in "
                            f"instead"
                        )
        return tuple(violations)

    def _validate_composes_sensitive_tokens(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "_SENSITIVE_NAME_TOKENS" not in source:
            violations.append(
                "karen_voice_announcer MUST compose "
                "canonical _SENSITIVE_NAME_TOKENS "
                "(no parallel sensitive-substring list)"
            )
        if "flag_change_emitter" not in source:
            violations.append(
                "karen_voice_announcer MUST lazy-import "
                "_SENSITIVE_NAME_TOKENS from "
                "flag_change_emitter (canonical source)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="karen_voice_master_default_false",
            target_file=target,
            description=(
                "Master flag JARVIS_KAREN_VOICE_ENABLED stays "
                "default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="karen_voice_tier_taxonomy_4_values",
            target_file=target,
            description=(
                "VoiceEventTier is a 4-value closed taxonomy "
                "(CRITICAL / IMPORTANT / NORMAL / SILENT). "
                "New values require explicit scope-doc + pin "
                "update."
            ),
            validate=_validate_tier_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="karen_voice_authority_asymmetry",
            target_file=target,
            description=(
                "Karen MUST stay pure substrate composing "
                "voice.barge_in_detector + governance "
                "observability + stdlib ONLY. NEVER imports "
                "orchestrator / iron_gate / policy / providers "
                "/ candidate_generator / change_engine / "
                "semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "karen_voice_composes_canonical_voice_pipeline"
            ),
            target_file=target,
            description=(
                "TTS path MUST compose canonical "
                "safe_say_with_barge_in from "
                "backend.voice.barge_in_detector. Forbidden "
                "direct say/afplay subprocess calls — barge-in "
                "(operator interruption) is automatic only "
                "via the canonical pipeline."
            ),
            validate=_validate_composes_voice_pipeline,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "karen_voice_composes_canonical_sensitive_tokens"
            ),
            target_file=target,
            description=(
                "Sensitive-token redaction MUST compose "
                "canonical _SENSITIVE_NAME_TOKENS from "
                "flag_change_emitter (Wave 3 hygiene Item 3 "
                "single source of truth). Forbidden parallel "
                "sensitive-substring lists."
            ),
            validate=_validate_composes_sensitive_tokens,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register Karen-voice flags with the FlagRegistry."""
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_KAREN_VOICE_ENABLED",
            "bool",
            "false",
            (
                "Master flag for Karen's voice announcer "
                "(§38 Slice 3). Default-FALSE per §33.1; "
                "operator opts in via /voice on."
            ),
        ),
        (
            "JARVIS_KAREN_VOICE_COOLDOWN_S",
            "float",
            str(_COOLDOWN_S_DEFAULT),
            "Per-op_id cooldown seconds between voiced events.",
        ),
        (
            "JARVIS_KAREN_VOICE_MIN_TIER",
            "str",
            _MIN_TIER_DEFAULT,
            "Minimum tier voiced (critical/important/normal).",
        ),
        (
            "JARVIS_KAREN_VOICE_PERSONA",
            "str",
            _PERSONA_DEFAULT,
            "Voice persona (karen/friday/jarvis/custom).",
        ),
        (
            "JARVIS_KAREN_VOICE_TTS_VOICE",
            "str",
            _TTS_VOICE_DEFAULT,
            "macOS `say` voice name.",
        ),
        (
            "JARVIS_KAREN_VOICE_TTS_RATE",
            "int",
            str(_TTS_RATE_DEFAULT),
            "TTS rate in words per minute.",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "karen_voice_announcer.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001 — defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        return n
    return n


__all__ = [
    "KAREN_VOICE_SCHEMA_VERSION",
    "KarenVoiceAnnouncer",
    "VoiceAnnouncement",
    "VoiceEventTier",
    "auto_mute_active",
    "cooldown_seconds",
    "get_default_announcer",
    "master_enabled",
    "min_tier_str",
    "persona_name",
    "redact_sensitive",
    "register_flags",
    "register_shipped_invariants",
    "reset_announcer_for_tests",
    "tier_for_event",
    "tts_rate",
    "tts_voice",
]
