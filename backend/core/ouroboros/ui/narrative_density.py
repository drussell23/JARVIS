"""How much voice the operator wants — asked once, by everyone who speaks.

`/narrate {off|preambles|on|verbose}` reads as the dial for the organism's
voice. It is not one. It is four `os.environ` writes to a list of producer
flags hardcoded in the verb, and the list is wrong in three separate ways:

    JARVIS_NARRATIVE_DENSITY            0 readers, anywhere in the repo
    JARVIS_NARRATIVE_THINKING_VERBOSE   0 readers, anywhere in the repo
    JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED   1
    JARVIS_NARRATIVE_INTENT_ENABLED         1

So the density value the verb reports back — the one its docstring says it
sets "so subsystem readers see consistent state" — is consumed by nobody,
and `/narrate verbose`'s promise of extended-thinking streams is a write to
a flag no code has ever read.

Meanwhile the Moltbook agora — 13 personas with an autonomous reaction
engine — reads none of them. `/narrate off` silences two voices and leaves
the loudest one talking.

The bug is not the missing Moltbook entry
=========================================
Adding `JARVIS_MOLTBOOK_ENABLED` to the verb's list would fix Moltbook and
leave the class untouched: the next voice added to the organism is silent to
the dial again, and nothing detects it. The verb PUSHES to a list of
producers, so it must be edited every time the organism learns to speak —
and it was not edited even once.

Invert it. This module owns the ladder; producers REGISTER a voice and PULL.
Adding a voice requires no edit here and no edit to the verb, and `/narrate`
can finally answer "what will I actually hear" by reading the roster instead
of asserting it.

    OFF(0) < PREAMBLES(1) < ON(2) < VERBOSE(3)

Ordinal, so a voice declares a threshold and the comparison is total. This is
the same shape as `risk_tier_floor`'s composing knobs, for the same reason:
"strictest wins" needs an order, not a set of booleans.

Four rules, in order
====================
1. **Unknown voices are HEARD.** A dial that mutes what it does not
   recognise silently deletes the output of any subsystem that forgot to
   register — the exact failure this module exists to end, reintroduced
   from the other side. `posture_allows` already resolves an unknown posture
   to SPEAK for the same reason.
2. **Exempt voices bypass the dial.** A `⚔` conflict is REVIEW contesting
   GENERATE: the system reporting that its own components disagree. That is
   not banter and no verbosity preference should be able to suppress it.
   Reuses Moltbook's existing `is_conflict` notion rather than inventing a
   second one.
3. **An explicitly set legacy flag WINS.** Operator specificity beats a
   global dial. Detected by KEY PRESENCE in `os.environ`, never by
   truthiness — `FLAG=false` is an operator decision and `FLAG` unset is
   the absence of one, and a truthiness test cannot tell them apart.
   This is why the verb must stop writing those flags: once the dial writes
   them, every later read looks operator-set and the dial freezes itself at
   whatever it wrote first.
4. Otherwise: audible iff `current_density() >= voice.min_density`.

What this does NOT own
======================
POSTURE. Moltbook already refuses banter under HARDEN, and duplicating that
here would give the same question two answers. Density is the operator's
stated preference; posture is the organism's situational read; both gates
must pass, so "strictest wins" falls out of the AND without a combinator.

NEVER raises. A surface that cannot resolve a preference must still speak:
losing narration is a nuisance, losing the ability to reason about whether
narration happened is the bug being fixed.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.NarrativeDensity")

NARRATIVE_DENSITY_SCHEMA_VERSION = "narrative_density.v1"

#: The one key the dial writes. Everything else is derived or explicit.
DENSITY_ENV_VAR = "JARVIS_NARRATIVE_DENSITY"

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


class Density(enum.IntEnum):
    """How much of the organism's voice reaches the transcript.

    ``IntEnum`` because the whole point is that thresholds compare. A voice
    says "I need at least ON" and the dial answers with ``>=``; an
    unordered enum would push that comparison back into every producer,
    which is how the four-flag shotgun happened in the first place.
    """

    OFF = 0
    PREAMBLES = 1
    ON = 2
    VERBOSE = 3

    @property
    def label(self) -> str:
        return self.name.lower()


#: Matches the verb's existing vocabulary exactly. The vocabulary was never
#: the problem — `/narrate`'s four levels are good and operators know them.
DENSITY_NAMES: Tuple[str, ...] = tuple(d.label for d in Density)

#: Unset resolves here, matching the verb's own "(unset — defaults to 'on')".
DEFAULT_DENSITY = Density.ON


def coerce_density(raw: object, default: Density = DEFAULT_DENSITY) -> Density:
    """Parse a density from anything. NEVER raises.

    Accepts the label, the int, or a `Density`. An unparseable value returns
    ``default`` rather than OFF: a typo in a config must not silence the
    organism, because silence is indistinguishable from a healthy quiet
    system and the operator would have no signal that their value was junk.
    """
    try:
        if isinstance(raw, Density):
            return raw
        if isinstance(raw, bool):          # bool is an int — check first
            return Density.ON if raw else Density.OFF
        if isinstance(raw, int):
            return Density(max(0, min(3, int(raw))))
        text = str(raw or "").strip().lower()
        if not text:
            return default
        for d in Density:
            if text == d.label:
                return d
        if text.isdigit():
            return Density(max(0, min(3, int(text))))
        return default
    except Exception:  # noqa: BLE001
        return default


def current_density() -> Density:
    """The operator's stated preference right now. NEVER raises."""
    try:
        return coerce_density(os.environ.get(DENSITY_ENV_VAR))
    except Exception:  # noqa: BLE001
        return DEFAULT_DENSITY


