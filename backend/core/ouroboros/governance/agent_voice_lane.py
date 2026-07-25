"""One voice lane per agent — the generalisation of ``karen_voice_lane``.

What was wrong with the old shape
---------------------------------
``karen_voice_lane`` elects which DoubleWord model Karen SPEAKS through, by
measuring TTFT against a conversational budget. Everything in it was correct
and none of it was Karen-specific: the ledger, the probe, the two-tier
election, the runtime demotion path. Only the *bindings* were — one hardcoded
env namespace, one hardcoded ledger file, one hardcoded probe persona.

So a second agent had exactly two options, and both were bad: share Karen's
lane (JARVIS then inherits an election measured against Karen's prompt and
budget, and either agent's runtime demotion silently mutes the other), or fork
the module (two copies of the ledger, the probe, the election and the demotion
path — and the copy drifts from the original the first time either is fixed).

This module takes the third option. ``karen_voice_lane`` is now parameterised
by ``prefix`` and ``ledger``; this class BINDS those parameters to a persona
drawn from :mod:`backend.voice.agent_registry`. There is no second
implementation of anything, and no logic here at all — only binding.

    lane = lane_for(AgentPersona.JARVIS)
    lane.resolve_model()      # JARVIS's elected DW model
    lane.voice_profile()      # -> Daniel
    lane.system_prompt()      # -> the JARVIS identity prompt

Why the probe is persona-bound
------------------------------
The probe measures time-to-first-token for a real spoken turn. The prompt is
part of that measurement — a longer system prompt is more tokens to prefill —
so grading JARVIS's models with "Hey Karen, are you there?" measures the wrong
workload. Each lane probes with its own identity and its own wake word.

Backward compatibility
----------------------
``AgentPersona.OV``/``KAREN`` resolve to the legacy ``JARVIS_KAREN_VOICE_*``
namespace and the existing ``.jarvis/karen_voice_lane.json``, so measurements
already on disk are inherited rather than orphaned, and an operator's current
configuration keeps working untouched. New agents get their own file and MAY
override any knob; unset knobs fall back to the legacy ones, so per-agent
configuration is optional rather than mandatory duplication.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance import karen_voice_lane as _lane
from backend.voice.agent_persona import AgentPersona, VoiceProfile

logger = logging.getLogger(__name__)

#: OV keeps the namespace and ledger filename it shipped with.
_LEGACY_PERSONAS = (AgentPersona.OV, AgentPersona.KAREN)


class AgentVoiceLane:
    """A persona bound to the voice-election machinery.

    Holds no election logic. Every method delegates to ``karen_voice_lane``
    with this agent's prefix and ledger — which is what makes the two lanes
    genuinely the same code path rather than two that merely look alike.
    """

    def __init__(self, persona: AgentPersona) -> None:
        self.persona = persona
        self._ledger: Optional[_lane.VoiceLatencyLedger] = None
        self._ledger_lock = threading.Lock()

    # -- identity -------------------------------------------------------

    @property
    def spec(self) -> Any:
        """Registry entry — voice, wake words, display name. Imported lazily:
        the registry imports the persona module, and a module-level import
        here would close a cycle through the pipeline."""
        from backend.voice.agent_registry import spec_for
        return spec_for(self.persona)

    @property
    def slug(self) -> str:
        """Filesystem-safe lane id. OV is ``karen`` for continuity."""
        if self.persona in _LEGACY_PERSONAS:
            return "karen"
        return str(getattr(self.persona, "value", self.persona)).lower()

    @property
    def prefix(self) -> str:
        """Env namespace. OV keeps the legacy one; others get their own and
        INHERIT any knob they do not set."""
        if self.persona in _LEGACY_PERSONAS:
            return _lane._LEGACY_PREFIX
        return f"JARVIS_{self.slug.upper()}_VOICE"

    def system_prompt(self) -> Optional[str]:
        from backend.voice.agent_registry import prompt_for
        return prompt_for(self.persona)

    def voice_profile(self) -> Optional[VoiceProfile]:
        from backend.voice.agent_registry import voice_profile_for
        return voice_profile_for(self.persona)

    # -- probe identity -------------------------------------------------

    def _probe_system(self) -> str:
        """This agent's own identity, one spoken-format instruction added.

        Falls back to the module default rather than to nothing: an agent with
        no prompt still needs a probe that exercises a spoken turn."""
        prompt = (self.system_prompt() or "").strip()
        if not prompt:
            return _lane.VOICE_PROBE_SYSTEM
        return f"{prompt}\n\nReply in ONE short spoken sentence. No markdown, no lists."

    def _probe_user(self) -> str:
        spec = self.spec
        name = getattr(spec, "display_name", "") if spec else ""
        return f"Hey {name}, are you there?" if name else _lane.VOICE_PROBE_USER

    # -- ledger ---------------------------------------------------------

    @property
    def ledger(self) -> _lane.VoiceLatencyLedger:
        """This lane's measurements. Lazy + locked: several turns can arrive
        together on different threads, and two ledgers over one file would
        each hold half the evidence."""
        with self._ledger_lock:
            if self._ledger is None:
                self._ledger = _lane.VoiceLatencyLedger(
                    _lane.ledger_path(prefix=self.prefix, slug=self.slug),
                    prefix=self.prefix,
                ).load()
            return self._ledger

    def reset(self) -> None:
        """Test seam — drops the cached ledger handle."""
        with self._ledger_lock:
            self._ledger = None

    # -- the four calls the turn path makes ------------------------------

    def enabled(self) -> bool:
        return _lane.voice_lane_enabled(prefix=self.prefix)

    def resolve_model(self) -> Optional[str]:
        """The DW model THIS agent should speak through, or None.

        Synchronous and allocation-cheap, exactly as the single-agent version
        was: it sits on the turn path."""
        return _lane.resolve_voice_model(ledger=self.ledger, prefix=self.prefix)

    def ensure_warm(self, *, force: bool = False) -> bool:
        return _lane.ensure_voice_lane_warm(
            force=force, prefix=self.prefix, ledger=self.ledger,
        )

    async def refresh(
        self, *, models: Optional[List[str]] = None, force: bool = False,
        dispatch_fn: Any = None,
    ) -> Optional[str]:
        return await _lane.refresh_voice_lane(
            models=models, force=force, dispatch_fn=dispatch_fn,
            ledger=self.ledger, prefix=self.prefix,
            probe_system=self._probe_system(), probe_user=self._probe_user(),
        )

    def record_failure(self, model: str, reason: str = "runtime") -> bool:
        """Demote a model that failed on a REAL turn — for THIS agent only.

        Per-lane on purpose: a model that goes mute under Karen's prompt has
        said nothing about how it behaves under JARVIS's, and a shared ledger
        would let one agent's bad turn silence the other."""
        return _lane.record_runtime_failure(model, reason, ledger=self.ledger)

    def status(self) -> Dict[str, Any]:
        st = _lane.voice_lane_status(prefix=self.prefix, ledger=self.ledger)
        spec = self.spec
        st["agent"] = getattr(spec, "display_name", self.slug) if spec else self.slug
        st["persona"] = str(getattr(self.persona, "value", self.persona))
        profile = self.voice_profile()
        st["voice"] = getattr(profile, "voice", None) if profile else None
        return st


