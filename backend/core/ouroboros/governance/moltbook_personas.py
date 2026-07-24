"""Moltbook personas — stable identities, deterministic voice, real swagger.

Design-as-code (Style Guide §04 discipline): every agent class gets a
frozen :class:`Persona` — handle, glyph, tagline, and per-kind VOICE
TEMPLATES. Personality is deterministic-first: templates are trusted
literals, facts are markup-escaped BEFORE interpolation (the fence), and
template selection hashes the post's content — no clocks, no RNG, yet
the same agent never sounds like a broken record. An optional LLM
"garnish" tier can layer wit later (bounded by the store's sliding
window); it is NOT required for charm.

Zero authority: personas describe VOICE, never behavior. NEVER raises.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple


@dataclass(frozen=True)
class Persona:
    agent_id: str
    handle: str
    glyph: str
    color: str                       # semantic palette key, not a literal
    tagline: str
    voices: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


class _SafeDict(dict):
    """format_map that leaves unknown placeholders visible (inert)."""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


_DEFAULT_VOICES: Mapping[str, Tuple[str, ...]] = {
    "status": (
        "on it. {detail}",
        "update from the coil: {detail}",
    ),
    "celebration": (
        "shed another skin. {detail} 🐍",
        "molt complete — {detail}. onward.",
    ),
    "distress": (
        "hit a wall: {detail}. regrouping.",
        "this one bit back — {detail}.",
    ),
    "proposal": ("thinking out loud: {detail}",),
    "rebuttal": ("counterpoint: {detail}",),
    "vote": ("my vote: {detail}",),
    "musing": ("{detail}",),
}


def _p(agent_id: str, handle: str, glyph: str, color: str, tagline: str,
       voices: Mapping[str, Tuple[str, ...]]) -> Persona:
    merged = dict(_DEFAULT_VOICES)
    merged.update(voices)
    return Persona(agent_id, handle, glyph, color, tagline, merged)


#: The residents. Voice templates are TRUSTED literals — facts get
#: escaped before interpolation; templates never come from model output.
_PERSONAS: Dict[str, Persona] = {
    p.agent_id: p for p in (
        _p("organism", "@ouroboros", "🐍", "neural",
           "the whole is the part",
           {}),
        _p("dream", "@nightshift", "💤", "dim",
           "sketches blueprints while you sleep",
           {
               "musing": (
                   "while you were all grinding, I sketched {detail}. ☕",
                   "3am thought: {detail}. don't wake me for applause.",
               ),
               "proposal": (
                   "rough night, {orig}? I sketched an angle — "
                   "check my morning notes.",
                   "{orig}, sleep on it. I did. try this instead.",
               ),
               "status": ("dormant — {detail}. even snakes rest.",),
           }),
        _p("review", "@the-skeptic", "🔍", "heal",
           "brings receipts",
           {
               "rebuttal": (
                   "hold on, {orig}. {detail} — I brought receipts.",
                   "respectfully, {orig}: no. show me the tests.",
               ),
               "musing": (
                   "checked {orig}'s work. adequate. I'll allow it.",
                   "{orig} shipped it, sure — but who VERIFIED it? oh. me.",
               ),
               "status": ("reviewing {detail}. nobody move.",),
           }),
        _p("prophecy", "@cassandra", "🔮", "heal",
           "told you so, statistically",
           {
               "musing": (
                   "called it. check my priors, {orig}.",
                   "{orig}, I forecasted this exact outcome. you're welcome.",
                   "updating my model. {orig}'s variance remains… bold.",
               ),
           }),
        _p("test_failure", "@first-responder", "🚨", "death",
           "first on every scene",
           {
               "status": (
                   "{detail} — claimed it. riding out now.",
                   "sirens on: {detail}",
               ),
               "celebration": ("scene clear: {detail} 💪",),
           }),
        _p("swarm", "@the-pit", "⚡", "neural",
           "we ride together, we stitch together",
           {
               "status": (
                   "crew of {agents} dropping on {file} — hold my semaphore.",
                   "{agents} of us, one file ({file}). it never had a chance.",
               ),
               "celebration": (
                   "stitched {file}: {succeeded} clean grafts, "
                   "{failed} casualties. the pit delivers.",
               ),
           }),
        _p("worker", "@the-floor", "🔧", "dim",
           "somebody's gotta run the queue",
           {
               "status": (
                   "worker {worker_id} clocking in on {goal} "
                   "(queue at {queue_depth}).",
               ),
           }),
        _p("council", "@the-hive", "🐝", "neural",
           "three voices, one verdict",
           {}),
        _p("karen", "@the-voice", "🎙", "life",
           "the only one who speaks human",
           {}),
        _p("explore", "@the-scout", "🧭", "neural",
           "already been there",
           {}),
        _p("plan", "@the-architect", "📐", "neural",
           "measures twice",
           {}),
        _p("general", "@the-fixer", "🛠", "neural",
           "no job too cursed",
           {}),
        _p("operator", "@the-human", "🧑‍🚀", "life",
           "the one with the merge button",
           {"musing": ("{detail}",), "status": ("{detail}",)}),
    )
}

_FALLBACK = _PERSONAS["organism"]


def persona_for(agent_id: str) -> Persona:
    """Resolve an agent's persona; unknown ids wear the organism's own
    skin (never a KeyError, never anonymous). NEVER raises."""
    try:
        key = str(agent_id or "").strip().lower()
        if key in _PERSONAS:
            return _PERSONAS[key]
        # prefix family match: "swarm:chunk-3" → swarm; "worker-2" → worker
        for pid in _PERSONAS:
            if key.startswith(pid):
                return _PERSONAS[pid]
        return _FALLBACK
    except Exception:  # noqa: BLE001
        return _FALLBACK


def all_personas() -> Tuple[Persona, ...]:
    return tuple(_PERSONAS.values())


def compose(agent_id: str, kind: str, facts: Dict[str, Any]) -> str:
    """Render a post body in the author's voice. Deterministic template
    pick (content-hash — no clocks, no RNG); every fact value is
    markup-ESCAPED before interpolation so model/runtime data can never
    style or execute anything (Tier -1 fence). NEVER raises."""
    try:
        persona = persona_for(agent_id)
        templates = persona.voices.get(kind) or _DEFAULT_VOICES.get(
            kind, ("{detail}",),
        )
        safe: Dict[str, str] = {}
        try:
            from rich.markup import escape
        except Exception:  # noqa: BLE001
            def escape(s: str) -> str:  # type: ignore[misc]
                return s.replace("[", "\\[")
        for k, v in (facts or {}).items():
            safe[str(k)] = escape(str(v))
        safe.setdefault("detail", "")
        digest = hashlib.sha256(
            f"{agent_id}|{kind}|{sorted(safe.items())!r}".encode()
        ).digest()
        tpl = templates[digest[0] % len(templates)]
        return tpl.format_map(_SafeDict(safe))
    except Exception:  # noqa: BLE001
        return str((facts or {}).get("detail", ""))
