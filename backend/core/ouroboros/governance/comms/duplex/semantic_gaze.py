"""Semantic Gaze — foveated vision that is DORMANT by construction.

Multimodal Vision Plane (operator authorization 2026-07-19). This is
an ORCHESTRATOR over existing organs — zero new capture code:

  * capture   → ``backend.vision.screen_vision.ScreenVisionSystem.
    capture_screen`` (async; native ``CGWindowListCreateImage`` /
    ``CGDisplayCreateImage``; injectable seam)
  * VLM       → ``backend.vision.claude_vision_analyzer_main.
    ClaudeVisionAnalyzer.analyze_image_with_prompt`` (existing organ)
  * delta     → the VisionSensor's dhash discipline (buffer-hash
    delta pruning; unchanged screen → the VLM is NEVER invoked, the
    cached semantic state answers)
  * thermal   → the SAME SovereignGovernor verdict as the audio plane
    (mandate 3): SERIOUS/CRITICAL hard-aborts BEFORE capture and
    Daniel verbally reports the degradation instead.

Gating law (mandate 2 — no while-True frame pumps, ever):

    capture ⇐ lease ACTIVE ∧ semantic-visual intent ∧ thermal OK
    VLM     ⇐ capture ∧ dhash delta changed

Everything injectable; NEVER raises on any path.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.SemanticGaze")

VERDICT_DORMANT = "dormant"                # no lease / no visual intent
VERDICT_THERMAL_LOCKED = "thermal_locked"
VERDICT_CACHED = "cached_semantic_state"
VERDICT_ANALYZED = "analyzed"

_THERMAL_WARNING = (
    "I can't look at the screen right now — the system is thermally "
    "degraded and vision processing is paused to protect the hardware."
)


def _visual_vocabulary() -> tuple:
    raw = os.environ.get(
        "JARVIS_GAZE_SEMANTIC_TRIGGERS",
        "this,look,screen,code,see,showing,display,window,error here",
    )
    return tuple(w.strip().lower() for w in raw.split(",") if w.strip())


def has_visual_intent(command: str) -> bool:
    """Semantic requirement detector — closed trigger vocabulary,
    env-extendable. NEVER raises."""
    try:
        import re  # noqa: PLC0415
        # Word tokens only (strip punctuation) so "screen?" matches
        # "screen" — a trigger followed by punctuation is still a
        # trigger; substring false-hits ("scanner"⊅"scan") are avoided.
        tokens = set(re.findall(r"[a-z']+", str(command or "").lower()))
        low = str(command or "").lower()
        for w in _visual_vocabulary():
            if " " in w:                       # multi-word trigger
                if w in low:
                    return True
            elif w in tokens:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


class SemanticGaze:
    """Collaborators (all injected; production defaults resolve the
    EXISTING organs lazily):

    * ``lease_active``  — () → bool (sentry LEASED?)
    * ``thermal_ok``    — () → bool (SovereignGovernor verdict)
    * ``capture``       — () → frame-bytes | None (CGWindowList seam)
    * ``frame_hash``    — (frame) → str (dhash discipline)
    * ``vlm``           — async (frame, command) → str (semantic state)
    * ``speak``         — async (text, persona) → bool (Daniel's voice)
    """

    def __init__(
        self,
        *,
        lease_active: Optional[Callable[[], bool]] = None,
        thermal_ok: Optional[Callable[[], bool]] = None,
        capture: Optional[Callable[[], Any]] = None,
        frame_hash: Optional[Callable[[Any], str]] = None,
        vlm: Optional[Callable[..., Awaitable[str]]] = None,
        speak: Optional[Callable[..., Awaitable[bool]]] = None,
    ) -> None:
        self._lease_active = lease_active or (lambda: False)
        self._thermal_ok = thermal_ok or self._default_thermal_ok
        self._capture = capture or self._default_capture
        self._hash = frame_hash or self._default_hash
        self._vlm = vlm if vlm is not None else self._default_vlm
        self._speak = speak or self._default_speak
        # Unified Epistemic Bus (2026-07-19): the cache lives at the
        # orchestrator root, NOT in this persona FSM — every gaze
        # instance shares one pointer (split-brain cure).
        from .epistemic_bus import get_default_bus  # noqa: PLC0415
        self._bus = get_default_bus()
        self.stats: Dict[str, int] = {
            "dormant": 0, "thermal_locks": 0, "cache_hits": 0,
            "vlm_calls": 0, "captures": 0,
        }

    # ---- production defaults (existing organs, lazily) ----

    @staticmethod
    def _default_thermal_ok() -> bool:
        # DRY mandate 3: the SAME governor verdict the audio plane
        # obeys — one thermal truth for every modality.
        try:
            from .sovereign_governor import evolution_permitted  # noqa: E501,PLC0415
            return evolution_permitted()
        except Exception:  # noqa: BLE001
            return False                       # unreadable = locked

    @staticmethod
    async def _default_capture() -> Any:
        # EXISTING organ (operator directive: reuse, never duplicate):
        # ScreenVisionSystem.capture_screen is async + native Quartz.
        from backend.vision.screen_vision import (  # noqa: PLC0415
            ScreenVisionSystem,
        )
        return await ScreenVisionSystem().capture_screen()

    @staticmethod
    def _default_hash(frame: Any) -> str:
        try:
            import hashlib  # noqa: PLC0415
            data = frame if isinstance(frame, (bytes, bytearray)) else bytes(
                getattr(frame, "tobytes", lambda: repr(frame).encode())(),
            )
            # Coarse structural hash on a strided sample — the delta
            # gate needs CHANGE detection, not cryptography.
            return hashlib.sha256(data[::max(1, len(data) // 65536)]).hexdigest()[:16]
        except Exception:  # noqa: BLE001
            return str(time.time())            # unhashable → always-changed

    @staticmethod
    async def _default_vlm(frame: Any, command: str) -> str:
        # EXISTING organ: ClaudeVisionAnalyzer.analyze_image_with_prompt.
        from backend.vision.claude_vision_analyzer_main import (  # noqa: E501,PLC0415
            ClaudeVisionAnalyzer,
        )
        try:
            analyzer = ClaudeVisionAnalyzer(
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
            )
        except TypeError:
            analyzer = ClaudeVisionAnalyzer()  # older ctor signatures
        result = await analyzer.analyze_image_with_prompt(frame, command)
        if isinstance(result, dict):
            return str(result.get("content", "") or "")
        return str(result or "")

    @staticmethod
    async def _default_speak(text: str, persona: str = "daniel") -> bool:
        from .ambient import say_with_persona  # noqa: PLC0415
        return await say_with_persona(text, persona)

    @staticmethod
    def _referential(command: str) -> bool:
        """True when the command REFERS to prior visual context rather
        than requesting a fresh look ("that error", "patch this",
        "fix it")."""
        import re  # noqa: PLC0415
        tokens = set(re.findall(r"[a-z']+", str(command or "").lower()))
        return bool(tokens & {"that", "it", "this", "same", "those"})

    @staticmethod
    def _force_recapture(command: str) -> bool:
        """Explicit re-look verbs override inheritance ("look AGAIN",
        "refresh")."""
        low = str(command or "").lower()
        return any(w in low for w in ("again", "refresh", "re-look", "now"))

    # ---- the gaze ----

    async def request(self, command: str) -> Dict[str, Any]:
        """One gaze request. Returns
        ``{"verdict": ..., "semantic_state": ...}``. NEVER raises."""
        try:
            if not self._lease_active() or not has_visual_intent(command):
                self.stats["dormant"] += 1
                return {"verdict": VERDICT_DORMANT, "semantic_state": ""}
            if not self._thermal_ok():
                # Mandate 4: the lock is caught BEFORE capture and
                # Daniel says WHY — silence would read as deafness.
                self.stats["thermal_locks"] += 1
                try:
                    await self._speak(_THERMAL_WARNING, "daniel")
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "verdict": VERDICT_THERMAL_LOCKED,
                    "semantic_state": _THERMAL_WARNING,
                }
            shared_pre = self._bus.inherit_visual()
            if (
                shared_pre is not None
                and self._referential(command)
                and not self._force_recapture(command)
            ):
                # Cross-persona inheritance: "Karen, patch THAT" — a
                # REFERENTIAL command points at the shared memory; the
                # pointer is fresh; Quartz is NOT re-triggered. Fresh-
                # look commands ("look at the screen") always capture
                # so spatial invalidation can see a Space swap.
                self.stats["cache_hits"] += 1
                return {
                    "verdict": VERDICT_CACHED,
                    "semantic_state": shared_pre["semantic_state"],
                }
            import inspect  # noqa: PLC0415
            frame = self._capture()
            if inspect.isawaitable(frame):
                frame = await frame            # ScreenVisionSystem is async
            if frame is None:
                self.stats["dormant"] += 1
                return {"verdict": VERDICT_DORMANT, "semantic_state": ""}
            self.stats["captures"] += 1
            h = self._hash(frame)
            shared = self._bus.inherit_visual()
            if shared is not None and shared["frame_hash"] == h:
                # Delta pruning against the SHARED pointer: unchanged
                # screen → VLM bypassed, every persona answers from
                # the same epistemic state.
                self.stats["cache_hits"] += 1
                return {
                    "verdict": VERDICT_CACHED,
                    "semantic_state": shared["semantic_state"],
                }
            if shared is not None:
                # Catastrophic delta: Space swap / IDE closed — the
                # remembered screen is gone; flush before re-looking.
                self._bus.spatial_invalidate(h)
            if self._vlm is None:
                return {"verdict": VERDICT_DORMANT, "semantic_state": ""}
            state = await self._vlm(frame, command)
            self._bus.deposit_visual(str(state or ""), h)
            self.stats["vlm_calls"] += 1
            return {
                "verdict": VERDICT_ANALYZED,
                "semantic_state": str(state or ""),
            }
        except Exception:  # noqa: BLE001
            logger.debug("[SemanticGaze] request degraded", exc_info=True)
            return {"verdict": VERDICT_DORMANT, "semantic_state": ""}


__all__ = [
    "VERDICT_ANALYZED",
    "VERDICT_CACHED",
    "VERDICT_DORMANT",
    "VERDICT_THERMAL_LOCKED",
    "SemanticGaze",
    "has_visual_intent",
]