def set_density(raw: object) -> Density:
    """Set the dial and return what it resolved to. NEVER raises.

    Writes ONE key. The verb's old behaviour — also writing each producer's
    own master flag — is what made rule 3 unusable, because after the first
    `/narrate` every producer flag looked operator-set forever.
    """
    resolved = coerce_density(raw)
    try:
        os.environ[DENSITY_ENV_VAR] = resolved.label
    except Exception:  # noqa: BLE001
        logger.debug("[NarrativeDensity] set degraded", exc_info=True)
    return resolved


@dataclass(frozen=True)
class Voice:
    """One thing that speaks, and the level at which it becomes audible."""

    name: str
    min_density: Density
    #: Pre-existing master flag for this producer. Honoured when EXPLICITLY
    #: present in the environment, so operators and launch scripts that set
    #: it keep working and keep winning over the dial.
    legacy_flag: str = ""
    #: Alarms. Not subject to the dial at any level — see rule 2.
    exempt: bool = False
    describe: str = ""
    owner: str = ""


@dataclass(frozen=True)
class Verdict:
    """Whether a voice is audible, and WHY.

    Never a bare bool, for the same reason `SubmitResult` is not: the whole
    defect being fixed is a dial that could not explain itself. A caller
    that only ever learns "no" cannot tell the operator whether the cause
    was the dial, an explicit flag, or an unregistered producer.
    """

    heard: bool
    reason: str = ""
    voice: Optional[Voice] = None

    def __bool__(self) -> bool:
        return bool(self.heard)


class VoiceRegistry:
    """Every voice that has declared itself. Thread-safe. NEVER raises."""

    def __init__(self) -> None:
        self._voices: Dict[str, Voice] = {}
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        min_density: object = DEFAULT_DENSITY,
        *,
        legacy_flag: str = "",
        exempt: bool = False,
        describe: str = "",
        owner: str = "",
    ) -> Optional[Voice]:
        """Declare a voice. Idempotent — re-registration overrides in place,
        matching `FlagRegistry.bulk_register(override=True)` so a reloaded
        module does not fork the roster."""
        try:
            key = str(name or "").strip().lower()
            if not key:
                return None
            voice = Voice(
                name=key,
                min_density=coerce_density(min_density),
                legacy_flag=str(legacy_flag or "").strip(),
                exempt=bool(exempt),
                describe=str(describe or "").strip(),
                owner=str(owner or "").strip(),
            )
            with self._lock:
                self._voices[key] = voice
            return voice
        except Exception:  # noqa: BLE001
            logger.debug("[NarrativeDensity] register degraded for %r",
                         name, exc_info=True)
            return None

    def get(self, name: str) -> Optional[Voice]:
        try:
            with self._lock:
                return self._voices.get(str(name or "").strip().lower())
        except Exception:  # noqa: BLE001
            return None

    def all(self) -> Tuple[Voice, ...]:
        try:
            with self._lock:
                return tuple(sorted(
                    self._voices.values(),
                    key=lambda v: (int(v.min_density), v.name),
                ))
        except Exception:  # noqa: BLE001
            return ()

    def __len__(self) -> int:
        with self._lock:
            return len(self._voices)


_REGISTRY = VoiceRegistry()
_REGISTRY_LOCK = threading.Lock()


def default_registry() -> VoiceRegistry:
    return _REGISTRY


def register_voice(name: str, min_density: object = DEFAULT_DENSITY,
                   **kw: object) -> Optional[Voice]:
    """Module-level convenience — the spelling producers use."""
    return _REGISTRY.register(name, min_density, **kw)  # type: ignore[arg-type]


def reset_for_tests() -> None:
    """Drop every voice and the discovery latch."""
    global _discovered
    with _REGISTRY_LOCK:
        _discovered = False
    _REGISTRY._voices.clear()  # noqa: SLF001 — test seam, same module


# ---------------------------------------------------------------------------
# The question every producer asks
# ---------------------------------------------------------------------------


