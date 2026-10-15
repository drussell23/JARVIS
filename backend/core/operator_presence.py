"""
operator_presence - is there a human who can hear this machine right now?

One answer for every voice line in the process (:func:`speech_gate`, which
composes it with the master mute of backend/core/voice_mute) and for
everything else that asks (O+V's sensor governor and
narration, through ``cockpit_attach.operator_present``).

Why it is more than "is a cockpit attached"
-------------------------------------------
Presence used to be two arms: an ``ov`` cockpit attached over the bridge, or
this process owning a real terminal. That is right for O+V, which detaches
from its operator by design. It is wrong for the Body: JARVIS started in the
background (``jarvis.ps1 start``, a service, a login task) runs on the
operator's own desktop with no terminal of its own, so every truthful boot
line was silenced -- while the legacy narrators, which never asked, kept
talking (2026-10-15 11:19 boot: only "jarvis-prime linked" / "All systems
online" were said).

So presence has a third arm, read from the OS itself:

* **Windows** -- the session this process runs in (``ProcessIdToSessionId``)
  is an interactive one (not session 0, where services live) and its WTS
  connect state is ``WTSActive``: someone is at the console, or a Remote
  Desktop client is connected and receiving its audio. A disconnected RDP
  session is not presence -- nobody hears it.
* **macOS** -- the Quartz session dictionary says the session is on the
  console.
* elsewhere (and inside WSL, which cannot see the Windows session) the arm
  has no answer and the other arms decide, exactly as before.

Each arm answers True / False / None (cannot tell). Present when any arm
says True. Every question gets a fresh reading: each arm is a length, an
isatty or one OS call (microseconds), so a cache would only make the answer
stale -- an RDP client that just disconnected would still be "present". A
change of answer is logged once. The desktop arm can be switched off with
``JARVIS_PRESENCE_DESKTOP_SESSION=false``. NEVER raises.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def desktop_arm_enabled() -> bool:
    return (os.environ.get("JARVIS_PRESENCE_DESKTOP_SESSION", "true") or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class PresenceReading:
    present: bool
    by: str          # which arm decided ("" when none did)
    at: float        # time.monotonic() of the reading


# --- the arms ----------------------------------------------------------------

def _cockpit_attached() -> Optional[bool]:
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import attached_cockpits
        return attached_cockpits() > 0
    except Exception:  # noqa: BLE001
        return None


def _owns_terminal() -> Optional[bool]:
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import real_stdout_isatty
        return bool(real_stdout_isatty())
    except Exception:  # noqa: BLE001
        return None


_WTS_CONNECT_STATE = 8   # WTS_INFO_CLASS.WTSConnectState
_WTS_ACTIVE = 0          # WTS_CONNECTSTATE_CLASS.WTSActive


def _windows_session_active() -> Optional[bool]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        return None
    if session.value == 0:
        return False  # session 0 is for services: nobody sits at it
    wtsapi = ctypes.windll.wtsapi32
    buffer = ctypes.c_void_p()
    returned = wintypes.DWORD()
    if not wtsapi.WTSQuerySessionInformationW(None, session.value, _WTS_CONNECT_STATE,
                                              ctypes.byref(buffer), ctypes.byref(returned)):
        return None
    try:
        state = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value
    finally:
        wtsapi.WTSFreeMemory(buffer)
    return state == _WTS_ACTIVE


def _macos_session_on_console() -> Optional[bool]:
    try:
        from Quartz import CGSessionCopyCurrentDictionary  # pyobjc, where installed
    except Exception:  # noqa: BLE001
        return None
    session = CGSessionCopyCurrentDictionary()
    if not session:
        return False  # no GUI session for this process
    on_console = session.get("kCGSSessionOnConsoleKey")
    return None if on_console is None else bool(on_console)


def _desktop_session() -> Optional[bool]:
    if not desktop_arm_enabled():
        return None
    try:
        if sys.platform == "win32":
            return _windows_session_active()
        if sys.platform == "darwin":
            return _macos_session_on_console()
    except Exception:  # noqa: BLE001 -- unreadable: this arm has no answer
        logger.debug("[Presence] desktop session unreadable", exc_info=True)
    return None


#: (name, arm). Order is only the order reasons are reported in.
ARMS: List[Tuple[str, Callable[[], Optional[bool]]]] = [
    ("cockpit", _cockpit_attached),
    ("terminal", _owns_terminal),
    ("desktop_session", _desktop_session),
]


# --- the manager -------------------------------------------------------------

class PresenceManager:
    """Composes the arms into one reading per question. Thread-safe."""

    def __init__(self, arms: Optional[List[Tuple[str, Callable[[], Optional[bool]]]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._arms = arms if arms is not None else ARMS
        self._clock = clock
        self._lock = threading.Lock()
        self._last: Optional[PresenceReading] = None

    def read(self) -> PresenceReading:
        now = self._clock()
        with self._lock:
            last = self._last
        reading = PresenceReading(False, "", now)
        for name, arm in self._arms:
            try:
                answer = arm()
            except Exception:  # noqa: BLE001
                answer = None
            if answer:
                reading = PresenceReading(True, name, now)
                break
        with self._lock:
            changed = last is None or last.present != reading.present
            self._last = reading
        if changed:
            logger.info("[Presence] operator %s%s", "present" if reading.present else "absent",
                        f" ({reading.by})" if reading.by else "")
        return reading


_manager = PresenceManager()


def presence() -> PresenceReading:
    """The current reading. NEVER raises."""
    try:
        return _manager.read()
    except Exception:  # noqa: BLE001
        return PresenceReading(False, "", time.monotonic())


def operator_present() -> bool:
    """Is there a human who can perceive output right now? NEVER raises."""
    return presence().present


def speech_gate(*, urgent: bool = False) -> str:
    """Why a line must NOT be said through this machine's speakers right
    now, or "" when it may. THE gate every host-speech entry point asks --
    the queue the legacy narrators speak through as much as safe_say and the
    Trinity coordinator -- so no path talks to an empty room, or past a mute,
    because it never asked. NEVER raises.

    * the master mute (``voice_mute.voice_muted``) always wins;
    * no operator present (:func:`presence`: a cockpit, a terminal, or an
      active desktop / Remote Desktop session) silences
      everything except an *urgent* line.

    A line rendered for a client that plays it itself (the web app) does not
    ask: the client's presence is the answer, and only the mute applies.
    Presence fails OPEN like the mute: an unreadable presence never silences.
    """
    from backend.core.voice_mute import voice_muted
    if voice_muted():
        return "muted"
    if urgent:
        return ""
    try:
        reading = presence()
    except Exception:  # noqa: BLE001
        return ""
    return "" if reading.present else "no operator present"


__all__ = ["ARMS", "PresenceManager", "PresenceReading", "desktop_arm_enabled",
           "operator_present", "presence", "speech_gate"]
