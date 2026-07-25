"""Which agent is speaking — and therefore, in whose voice.

This is a multi-agent ecosystem and the voices are not interchangeable. JARVIS
speaks as Daniel (en_GB); Karen is O+V's voice and speaks as Karen (en_AU),
which is not a coincidence — her system prompt describes "a terse, senior
Australian engineer", and the persona was written around that voice.

The defect this closes
----------------------
``MacOSVoice._select_best_voice()`` picks a voice from a British-first
preference list with no idea which agent it is speaking for. Every process
that mounted TTS therefore got JARVIS's voice, including the `ov` cockpit,
whose audio host logged exactly that on boot::

    RealTimeVoiceCommunicator initialized with voice: Daniel

Karen's cockpit, speaking in JARVIS's voice. The synthesis layer had no
concept of WHO it was synthesizing for, so there was no seam at which the
right answer could even be expressed.

Why an enum and not a voice name
--------------------------------
Binding a name into the audio host would hardcode a macOS-specific string into
a layer that has no business knowing about macOS voices, and would break the
moment a persona moves to a different engine. A persona is an IDENTITY; a
voice is one platform's rendering of it. The pipeline injects the identity and
this module resolves the rendering.

Resolution is a chain, not a constant:

  1. an explicit operator override (``JARVIS_VOICE_KAREN=Serena``);
  2. the persona's ordered preference list, filtered by what is ACTUALLY
     installed — a preference for a voice the machine does not have is not a
     preference, it is a silent failure;
  3. the first installed voice of the persona's preferred locale;
  4. ``None`` — the caller keeps its existing default rather than guessing.

Every step is checked against the live inventory from ``say -v ?``, so a
machine without Karen installed degrades to a real voice instead of failing to
synthesize.
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


#: Sentinel meaning "whatever voice macOS is configured to use" — `say` with
#: no ``-v`` at all. Not a voice NAME, and deliberately so: it follows the
#: operator's System Settings instead of pinning a name that goes stale the
#: moment they change it, and it is the only value that stays correct across
#: machines with different voices installed.
SYSTEM_DEFAULT = "system"

_SYSTEM_ALIASES = ("system", "default", "system_default", "os", "")


def _is_system_default(name: object) -> bool:
    return str(name or "").strip().lower() in _SYSTEM_ALIASES


class AgentPersona(str, enum.Enum):
    """Who is speaking. The value is the env-knob suffix and log token."""

    KAREN = "karen"        # O+V's voice — the ov cockpit
    JARVIS = "jarvis"      # the supervisor / Body
    SYSTEM = "system"      # unattributed system speech

    @classmethod
    def coerce(cls, value: object) -> Optional["AgentPersona"]:
        """Best-effort parse. Returns None rather than raising, so a bad
        persona costs the default voice and never a spoken reply."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return None


#: Ordered voice preferences per persona. ORDERED LISTS, not single names:
#: the first installed entry wins, so a machine missing the ideal voice still
#: gets a deliberate second choice rather than whatever the generic selector
#: happened to pick.
_PREFERENCES: Dict[AgentPersona, Tuple[str, ...]] = {
    # Australian first — her prompt says so.
    # SYSTEM_DEFAULT first, by operator request 2026-07-25: the voice they
    # actually want for O+V is the one macOS is set to, and asking for "the
    # system voice" is a stable request while asking for a NAME is not — the
    # name is right until they change the setting, and wrong silently after.
    # The named fallbacks stay for machines whose default cannot be resolved.
    AgentPersona.KAREN: (SYSTEM_DEFAULT, "Karen", "Tessa", "Serena", "Samantha"),
    # British first — the established JARVIS voice.
    AgentPersona.JARVIS: ("Daniel", "Oliver", "Serena", "Alex"),
    AgentPersona.SYSTEM: ("Samantha", "Alex", "Daniel"),
}

#: Locale to fall back to when no preferred voice is installed.
_LOCALES: Dict[AgentPersona, str] = {
    AgentPersona.KAREN: "en_AU",
    AgentPersona.JARVIS: "en_GB",
    AgentPersona.SYSTEM: "en_US",
}


@dataclass(frozen=True)
class VoiceProfile:
    """A persona's concrete rendering on this machine."""

    persona: AgentPersona
    voice: str
    rate: int
    source: str          # how it was chosen — for honest logging

    @property
    def is_system_default(self) -> bool:
        """True when this profile defers to the OS-configured voice."""
        return _is_system_default(self.voice)

    def as_say_args(self) -> List[str]:
        """``say`` arguments for this profile.

        The system default is expressed by OMITTING ``-v`` entirely rather
        than passing a resolved name: `say` then uses whatever the operator
        has configured, and stays correct when they change it."""
        if self.is_system_default:
            return ["-r", str(self.rate)]
        return ["-v", self.voice, "-r", str(self.rate)]


