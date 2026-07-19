"""Ambient OS — Phase 1 (dual personas) + Phase 2 (wake briefing).

Operator authorization 2026-07-19. Two persona lanes over ONE audio
plane:

  * **JARVIS / Daniel** — system + managerial voice (wake briefings,
    session status, host-OS events).
  * **O+V / Karen**    — engineering voice (codebase, ops, failures).

``classify_persona`` is deterministic semantic routing (mandate 1: no
hardcoded toggles — the FSM payload's semantic class picks the voice;
voice NAMES are env-tunable, the CLASS map is closed vocabulary).

Phase 2 — the Lid-Open briefing pipeline (every stage injectable):

  wake event → delta (LastSessionSummary) → ZERO delta: abort silently
  → oversized delta: compress (ContextCompactor composition, mandate 3)
  → **Coffee-Shop Protocol**: interrogate live audio topology; loud
  speakers + no private output → ABORT TTS, emit a silent IPC/TUI
  payload instead (no public acoustic leak) → speak via Daniel.

Production wake source is ``NSWorkspaceDidWakeNotification`` via
pyobjc (zero-cost event, never a poll); tests inject ``fire()``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.Ambient")

PERSONA_DANIEL = "daniel"
PERSONA_KAREN = "karen"
PERSONAS = (PERSONA_DANIEL, PERSONA_KAREN)

#: Closed semantic-class → persona map. Classes come from the FSM
#: payload (SpeechRequest source / event kind), NEVER from a toggle.
_SEMANTIC_MAP = {
    # system / managerial plane → JARVIS (Daniel)
    "system": PERSONA_DANIEL,
    "briefing": PERSONA_DANIEL,
    "session": PERSONA_DANIEL,
    "wake": PERSONA_DANIEL,
    "health": PERSONA_DANIEL,
    # codebase / engineering plane → O+V (Karen)
    "engineering": PERSONA_KAREN,
    "ouroboros": PERSONA_KAREN,
    "codebase": PERSONA_KAREN,
    "op": PERSONA_KAREN,
    "test": PERSONA_KAREN,
    "karen": PERSONA_KAREN,
    "chat": PERSONA_KAREN,
}


def classify_persona(semantic_class: str) -> str:
    """Deterministic payload-class → persona. Unknown classes route to
    Karen (the organism's own voice — engineering is the default plane
    of this codebase). NEVER raises."""
    try:
        return _SEMANTIC_MAP.get(
            str(semantic_class or "").strip().lower(), PERSONA_KAREN,
        )
    except Exception:  # noqa: BLE001
        return PERSONA_KAREN


def persona_voice(persona: str) -> str:
    """macOS voice name for a persona — env-tunable, never hardcoded
    at call sites. NEVER raises."""
    try:
        if persona == PERSONA_DANIEL:
            return os.environ.get("JARVIS_PERSONA_VOICE_DANIEL", "Daniel")
        return os.environ.get("JARVIS_PERSONA_VOICE_KAREN", "Karen")
    except Exception:  # noqa: BLE001
        return "Karen"


async def say_with_persona(
    text: str,
    persona: str,
    *,
    runner: Optional[Callable[..., Awaitable[Any]]] = None,
) -> bool:
    """Native macOS ``say -v <voice>`` binding (async subprocess — the
    OS's own synthesis, no ML stack in-process). Injectable ``runner``
    for tests. True when the utterance completed. NEVER raises."""
    try:
        text = str(text or "").strip()
        if not text:
            return False
        voice = persona_voice(persona)
        if runner is not None:
            await runner(voice, text)
            return True
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", voice, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        logger.debug("[Ambient] say degraded", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Coffee-Shop Protocol — audio topology awareness
# ---------------------------------------------------------------------------


def _volume_threshold() -> float:
    try:
        raw = float(os.environ.get("JARVIS_AMBIENT_VOLUME_THRESHOLD", "0.30"))
    except (TypeError, ValueError):
        raw = 0.30
    return max(0.0, min(1.0, raw))


def default_topology_probe() -> Dict[str, Any]:
    """Interrogate the host's REAL audio state: output volume (0..1)
    and whether a private output (headphones/Bluetooth) is active.
    osascript is the dependency-free native binding; a pyobjc/CoreAudio
    probe can replace it behind the same shape. Fail-CLOSED: an
    unreadable topology reports loud speakers (never risk the leak).
    NEVER raises."""
    import subprocess
    vol, external = 1.0, False
    try:
        out = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        vol = max(0.0, min(1.0, float(out) / 100.0))
    except Exception:  # noqa: BLE001
        pass
    try:
        prof = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-detailLevel", "mini"],
            capture_output=True, text=True, timeout=5,
        ).stdout.lower()
        external = any(
            marker in prof
            for marker in ("headphone", "bluetooth", "usb", "airpods")
        )
    except Exception:  # noqa: BLE001
        pass
    return {"volume": vol, "external_output": external}


def speech_permitted(topology: Dict[str, Any]) -> bool:
    """The Coffee-Shop rule: block TTS when the room would hear it —
    loud built-in speakers (> threshold) with NO private output.
    NEVER raises; unreadable fields fail CLOSED (silent)."""
    try:
        vol = float(topology.get("volume", 1.0))
        external = bool(topology.get("external_output", False))
        if external:
            return True
        return vol <= _volume_threshold()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Delta briefing — conditional + compressed
# ---------------------------------------------------------------------------


def _briefing_max_chars() -> int:
    try:
        return max(80, int(os.environ.get(
            "JARVIS_BRIEFING_MAX_CHARS", "420",
        )))
    except (TypeError, ValueError):
        return 420


async def compress_delta(entries: List[Dict[str, Any]]) -> str:
    """Delta Summarization Compression (mandate 3: ContextCompactor
    composition — its deterministic summary machinery, no second
    summarizer). Small deltas render verbatim; a multi-day backlog
    compresses to high-level abstraction so Daniel never delivers a
    multi-minute monologue. NEVER raises; degrades to a bounded
    count-digest."""
    try:
        if not entries:
            return ""
        joined = " ".join(str(e.get("text", "")) for e in entries).strip()
        if len(joined) <= _briefing_max_chars():
            return joined
        try:
            from backend.core.ouroboros.governance.context_compaction import (  # noqa: E501,PLC0415
                ContextCompactor,
            )
            result = await ContextCompactor().compact(list(entries))
            summary = ""
            for attr in ("summary_text", "summary", "digest"):
                summary = str(getattr(result, attr, "") or "")
                if summary:
                    break
            if summary:
                return summary[: _briefing_max_chars()]
        except Exception:  # noqa: BLE001
            logger.debug("[Ambient] compactor degraded", exc_info=True)
        n = len(entries)
        head = str(entries[0].get("text", ""))[:160]
        return (
            f"While you were away I tracked {n} items. Most recent: "
            f"{head}. Ask for the full ledger when ready."
        )
    except Exception:  # noqa: BLE001
        return ""


class SystemWakeObserver:
    """Phase 2 orchestrator — one wake event in, at most ONE briefing
    out. Every collaborator is injected (delta provider, topology
    probe, speaker, silent sink) so the full FSM is unit-provable
    without hardware. Production wiring:

      * ``start()`` registers ``NSWorkspaceDidWakeNotification`` via
        pyobjc when available (zero-cost native event; NO polling —
        absence of pyobjc simply leaves the observer dormant).
      * delta   ← LastSessionSummary operator digest
      * speaker ← :func:`say_with_persona` (Daniel)
      * silent  ← cockpit-attach ``publish_line`` (the TUI payload).
    """

    def __init__(
        self,
        *,
        delta_provider: Optional[Callable[[], Any]] = None,
        topology_probe: Optional[Callable[[], Dict[str, Any]]] = None,
        speaker: Optional[Callable[[str, str], Awaitable[bool]]] = None,
        silent_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._delta = delta_provider or self._default_delta
        self._topology = topology_probe or default_topology_probe
        self._speak = speaker or say_with_persona
        self._silent = silent_sink or (lambda _t: None)
        self._observer_token: Any = None
        self.stats: Dict[str, int] = {
            "wakes": 0, "no_delta_aborts": 0, "spoken": 0,
            "suppressed_tui": 0, "compressed": 0,
        }

    @staticmethod
    def _default_delta() -> Any:
        try:
            from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501,PLC0415
                get_default_summary,
            )
            text = get_default_summary().operator_digest_sync()
            return [{"text": text}] if text and str(text).strip() else []
        except Exception:  # noqa: BLE001
            return []

    async def handle_system_wake(self) -> str:
        """The briefing FSM. Returns the outcome verdict:
        ``no_delta`` / ``suppressed_tui`` / ``spoken`` / ``degraded``.
        NEVER raises."""
        try:
            self.stats["wakes"] += 1
            raw = self._delta() or []
            entries = [
                e if isinstance(e, dict) else {"text": str(e)} for e in raw
            ]
            entries = [e for e in entries if str(e.get("text", "")).strip()]
            if not entries:
                # Ambient systems earn trust by knowing when to shut up.
                self.stats["no_delta_aborts"] += 1
                return "no_delta"
            joined_len = sum(len(str(e.get("text", ""))) for e in entries)
            briefing = await compress_delta(entries)
            if joined_len > _briefing_max_chars():
                self.stats["compressed"] += 1
            if not briefing:
                return "no_delta"
            line = f"Welcome back. {briefing}"
            topology = self._topology() or {}
            if not speech_permitted(topology):
                # Coffee-Shop Protocol: the room would hear it — route
                # the SAME payload silently to the TUI plane instead.
                self.stats["suppressed_tui"] += 1
                self._safe_silent(f"💭 JARVIS ▸ {line}")
                return "suppressed_tui"
            spoken = await self._speak(line, PERSONA_DANIEL)
            if spoken:
                self.stats["spoken"] += 1
                self._safe_silent(f"💭 JARVIS ▸ {line}")
                return "spoken"
            self._safe_silent(f"💭 JARVIS ▸ {line}")
            return "degraded"
        except Exception:  # noqa: BLE001
            logger.debug("[Ambient] wake handling degraded", exc_info=True)
            return "degraded"

    def _safe_silent(self, text: str) -> None:
        try:
            self._silent(text)
        except Exception:  # noqa: BLE001
            pass

    # ---- production notification wiring (pyobjc) ----

    def start(self) -> bool:
        """Register the native wake observer. False (dormant, never a
        poll loop) when pyobjc is unavailable. NEVER raises."""
        try:
            from AppKit import NSWorkspace  # noqa: PLC0415
            from Foundation import NSOperationQueue  # noqa: PLC0415

            loop = asyncio.get_event_loop()

            def _on_wake(_note: Any) -> None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.handle_system_wake(), loop,
                    )
                except Exception:  # noqa: BLE001
                    pass

            center = NSWorkspace.sharedWorkspace().notificationCenter()
            self._observer_token = center.addObserverForName_object_queue_usingBlock_(  # noqa: E501
                "NSWorkspaceDidWakeNotification", None,
                NSOperationQueue.mainQueue(), _on_wake,
            )
            logger.info("[Ambient] NSWorkspace wake observer registered")
            return True
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Ambient] pyobjc unavailable — wake observer dormant",
            )
            return False

    def stop(self) -> None:
        """Unregister. NEVER raises."""
        try:
            token = self._observer_token
            self._observer_token = None
            if token is not None:
                from AppKit import NSWorkspace  # noqa: PLC0415
                NSWorkspace.sharedWorkspace().notificationCenter(
                ).removeObserver_(token)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "PERSONA_DANIEL",
    "PERSONA_KAREN",
    "PERSONAS",
    "SystemWakeObserver",
    "classify_persona",
    "compress_delta",
    "default_topology_probe",
    "persona_voice",
    "say_with_persona",
    "speech_permitted",
]
