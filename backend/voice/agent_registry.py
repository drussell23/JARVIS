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


# ---------------------------------------------------------------------------
# Dual summon — "Hey JARVIS, ask Karen to verify the deployment"
# ---------------------------------------------------------------------------

#: Verbs that DELEGATE: they take an animate object and hand it a task.
#: This is the structural signal that distinguishes "ask Karen to verify"
#: (Karen is being given work) from "Karen is broken" (Karen is the topic).
#: A closed class, because delegation is a small and stable construction.
_DELEGATION_VERBS = frozenset("""
ask asks tell tells have has get gets ping pings request requests
delegate delegates forward forwards hand hands pass passes loop
check chase escalate escalates route routes
""".split())

#: Tokens that may sit between the verb and its object without breaking the
#: relation ("ask my Karen", "get the JARVIS"). Kept tiny: the further apart
#: they are, the less likely the verb governs that name at all.
_DELEGATION_GAP = frozenset("""a an the my our your that this in to with over up""".split())

#: Coordination, not delegation. "Karen and JARVIS, status" addresses two
#: agents at once — nobody is being handed a task, and simultaneous speech is
#: exactly what the scheduler exists to prevent.
_COORDINATORS = frozenset({"and", "&", "plus", "or"})

#: Complementiser that opens the delegated clause: "ask Karen TO verify".
_TASK_OPENERS = ("to", "that", "if", "whether", "about", "for")

_TOKEN = re.compile(r"[A-Za-z0-9'+]+")


@dataclass(frozen=True)
class Summons:
    """Who was addressed, who was delegated to, and what they were given.

    Roles rather than string halves. The collision this resolves is not
    "two names in one sentence" — it is the absence of ARBITRATION: without
    a designated primary, both agents claim the audio plane and the local
    model at once, and on a 16GB machine that is a memory wall and two
    voices talking over each other."""

    primary: AgentSpec
    secondary: Optional[AgentSpec]
    #: What the secondary was asked to do, verbatim from the transcript.
    delegated_task: str
    #: The utterance as addressed to the primary — the delegation clause
    #: removed, because the primary is not being asked to do that work.
    primary_text: str
    #: Why arbitration decided as it did. Legible, because a wrong call here
    #: sends the wrong agent to the wrong model.
    reason: str

    @property
    def is_dual(self) -> bool:
        return self.secondary is not None


def _occurrences(text: str) -> List[Tuple[int, AgentSpec]]:
    """Every wake-word hit with its position, earliest first."""
    hits: List[Tuple[int, AgentSpec]] = []
    for spec in _AGENTS.values():
        for m in spec.wake_pattern.finditer(text):
            hits.append((m.start(), spec))
    hits.sort(key=lambda h: h[0])
    return hits


def _governing_verb(tokens: List[Tuple[int, str]], name_idx: int) -> str:
    """The delegation verb governing the name at *name_idx*, or "".

    Looks BACKWARD across at most a determiner or two. A verb further away
    than that is not governing this name — it is a different clause, and
    treating it as delegation would hand work to an agent the operator only
    mentioned."""
    steps = 0
    for i in range(name_idx - 1, -1, -1):
        word = tokens[i][1].lower()
        if word in _DELEGATION_VERBS:
            return word
        if word in _DELEGATION_GAP and steps < 2:
            steps += 1
            continue
        return ""
    return ""


def arbitrate(transcript: str) -> Optional[Summons]:
    """Assign PRIMARY and SECONDARY roles to a possibly-dual summon.

    Returns None when no agent was addressed at all.

    The primary is whoever was addressed FIRST — they claim the audio plane
    and answer. A second agent becomes the SECONDARY only when it is the
    object of a delegation verb; otherwise it was merely mentioned, and
    handing work to a mentioned agent would answer a sentence nobody asked.

    Deliberately NOT a regex that splits the sentence in half. A split has no
    idea which side is an instruction and which is a topic, so "Karen, JARVIS
    is down" would summon JARVIS to do nothing. What is parsed here is the
    RELATION — verb governs object governs complement — which is what
    delegation actually is. Pure code, no model call. NEVER raises."""
    try:
        text = str(transcript or "")
        if not text.strip():
            return None
        hits = _occurrences(text)
        if not hits:
            return None

        primary = hits[0][1]
        # Distinct second agent, if any. Repeating one name is emphasis.
        second = next(
            ((pos, spec) for pos, spec in hits if spec.persona is not primary.persona),
            None,
        )
        if second is None:
            return Summons(primary, None, "", text.strip(), "single_agent")

        sec_pos, sec_spec = second
        tokens = [(m.start(), m.group(0)) for m in _TOKEN.finditer(text)]
        name_idx = next(
            (i for i, (pos, _w) in enumerate(tokens) if pos >= sec_pos), -1,
        )
        if name_idx < 0:
            return Summons(primary, None, "", text.strip(), "single_agent")

        prev = tokens[name_idx - 1][1].lower() if name_idx > 0 else ""
        if prev in _COORDINATORS:
            # Both addressed at once. One still has to go first — simultaneous
            # speech is the collision — so the primary answers and the second
            # agent is NOT given work it was never handed.
            return Summons(
                primary, None, "", text.strip(), "coordination_not_delegation",
            )

        verb = _governing_verb(tokens, name_idx)
        if not verb:
            return Summons(primary, None, "", text.strip(), "mention_not_delegation")

        # The delegated clause is what FOLLOWS the name; the primary's own
        # utterance is what precedes the governing verb.
        after = text[tokens[name_idx][0] + len(tokens[name_idx][1]):].strip()
        for opener in _TASK_OPENERS:
            if after.lower().startswith(opener + " "):
                after = after[len(opener) + 1:].strip()
                break
        task = after.strip(" ,.;:!?")

        verb_idx = next(
            (i for i in range(name_idx - 1, -1, -1)
             if tokens[i][1].lower() == verb), name_idx,
        )
        primary_text = text[:tokens[verb_idx][0]].strip(" ,.;:!?")

        return Summons(
            primary, sec_spec, task,
            primary_text or text.strip(),
            f"delegation:{verb}" if task else f"delegation_no_task:{verb}",
        )
    except (TypeError, re.error, ValueError, IndexError):
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
    "Summons",
    "arbitrate",
    "all_agents",
    "prompt_for",
    "route_by_wake_word",
    "spec_for",
    "voice_profile_for",
]
