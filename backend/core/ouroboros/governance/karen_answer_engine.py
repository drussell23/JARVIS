"""Karen Answer Engine — grounded, narrated, policy-routed Q&A.

Operator mandate 2026-07-18: "not simulation crap." When the operator
asks *"what was the last thing I worked on?"* Karen must (1) SHOW her
thinking live in the CLI (Gemini-style progress, in our glyph grammar),
(2) ground the answer in the ACTUAL codebase/session state — not
model imagination — and (3) answer through the same provider policy
lane everything else uses.

Three organs, all composition:

* **Grounding pack** — a bounded organism-state digest composed from
  EXISTING read-only surfaces: the LastSessionSummary operator plane
  (what actually happened last sessions), recent git subjects (what
  actually landed), live posture + phase. "What did I last work on"
  is answered from evidence.
* **KarenQueryProvider** — a ``ClaudeQueryProvider`` implementation
  that plugs into the EXISTING ClaudeChatActionExecutor (its cost caps
  + audit ride along untouched). Routing rides ``rt_gate.gate_completion``
  — Claude-RT first, DW fallback — the SAME lane Karen's boot-briefing
  synthesis uses. One policy brain for everything Karen says.
* **Progress narration** — every stage emits one design-language line
  through the injected sink (the ``_repl_print`` chokepoint → router
  conformance + attach-terminal mirror for free): the operator is
  never left in the dark between Enter and the answer.
* **speak_answer** — the voice tap: first spoken-sized sentence to the
  mounted Karen duplex (supervisor-owned speakers); silent no-op when
  voice isn't mounted. Deterministic compression for v1 — zero extra
  LLM spend to talk.

NEVER raises anywhere; a failed stage degrades to an honest
"couldn't reach a provider" answer, never a traceback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

_SYSTEM_PROMPT = (
    "You are Karen, the voice of O+V (Ouroboros + Venom) — an autonomous "
    "self-developing engineering organism. You are a terse, senior "
    "Australian engineer: plain words, no filler, no bullet spam. "
    "Answer ONLY from the organism-state context provided and general "
    "engineering knowledge; if the context doesn't contain the answer, "
    "say so plainly. Two to five sentences."
)


def _answer_max_tokens() -> int:
    try:
        return max(64, int(os.environ.get(
            "JARVIS_KAREN_ANSWER_MAX_TOKENS", "400",
        )))
    except (TypeError, ValueError):
        return 400


def _grounding_cap_chars() -> int:
    try:
        return max(400, int(os.environ.get(
            "JARVIS_KAREN_GROUNDING_CAP_CHARS", "2400",
        )))
    except (TypeError, ValueError):
        return 2400


# ---------------------------------------------------------------------------
# Grounding pack — evidence, not imagination
# ---------------------------------------------------------------------------


def build_grounding_pack() -> str:
    """Bounded organism-state digest from existing read-only surfaces.
    Every source is independently defensive; a dead source contributes
    nothing rather than an error. NEVER raises."""
    sections = []
    # (1) What the last sessions actually did — the LSS operator plane.
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )
        digest = get_default_summary().operator_digest_sync()
        if digest:
            sections.append("recent sessions:\n" + digest)
    except Exception:  # noqa: BLE001
        pass
    # (2) What actually landed — recent git subjects.
    try:
        import subprocess
        out = subprocess.run(
            ["git", "log", "--oneline", "-6", "--no-decorate"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            sections.append("recent commits:\n" + out.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    # (3) Live stance — posture + phase + liquidity headline.
    try:
        from backend.core.ouroboros.battle_test.status_line import (
            get_status_line_builder,
        )
        b = get_status_line_builder()
        if b is not None:
            snap = b.snapshot()
            sections.append(
                f"live state: phase={snap.phase} "
                f"cost=${snap.cost_spent_usd:.2f}/"
                f"${snap.cost_budget_usd:.2f}"
            )
    except Exception:  # noqa: BLE001
        pass
    pack = "\n\n".join(sections)
    return pack[: _grounding_cap_chars()]


# ---------------------------------------------------------------------------
# The provider — plugs into ClaudeChatActionExecutor's seam
# ---------------------------------------------------------------------------


class KarenQueryProvider:
    """``ClaudeQueryProvider`` impl: narrated + grounded + rt_gate-routed.

    Runs inside the chat multiplexer's worker thread (no running loop),
    so the async rt_gate lane executes on a thread-local loop via
    ``asyncio.run`` — the REPL event loop is never touched."""

    def __init__(
        self,
        progress_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._progress = progress_sink or (lambda _s: None)

    def _emit(self, line: str) -> None:
        try:
            self._progress(line)
        except Exception:  # noqa: BLE001
            pass

    def query(self, prompt: str, max_tokens: int = 0) -> str:
        """The executor hands us its composed prompt (message + recent
        turns); we prepend evidence and route through the policy lane,
        narrating each stage. NEVER raises — degrades to an honest
        can't-reach-provider answer."""
        try:
            self._emit("⎿ thinking · gathering organism context")
            pack = build_grounding_pack()
            grounded = (
                ("## Organism state (evidence — answer from this)\n"
                 f"{pack}\n\n" if pack else "")
                + prompt
            )
            # ADAPTIVE LANE. The right provider is a property of the
            # PAYLOAD, not of policy: a two-word question on DW's async
            # tier waits ~60s for its first token, and a 40k-token dump
            # on Claude buys latency nobody asked for at ~10x the price.
            # Measured on the GROUNDED prompt — what actually goes on the
            # wire, attachments and evidence included — so the estimate
            # cannot drift from the request.
            from backend.core.ouroboros.governance.adaptive_lane_router import (  # noqa: E501
                route as _route_lane,
            )
            _decision = _route_lane(
                grounded, _SYSTEM_PROMPT, notify=self._emit,
            )
            if not _decision.is_heavy:
                self._emit("⎿ thinking · asking claude (doubleword fallback)")
            from backend.core.ouroboros.governance.rt_gate import (
                gate_completion,
            )
            # Karen SPEAKS her answers, so her DW tier is a conversation lane,
            # not a code lane. Left unset it inherits DOUBLEWORD_MODEL — the
            # 397B code brain, measured at 22.8s to first token for a one-
            # sentence reply. The elected model comes from measurement
            # (karen_voice_lane), and ``None`` means "no evidence yet, keep the
            # default" — the lane never guesses.
            from backend.core.ouroboros.governance.karen_voice_lane import (
                ensure_voice_lane_warm, resolve_voice_model,
            )
            _voice_model = resolve_voice_model()
            if _voice_model is None:
                # Cold lane: learn in the background so the NEXT turn is fast.
                # Blocking here to elect a faster voice would make the first
                # reply slower to make later ones quicker — self-defeating.
                ensure_voice_lane_warm()
            if _voice_model:
                self._emit(f"⎿ voice lane · {_voice_model}")
            answer = asyncio.run(gate_completion(
                grounded,
                caller_id="karen_chat_answer",
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=max_tokens or _answer_max_tokens(),
                dw_model=_voice_model,
                # Bias only — the other tier stays the fallback, so a
                # heavy query still gets answered when DW is down.
                prefer=_decision.prefer,
            ))
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            return (
                "I got an empty reply from every provider — ask me again "
                "in a moment."
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[KarenAnswer] query degraded", exc_info=True)
            self._emit("⎿ providers unreachable")
            return (
                "I couldn't reach a provider just now "
                f"({type(exc).__name__}) — my windows may be dry. "
                "Try again shortly."
            )


# ---------------------------------------------------------------------------
# Voice tap — speak the short version, never block, never break
# ---------------------------------------------------------------------------


_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def spoken_digest(answer: str, *, cap: int = 140) -> str:
    """First spoken-sized sentence of *answer* — deterministic
    compression, zero LLM spend. Pure; NEVER raises."""
    try:
        text = " ".join(str(answer or "").split())
        if not text:
            return ""
        first = _SENTENCE_END.split(text, 1)[0]
        return first[:cap].rstrip()
    except Exception:  # noqa: BLE001
        return ""


def speak_answer(answer: str) -> bool:
    """Hand the spoken digest to the mounted Karen duplex. True when a
    line was actually submitted; silent False when voice isn't mounted
    (no supervisor / voice master off). NEVER raises."""
    try:
        digest = spoken_digest(answer)
        if not digest:
            return False
        from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (  # noqa: E501
            get_default_karen,
        )
        karen = get_default_karen()
        if karen is None:
            return False
        karen.submit_speech(digest)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "KarenQueryProvider",
    "build_grounding_pack",
    "speak_answer",
    "spoken_digest",
]
