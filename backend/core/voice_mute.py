"""One answer to "be quiet", for every speech path in the process.

The first attempt put the mute in `unified_voice_orchestrator.safe_say`,
on the strength of that function's own docstring calling itself "the
canonical safe speech function for the entire JARVIS process".

It is not. Verified after the operator reported still hearing her:

    voice_orchestrator.speak / .announce
    shared_voice_client.announce
    cross_repo_voice_client.announce_*        (9 of them)
    trinity_voice_coordinator engines         (pyttsx3, say -o + afplay)

A docstring claiming canonicity is a claim, not a fact, and trusting one
is how a mute shipped that did not mute. This module exists so the answer
lives somewhere with NO dependencies — importable from any speech path
without a cycle, and therefore actually reachable by all of them.

Fails OPEN. An unreadable flag must never silence the organism, or a
transient fault becomes permanent silence nobody can explain.
"""
from __future__ import annotations

import os

MASTER_MUTE_ENV_VAR = "JARVIS_VOICE_MUTED"

#: Runtime mute. A FILE, because the process that is talking is usually a
#: daemon that has been running for hours.
#:
#: The env var alone could not silence it, and that is not a bug in the
#: flag — it is what an environment IS. `export` affects processes started
#: AFTERWARDS, in that shell; a running process's environment was fixed at
#: its launch. The operator exported the variable, the daemon (already up,
#: headless, started from a different shell) never saw it, and kept
#: talking. Correct behaviour, useless outcome.
#:
#: A sentinel file is observable by a process that is already running,
#: needs no IPC, no port, no protocol, and survives the daemon outliving
#: the terminal that started it — which is the specific condition under
#: which someone actually wants silence.
SENTINEL_NAME = "voice_muted"

_TRUTHY = ("1", "true", "yes", "on")


def sentinel_paths() -> tuple:
    """Where a runtime mute may be declared. NEVER raises.

    Repo-local first (per-checkout, what a soak reads), then the user's
    home (machine-wide, what someone types once and forgets). Either
    silences: an operator asking twice for quiet should not have to learn
    which one this daemon happens to read.
    """
    try:
        here = os.path.join(os.getcwd(), ".jarvis", SENTINEL_NAME)
        home = os.path.join(
            os.path.expanduser("~"), ".jarvis", SENTINEL_NAME)
        return (here, home)
    except Exception:  # noqa: BLE001
        return ()


def voice_muted() -> bool:
    """Has the operator asked for silence? NEVER raises.

    Read fresh on every call — env AND sentinel — so a mute takes effect
    on the next sentence rather than the next restart. A `stat` costs
    microseconds and sentences are seconds apart, so there is nothing to
    cache and a cache would only delay the one thing being asked for.
    """
    try:
        if os.environ.get(
                MASTER_MUTE_ENV_VAR, "0").strip().lower() in _TRUTHY:
            return True
    except Exception:  # noqa: BLE001
        pass
    for path in sentinel_paths():
        try:
            if os.path.exists(path):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def mute(scope: str = "repo") -> str:
    """Create the sentinel. Returns the path written, or "". NEVER raises."""
    try:
        target = sentinel_paths()[0 if scope == "repo" else 1]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("silenced by the operator\n")
        return target
    except Exception:  # noqa: BLE001
        return ""


def unmute() -> int:
    """Remove every sentinel. Returns how many were cleared. NEVER raises.

    ALL of them, not the first found: a half-cleared mute that still
    silences is exactly as confusing as a mute that does not.
    """
    cleared = 0
    for path in sentinel_paths():
        try:
            if os.path.exists(path):
                os.remove(path)
                cleared += 1
        except Exception:  # noqa: BLE001
            continue
    return cleared


__all__ = [
    "MASTER_MUTE_ENV_VAR",
    "SENTINEL_NAME",
    "mute",
    "sentinel_paths",
    "unmute",
    "voice_muted",
]