# ---------------------------------------------------------------------------
# Registry of lanes — one per persona, created on demand
# ---------------------------------------------------------------------------

_LANES: Dict[AgentPersona, AgentVoiceLane] = {}
_LANES_LOCK = threading.Lock()


def lane_for(persona: object = None) -> AgentVoiceLane:
    """The lane for *persona*, defaulting to whoever is active.

    Never returns None: a caller on the turn path needs a lane to ask, and an
    unknown persona should degrade to the legacy OV lane rather than force
    every call site to handle absence."""
    from backend.voice.agent_persona import active_persona

    p = AgentPersona.coerce(persona) if persona is not None else None
    if p is None:
        p = active_persona() or AgentPersona.OV
    if p is AgentPersona.KAREN:
        p = AgentPersona.OV                # alias — one lane, not two
    with _LANES_LOCK:
        lane = _LANES.get(p)
        if lane is None:
            lane = AgentVoiceLane(p)
            _LANES[p] = lane
        return lane


def all_lanes() -> List[AgentVoiceLane]:
    """A lane per registered agent — for ``/provider`` style surfaces."""
    from backend.voice.agent_registry import all_agents
    return [lane_for(spec.persona) for spec in all_agents()]


def reset_lanes() -> None:
    """Test seam — drops every cached lane."""
    with _LANES_LOCK:
        _LANES.clear()


__all__ = [
    "AgentVoiceLane",
    "all_lanes",
    "lane_for",
    "reset_lanes",
]