def permits(name: str, *, density: Optional[Density] = None) -> Verdict:
    """May ``name`` speak right now, and why. NEVER raises.

    Cheap by construction: a dict lookup, an ordinal compare, and at most one
    `os.environ` read. It is called on posting paths, so it must never import,
    never walk packages, and never touch a database.
    """
    try:
        key = str(name or "").strip().lower()
        voice = _REGISTRY.get(key)
        if voice is None:
            # Rule 1 — fail OPEN. Muting the unrecognised would recreate the
            # very silent-drop this module exists to end.
            return Verdict(True, "unregistered")
        if voice.exempt:
            return Verdict(True, "exempt", voice)          # rule 2
        if voice.legacy_flag and voice.legacy_flag in os.environ:
            raw = os.environ.get(voice.legacy_flag, "").strip().lower()
            if raw in _FALSY:
                return Verdict(False, f"explicit:{voice.legacy_flag}", voice)
            if raw in _TRUTHY:
                return Verdict(True, f"explicit:{voice.legacy_flag}", voice)
            # Present but unparseable: fall through to the dial rather than
            # guessing. An operator typo should not pin a voice on or off.
        level = current_density() if density is None else density
        if level >= voice.min_density:
            return Verdict(True, f"density:{level.label}", voice)
        return Verdict(False, f"density:{level.label}", voice)
    except Exception:  # noqa: BLE001
        logger.debug("[NarrativeDensity] permits degraded for %r",
                     name, exc_info=True)
        return Verdict(True, "degraded")


def audible(name: str) -> bool:
    """`permits(...)` as a bare bool, for hot seams that only branch."""
    return bool(permits(name))


# ---------------------------------------------------------------------------
# The honesty surface
# ---------------------------------------------------------------------------

#: Packages whose direct submodules may own voices. Metadata about WHERE
#: voices live, not the voices themselves — adding a voice inside an
#: existing module needs zero edits here. Mirrors
#: `flag_registry_seed._FLAG_PROVIDER_PACKAGES`, deliberately: two walkers
#: with two conventions would be the duplication this module is arguing
#: against.
_VOICE_PROVIDER_PACKAGES: Tuple[str, ...] = (
    "backend.core.ouroboros.governance",
    "backend.core.ouroboros.battle_test",
)

_discovered = False


def ensure_discovered(*, force: bool = False) -> int:
    """Import-walk for voices declared by modules not yet loaded.

    Only the ROSTER needs this. Correctness does not: a producer registers
    its voices at its own import, so by the time it can speak it is in the
    registry. The walk exists so `/narrate` can list a voice belonging to a
    subsystem this session has not exercised yet — otherwise the surface
    would under-report exactly like the verb it replaces.

    Delegates to the existing AST-pinned discovery primitive rather than
    hand-rolling a second `pkgutil` walk. Idempotent. NEVER raises.
    """
    global _discovered
    try:
        with _REGISTRY_LOCK:
            if _discovered and not force:
                return len(_REGISTRY)
            _discovered = True
        from backend.core.ouroboros.governance.meta.module_discovery import (
            discover_module_provided_callable,
            make_registry_handler,
        )
        discover_module_provided_callable(
            packages=_VOICE_PROVIDER_PACKAGES,
            attr_name="register_voices",
            handler=make_registry_handler(registry=_REGISTRY),
            excluded_modules=(__name__,),
            log_prefix="NarrativeDensity",
        )
        return len(_REGISTRY)
    except Exception:  # noqa: BLE001
        logger.debug("[NarrativeDensity] discovery degraded", exc_info=True)
        return len(_REGISTRY)


@dataclass(frozen=True)
class RosterRow:
    voice: Voice
    verdict: Verdict


def roster(*, density: Optional[Density] = None,
           discover: bool = True) -> List[RosterRow]:
    """Every known voice and whether it is audible. NEVER raises.

    This is what `/narrate` should have been able to print from the start:
    the dial reporting what it actually reaches, rather than asserting a
    density nothing reads.
    """
    try:
        if discover:
            ensure_discovered()
        return [
            RosterRow(voice=v, verdict=permits(v.name, density=density))
            for v in _REGISTRY.all()
        ]
    except Exception:  # noqa: BLE001
        return []


def snapshot(*, discover: bool = True) -> dict:
    """Transport-safe projection for the cockpit / observability."""
    try:
        level = current_density()
        rows = roster(discover=discover)
        return {
            "schema_version": NARRATIVE_DENSITY_SCHEMA_VERSION,
            "density": level.label,
            "audible": [r.voice.name for r in rows if r.verdict.heard],
            "silenced": [r.voice.name for r in rows if not r.verdict.heard],
            "exempt": [r.voice.name for r in rows if r.voice.exempt],
            "overridden": [
                r.voice.name for r in rows
                if r.verdict.reason.startswith("explicit:")
            ],
        }
    except Exception:  # noqa: BLE001
        return {"density": DEFAULT_DENSITY.label, "audible": [],
                "silenced": [], "exempt": [], "overridden": []}


__all__ = [
    "DEFAULT_DENSITY",
    "DENSITY_ENV_VAR",
    "DENSITY_NAMES",
    "Density",
    "NARRATIVE_DENSITY_SCHEMA_VERSION",
    "RosterRow",
    "Verdict",
    "Voice",
    "VoiceRegistry",
    "audible",
    "coerce_density",
    "current_density",
    "default_registry",
    "ensure_discovered",
    "permits",
    "register_voice",
    "reset_for_tests",
    "roster",
    "set_density",
    "snapshot",
]