# ---------------------------------------------------------------------------
# Live voice inventory
# ---------------------------------------------------------------------------

_INVENTORY_LOCK = threading.Lock()
_INVENTORY: Optional[List[Tuple[str, str]]] = None
_INVENTORY_AT: float = 0.0


def _inventory_ttl_s() -> float:
    try:
        return max(10.0, float(os.getenv("JARVIS_VOICE_INVENTORY_TTL_S", "300")))
    except (TypeError, ValueError):
        return 300.0


def installed_voices(*, force: bool = False) -> List[Tuple[str, str]]:
    """``[(name, locale), ...]`` from ``say -v ?``. Cached, NEVER raises.

    Asked of the SYSTEM rather than assumed, because a preference for a voice
    the machine does not have is not a preference — it is a silent failure at
    synthesis time, which is the failure mode this whole module exists to
    remove."""
    global _INVENTORY, _INVENTORY_AT
    with _INVENTORY_LOCK:
        fresh = (
            _INVENTORY is not None
            and (time.time() - _INVENTORY_AT) < _inventory_ttl_s()
        )
        if fresh and not force:
            return list(_INVENTORY or ())
    parsed: List[Tuple[str, str]] = []
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            # "Karen               en_AU    # Hello! ..." — the locale is the
            # first token containing an underscore, because voice NAMES can
            # contain spaces ("Bad News", "Grandma (Enhanced)").
            loc_i = next(
                (i for i, p in enumerate(parts) if "_" in p and len(p) <= 6),
                None,
            )
            if loc_i is None or loc_i == 0:
                continue
            parsed.append((" ".join(parts[:loc_i]), parts[loc_i]))
    except Exception:  # noqa: BLE001 — no inventory is survivable
        logger.debug("[Persona] voice inventory unavailable", exc_info=True)
    with _INVENTORY_LOCK:
        _INVENTORY = parsed
        _INVENTORY_AT = time.time()
    return list(parsed)


def voice_installed(name: str) -> bool:
    n = (name or "").strip().lower()
    return any(v.lower() == n for v, _ in installed_voices())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _override_for(persona: AgentPersona) -> str:
    """``JARVIS_VOICE_KAREN`` / ``JARVIS_VOICE_JARVIS`` / ... — the operator's
    explicit choice, which always wins."""
    return os.getenv(f"JARVIS_VOICE_{persona.value.upper()}", "").strip()


def _rate_for(persona: AgentPersona) -> int:
    try:
        return max(80, int(os.getenv(
            f"JARVIS_VOICE_RATE_{persona.value.upper()}",
            os.getenv("JARVIS_VOICE_RATE", "175"),
        )))
    except (TypeError, ValueError):
        return 175


