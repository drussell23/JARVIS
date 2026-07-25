"""The persona matrix — which agent is speaking, and how it is rendered.

Two entities coexist in JARVIS-PRIME and they are not interchangeable:

    JARVIS  — the Body. British, understated. Rendered by the `Daniel` voice.
    O+V     — Ouroboros + Venom, the self-developing organism. Terse
              Australian engineer. Rendered by the `Karen` voice.

The distinction this module finally makes
-----------------------------------------
"Karen" has been used to mean both the AGENT and the VOICE, and that conflation
is why identity kept leaking: four synthesis sites each decided a voice, and
the conversation prompt separately decided a name, and nothing owned the pair.
An agent is an IDENTITY with a name, a character and a wake word; a voice is
one platform's rendering of it. ``AgentPersona.OV`` is the agent;
``Karen`` is what it sounds like on macOS.

``AgentPersona.KAREN`` is retained as an alias so nothing that already binds it
breaks, but OV is the canonical name for the entity.

Wake-word routing
-----------------
The active agent is selected from what the operator actually SAID rather than
from configuration: address "Karen" and O+V answers, address "JARVIS" and the
Body does. Both wake words are drawn from the same registry entries that
supply the voice and the prompt, so adding an agent is one entry — not an
entry plus a routing branch plus a voice mapping, which is exactly the shape
that let voice and identity drift apart in the first place.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from backend.voice.agent_persona import (
    AgentPersona,
    VoiceProfile,
    resolve_profile,
    system_prompt_for,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    """Everything that makes one agent itself.

    Frozen: an agent's identity is not something a caller should be able to
    edit at runtime, and the drift this module exists to prevent came from
    exactly that kind of per-site mutation."""

    persona: AgentPersona
    display_name: str
    wake_words: Tuple[str, ...]
    #: Preferred macOS voice. The registry states the INTENT; the persona
    #: resolver decides what is actually installed and fast enough, so a
    #: machine without this voice degrades rather than failing.
    preferred_voice: str

    @property
    def wake_pattern(self) -> "re.Pattern[str]":
        """Whole-word alternation over this agent's wake words.

        Word-boundaried, because the substring lesson has been learned
        expensively in this codebase: 'ov' inside 'over', 'karen' inside a
        longer token, and — most memorably — 'rain' inside 'brain' opening
        the Weather app."""
        alt = "|".join(re.escape(w) for w in self.wake_words)
        return re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)


#: THE MATRIX. One entry per agent; voice, prompt and wake word all hang off
#: it, so they cannot be changed independently and drift.
_AGENTS: Dict[AgentPersona, AgentSpec] = {
    AgentPersona.OV: AgentSpec(
        persona=AgentPersona.OV,
        display_name="Karen",
        wake_words=("karen", "o+v", "ouroboros"),
        preferred_voice="Karen",
    ),
    AgentPersona.JARVIS: AgentSpec(
        persona=AgentPersona.JARVIS,
        display_name="JARVIS",
        wake_words=("jarvis", "javis", "jervis"),   # common STT mishearings
        preferred_voice="Daniel",
    ),
}


def all_agents() -> Tuple[AgentSpec, ...]:
    return tuple(_AGENTS.values())


def spec_for(persona: object) -> Optional[AgentSpec]:
    """The registry entry for *persona*, or None. NEVER raises."""
    p = AgentPersona.coerce(persona)
    if p is None:
        return None
    if p in _AGENTS:
        return _AGENTS[p]
    # KAREN is an alias for OV — the agent, not the voice.
    if p is AgentPersona.KAREN:
        return _AGENTS.get(AgentPersona.OV)
    return None


def route_by_wake_word(transcript: str) -> Optional[AgentSpec]:
    """Which agent was ADDRESSED, from what the operator said.

    Returns None when no agent was named — the caller keeps whoever is
    already active, because a turn that names nobody is a continuation of the
    current conversation, not a request to switch. Silently reassigning on
    every unnamed turn would make the active agent depend on sentence
    structure rather than on intent.

    When two agents are named, the one appearing FIRST wins: "Karen, ask
    JARVIS to…" addresses Karen about JARVIS. NEVER raises."""
    try:
        text = str(transcript or "")
        if not text.strip():
            return None
        best: Optional[Tuple[int, AgentSpec]] = None
        for spec in _AGENTS.values():
            m = spec.wake_pattern.search(text)
            if m is not None and (best is None or m.start() < best[0]):
                best = (m.start(), spec)
        return best[1] if best else None
    except (TypeError, re.error):
        return None


def voice_profile_for(persona: object) -> Optional[VoiceProfile]:
    """Resolved voice for *persona*, honouring the registry's preference.

    DRY: the registry says WHICH voice the agent should have; the persona
    resolver still decides whether it is installed, whether an operator
    override supersedes it, and whether it synthesizes fast enough. Two
    layers, one question each."""
    spec = spec_for(persona)
    if spec is None:
        return None
    return resolve_profile(spec.persona, prefer=spec.preferred_voice)


def prompt_for(persona: object) -> Optional[str]:
    """Identity prompt for *persona* — the same seam the pipeline uses."""
    spec = spec_for(persona)
    return system_prompt_for(spec.persona) if spec else None


__all__ = [
    "AgentSpec",
    "all_agents",
    "prompt_for",
    "route_by_wake_word",
    "spec_for",
    "voice_profile_for",
]
