"""
voice_persona - JARVIS has one voice; every engine renders it or stays silent.

JARVIS's voice is declared once: ``JARVIS_VOICE_NAME`` (canonically Daniel,
the macOS British English male voice). On macOS ``say`` has it. Elsewhere it
does not exist, and each engine fell back on its own: pyttsx3 to the first
installed voice (Windows' David), edge-tts to a hard-coded name map, the web
audio endpoint to silence (so the browser picked a voice of its own). The
operator heard three different people.

A persona is the name plus what it sounds like -- language and gender
(``JARVIS_VOICE_LOCALE`` / ``JARVIS_VOICE_GENDER``; on macOS the locale is
also read from the system catalogue). Each engine offers the voices it really
has, and :func:`choose` picks: the declared name if present, else the closest
match by locale and gender, else nothing -- an engine that cannot render the
persona does not speak (``JARVIS_ENFORCE_CANONICAL_VOICE``, default on, the
policy the coordinator already declared).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceOption:
    """One voice an engine can speak with."""
    id: str            # what the engine is told to use
    name: str          # display name
    locale: str = ""   # e.g. en-GB
    gender: str = ""   # "male" / "female" / ""


@dataclass(frozen=True)
class Persona:
    name: str
    locale: str = ""
    gender: str = ""

    def describe(self) -> str:
        bits = [b for b in (self.locale, self.gender) if b]
        return f"{self.name} ({', '.join(bits)})" if bits else self.name


def _norm_locale(value: str) -> str:
    return (value or "").strip().replace("_", "-").lower()


def _norm_gender(value: str) -> str:
    v = (value or "").strip().lower()
    return {"m": "male", "f": "female"}.get(v, v)


def say_voice_options() -> List[VoiceOption]:
    """The voices macOS `say` has (`say -v ?`, cached by system_voices);
    empty where there is no `say`."""
    try:
        from backend.core.system_voices import voice_catalog_raw
        raw = voice_catalog_raw() or ""
    except Exception:  # noqa: BLE001
        return []
    options = []
    for line in raw.splitlines():
        m = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Z]{2})\b", line)
        if m:
            name = m.group(1).strip()
            options.append(VoiceOption(id=name, name=name, locale=m.group(2)))
    return options


def say_voice(who: Optional["Persona"] = None) -> Optional[str]:
    """The `say` voice that renders *who* (default: JARVIS's persona), or None
    -- never "the first voice installed"."""
    choice = choose(say_voice_options(), who)
    return choice.id if choice else None


def _catalogue_locale(name: str) -> str:
    """The locale the OS voice catalogue (macOS `say -v ?`) lists for *name*."""
    for option in say_voice_options():
        if option.name.lower() == name.lower():
            return option.locale
    return ""


def persona() -> Persona:
    """JARVIS's declared voice."""
    name = (os.environ.get("JARVIS_VOICE_NAME") or os.environ.get("JARVIS_CANONICAL_VOICE_NAME")
            or "Daniel").strip()
    locale = os.environ.get("JARVIS_VOICE_LOCALE", "").strip() or _catalogue_locale(name)
    gender = os.environ.get("JARVIS_VOICE_GENDER", "").strip()
    return Persona(name=name, locale=_norm_locale(locale), gender=_norm_gender(gender))


def enforce_persona() -> bool:
    return (os.environ.get("JARVIS_ENFORCE_CANONICAL_VOICE", "true").strip().lower()
            in ("1", "true", "yes", "on"))


def choose(options: Iterable[VoiceOption], who: Optional[Persona] = None) -> Optional[VoiceOption]:
    """The option that renders *who* (default: JARVIS's persona), or None.

    Exact name first; then locale + gender; then locale alone when no gender
    is declared. Never "the first voice installed" -- that is someone else.
    """
    who = who or persona()
    opts: List[VoiceOption] = list(options)
    target = who.name.lower()
    for o in opts:
        if target and (o.name.lower() == target or o.id.lower() == target
                       or re.search(rf"\b{re.escape(target)}\b", o.name.lower())):
            return o
    if not who.locale:
        return None
    same_locale = [o for o in opts if _norm_locale(o.locale) == who.locale]
    if who.gender:
        same_locale = [o for o in same_locale if _norm_gender(o.gender) == who.gender]
    return same_locale[0] if same_locale else None


# --- pyttsx3: the OS speech engine (SAPI5 / NSSpeech / eSpeak) ---------------

def pyttsx3_persona_engine():
    """``(engine, options, error)`` for a pyttsx3 engine set to the persona.

    BLOCKING -- call it in a worker thread, and keep that engine's COM work
    there: on Windows the engine and its voices are COM objects, and one
    released on another thread (the event loop, by the garbage collector)
    blocks forever in comtypes -- it wedged boot. Only plain data, and the
    engine when it is kept, leave this function.

    ``engine`` is None when pyttsx3 failed (``error`` says why) or when no
    installed voice renders the persona under :func:`enforce_persona` -- told
    to use "Daniel", Windows SAPI silently speaks as David.
    """
    try:
        import pyttsx3

        engine = pyttsx3.init()
        options = [
            VoiceOption(id=str(v.id), name=str(getattr(v, "name", "") or v.id),
                        locale=str((list(getattr(v, "languages", None) or [""])[0]) or ""),
                        gender=str(getattr(v, "gender", "") or ""))
            for v in (engine.getProperty("voices") or [])
        ]
        choice = choose(options)
        if choice is None and enforce_persona():
            engine.stop()
            del engine
            import gc
            gc.collect()  # release the declined engine here, on its own thread
            return None, options, f"no installed voice renders {persona().describe()}"
        if choice is not None:
            engine.setProperty("voice", choice.id)
        return engine, options, ""
    except Exception as exc:  # noqa: BLE001 -- not installed / no audio stack
        return None, [], str(exc) or type(exc).__name__


# --- edge-tts: the cross-platform neural voices -------------------------------

_edge_options: Optional[List[VoiceOption]] = None
_edge_lock = asyncio.Lock()


async def edge_tts_voice(who: Optional[Persona] = None) -> Optional[str]:
    """The edge-tts voice id that renders *who* (default: JARVIS's persona;
    catalogue fetched once per process), or None if edge-tts is unreachable
    or has no such voice."""
    global _edge_options
    async with _edge_lock:
        if _edge_options is None:
            try:
                import edge_tts
                raw = await edge_tts.list_voices()
                _edge_options = [
                    VoiceOption(id=v["ShortName"], name=v.get("FriendlyName") or v["ShortName"],
                                locale=v.get("Locale", ""), gender=v.get("Gender", ""))
                    for v in raw
                ]
            except Exception as exc:  # noqa: BLE001 -- offline / not installed
                logger.info("[VoicePersona] edge-tts voices unavailable: %s", exc)
                return None
    choice = choose(_edge_options, who)
    return choice.id if choice else None


__all__ = ["Persona", "VoiceOption", "choose", "edge_tts_voice", "enforce_persona", "persona", "say_voice", "say_voice_options",
           "pyttsx3_persona_engine"]