def resolve_profile(persona: object) -> Optional[VoiceProfile]:
    """The voice *persona* should speak in on THIS machine, or None.

    ``None`` means "no opinion — keep your default": an unknown persona or a
    machine with no usable voice must not silence a reply, and guessing a
    voice would be worse than the caller's existing behaviour. NEVER raises."""
    p = AgentPersona.coerce(persona)
    if p is None:
        return None
    try:
        rate = _rate_for(p)

        override = _override_for(p)
        if _is_system_default(override) and override:
            return VoiceProfile(p, SYSTEM_DEFAULT, rate, "operator_override")
        if override:
            # Honour it even if the inventory lookup failed — the operator may
            # know about a voice this parse could not see.
            if voice_installed(override) or not installed_voices():
                return VoiceProfile(p, override, rate, "operator_override")
            logger.warning(
                "[Persona] %s override %r is not installed — falling back",
                p.value, override,
            )

        for name in _PREFERENCES.get(p, ()):
            if _is_system_default(name):
                return VoiceProfile(p, SYSTEM_DEFAULT, rate, "system_default")
            if voice_installed(name):
                return VoiceProfile(p, name, rate, "preference")

        locale = _LOCALES.get(p, "")
        for name, loc in installed_voices():
            if loc == locale:
                return VoiceProfile(p, name, rate, f"locale:{locale}")

        return None
    except Exception:  # noqa: BLE001
        logger.debug("[Persona] resolve degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Synthesis latency — measured, because voices differ by 3x
# ---------------------------------------------------------------------------


def synthesis_budget_ms() -> float:
    """How long a voice may take to synthesize a short reply before it stops
    feeling like a conversation.

    Measured on this machine for one sentence: Daniel 1012ms, Karen 1050ms,
    Samantha 1644ms, Alex 2702ms, and the SYSTEM DEFAULT 3158ms — the slowest
    installed. A three-second pause before "hello" is not a slow assistant,
    it is a broken conversational contract, and no amount of LLM speed hides
    it because synthesis happens after."""
    try:
        return max(100.0, float(os.getenv("JARVIS_VOICE_SYNTH_BUDGET_MS", "2500")))
    except (TypeError, ValueError):
        return 2500.0


def measure_synthesis_ms(voice: str, *, text: str = "Hello. How can I help?") -> float:
    """Milliseconds for *voice* to synthesize *text* to a file, or -1.

    An empty voice name measures the system default (no ``-v``), which is the
    only way to time a voice whose name cannot be resolved. NEVER raises."""
    import tempfile
    try:
        fd, path = tempfile.mkstemp(suffix=".aiff")
        os.close(fd)
        args = ["say"] + ([] if _is_system_default(voice) else ["-v", voice])
        t0 = time.monotonic()
        proc = subprocess.run(
            args + ["-o", path, text], capture_output=True, timeout=20,
        )
        elapsed = (time.monotonic() - t0) * 1000.0
        size = os.path.getsize(path)
        os.unlink(path)
        if proc.returncode != 0 or size < 1024:
            return -1.0
        return elapsed
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1.0


def fastest_acceptable_voice(persona: AgentPersona) -> Optional[VoiceProfile]:
    """The persona's preferred voice, unless it is too slow to converse with.

    Walks the SAME preference chain as :func:`resolve_profile` and returns the
    first entry that both exists and synthesizes inside the budget. Falls back
    to the plain resolution when nothing measures fast enough, because a slow
    voice still beats no voice — the identical judgement the DW voice lane
    makes about models.

    Measurement is real, not assumed: voices differ by 3x on this machine and
    a table of which is fast would rot the moment Apple ships a new one."""
    budget = synthesis_budget_ms()
    try:
        override = _override_for(persona)
        chain = ([override] if override else []) + list(
            _PREFERENCES.get(persona, ())
        )
        for name in chain:
            if not _is_system_default(name) and not voice_installed(name):
                continue
            ms = measure_synthesis_ms(name)
            if ms < 0:
                continue
            if ms <= budget:
                return VoiceProfile(
                    persona,
                    SYSTEM_DEFAULT if _is_system_default(name) else name,
                    _rate_for(persona),
                    f"measured:{ms:.0f}ms",
                )
            logger.info(
                "[Persona] %s synthesizes in %.0fms, over the %.0fms "
                "conversational budget — trying the next preference",
                name or "<system default>", ms, budget,
            )
        return resolve_profile(persona)
    except Exception:  # noqa: BLE001 — selection must never block speech
        return resolve_profile(persona)


def active_persona() -> Optional[AgentPersona]:
    """Process-wide persona from the environment.

    The audio host sets this at boot so every synthesis site in the process
    inherits the identity without each one having to be threaded — the
    alternative is a persona argument on every call, and the sites that got
    missed would silently keep the wrong voice, which is the bug being fixed."""
    return AgentPersona.coerce(os.getenv("JARVIS_AGENT_PERSONA", ""))


def latency_selection_enabled() -> bool:
    """Measure synthesis speed when choosing a voice? Default ON.

    OFF pins whatever the preference chain names regardless of how slow it
    is, which is the right choice for an operator who cares more about the
    voice than the pause before it."""
    return os.getenv(
        "JARVIS_VOICE_LATENCY_SELECTION", "true",
    ).strip().lower() in _TRUTHY


def bind_persona(persona: AgentPersona) -> None:
    """Declare the persona for this process. Idempotent; NEVER raises."""
    try:
        os.environ["JARVIS_AGENT_PERSONA"] = AgentPersona(persona).value
        prof = (
            fastest_acceptable_voice(persona) if latency_selection_enabled()
            else resolve_profile(persona)
        )
        logger.info(
            "[Persona] bound %s -> voice=%s (%s)",
            persona.value,
            prof.voice if prof else "<default>",
            prof.source if prof else "unresolved",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Persona] bind degraded", exc_info=True)


__all__ = [
    "SYSTEM_DEFAULT",
    "fastest_acceptable_voice",
    "measure_synthesis_ms",
    "synthesis_budget_ms",
    "AgentPersona",
    "VoiceProfile",
    "active_persona",
    "bind_persona",
    "installed_voices",
    "resolve_profile",
    "voice_installed",
]
