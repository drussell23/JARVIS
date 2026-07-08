# backend/core/ouroboros/governance/comms/karen_boot_briefing.py
"""Karen's boot briefing -- the awakening's voice (spec §3.3).

Live vectors -> Sprint-2 speech pipeline (LedgerView filters -> persona ->
DW -> sentence chunks -> arbiter) behind a hard asyncio.wait_for breaker.
Fallback is a LOCAL deterministic composition over the SAME vectors --
state-driven prose, never canned boilerplate. Fire-and-forget by contract:
run() NEVER raises and never blocks boot beyond its own awaited deadline.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from .karen_synth.ledger_view import LedgerView, strip_code

logger = logging.getLogger("Ouroboros.Karen.BootBriefing")

_TIMEOUT_ENV = "JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S"


@dataclass
class BriefingVectors:
    last_session: Optional[str] = None
    queued_ops: Optional[int] = None
    posture: Optional[str] = None
    pending_approvals: Optional[int] = None

    @property
    def empty(self) -> bool:
        return all(v is None for v in
                   (self.last_session, self.queued_ops, self.posture,
                    self.pending_approvals))


async def gather_vectors(*, intake_probe: Optional[Callable[[], Optional[int]]] = None,
                         approvals_probe: Optional[Callable[[], Optional[int]]] = None,
                         ) -> BriefingVectors:
    """Best-effort, read-only, per-vector fault isolation. NEVER raises."""
    v = BriefingVectors()
    loop = asyncio.get_event_loop()
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )
        v.last_session = await loop.run_in_executor(
            None, get_default_summary().format_for_prompt_sync)
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] last_session vector unavailable", exc_info=True)
    try:
        from backend.core.ouroboros.governance.posture_palette import (
            read_current_posture_safe,
        )
        reading = await loop.run_in_executor(None, read_current_posture_safe)
        if reading is not None:
            value = getattr(reading, "value", reading)
            v.posture = str(value or "") or None
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] posture vector unavailable", exc_info=True)
    for probe, attr in ((intake_probe, "queued_ops"),
                        (approvals_probe, "pending_approvals")):
        if probe is None:
            continue
        try:
            setattr(v, attr, probe())
        except Exception:  # noqa: BLE001
            logger.debug("[briefing] %s vector unavailable", attr, exc_info=True)
    return v


def compose_local(v: BriefingVectors) -> str:
    """Deterministic prose over LIVE vectors (the breaker's landing pad).
    Never a canned greeting: every clause carries real state."""
    if v.empty:
        return "Awake. First session on this repo."
    parts: List[str] = ["Awake."]
    if v.queued_ops:
        plural = "s" if v.queued_ops != 1 else ""
        parts.append(f"{v.queued_ops} op{plural} queued overnight.")
    if v.pending_approvals:
        plural = "s" if v.pending_approvals != 1 else ""
        parts.append(f"{v.pending_approvals} awaiting your sign-off.")
    if v.posture:
        parts.append(f"Posture {v.posture}.")
    if v.last_session and len(parts) == 1:
        parts.append(f"Last session: {strip_code(v.last_session)[:120]}.")
    return " ".join(parts)


class BootBriefing:
    def __init__(self, *, synthesizer: Optional[object] = None,
                 speak_sink: Optional[Callable[[str], None]] = None,
                 text_sink: Optional[Callable[[str], None]] = None,
                 timeout_s: Optional[float] = None,
                 vectors_override: Optional[BriefingVectors] = None) -> None:
        self._synth = synthesizer
        self._speak = speak_sink
        self._text = text_sink
        self._timeout = timeout_s if timeout_s is not None else float(
            os.environ.get(_TIMEOUT_ENV, "4.0"))
        self._vectors_override = vectors_override

    async def run(self) -> str:
        """Deliver the briefing. NEVER raises. Bounded by the breaker."""
        try:
            v = self._vectors_override if self._vectors_override is not None \
                else await gather_vectors()
            line = ""
            if self._synth is not None:
                line = await self._primary(v)
            if not line:
                line = compose_local(v)
                self._deliver(line)
            return line
        except Exception:  # noqa: BLE001
            logger.debug("[briefing] run failed", exc_info=True)
            return ""

    async def _primary(self, v: BriefingVectors) -> str:
        """DW path under the breaker. Returns "" on any failure/timeout."""
        payload = {"goal": compose_local(v)}
        view = LedgerView.from_payload("boot", payload)
        spoken: List[str] = []

        async def _consume() -> None:
            async for sentence in self._synth.synthesize(view):
                safe = strip_code(sentence).strip()
                if safe:
                    spoken.append(safe)
                    self._deliver(safe)

        task = asyncio.get_event_loop().create_task(_consume())
        try:
            # asyncio.wait_for raises asyncio.TimeoutError (an Exception) on its
            # own deadline — NEVER CancelledError. So we deliberately do NOT
            # catch CancelledError here: a genuine OUTER cancellation (boot
            # shutdown while we await DW) MUST propagate out of run(), not be
            # eaten by the breaker's local-fallback path.
            await asyncio.wait_for(task, timeout=self._timeout)
        except Exception:  # noqa: BLE001  (asyncio.TimeoutError is a subclass)
            task.cancel()
            try:
                # Awaiting the child we just cancelled legitimately raises
                # CancelledError HERE — it is ours to absorb, not the caller's.
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if not spoken:
                logger.debug("[briefing] breaker tripped -> local fallback")
                return ""
        return " ".join(spoken)

    def _deliver(self, line: str) -> None:
        for sink in (self._speak, self._text):
            if sink is None:
                continue
            try:
                sink(line)
            except Exception:  # noqa: BLE001
                logger.debug("[briefing] sink failed", exc_info=True)
            return                        # speak wins; text is the fallback


def build_default_synthesizer() -> Optional[object]:
    """Best-effort production synthesizer (DW -> Karen). None on any failure
    -- the breaker's local path covers it."""
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )
        from .karen_synth.speech_provider import DWSpeechProvider
        from .karen_synth.synthesizer import KarenSpeechSynthesizer
        if not os.environ.get("DOUBLEWORD_API_KEY"):
            return None
        return KarenSpeechSynthesizer(DWSpeechProvider(DoublewordProvider()))
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] default synthesizer unavailable", exc_info=True)
        return None


__all__ = ["BriefingVectors", "BootBriefing", "gather_vectors",
           "compose_local", "build_default_synthesizer"]
