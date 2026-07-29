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

_TRUTHY = ("1", "true", "yes", "on")


def voice_muted() -> bool:
    """Has the operator asked for silence? NEVER raises.

    Read fresh on every call rather than cached, so a mute takes effect on
    the next sentence rather than the next restart — someone reaching for
    silence is reaching for it now.
    """
    try:
        return os.environ.get(
            MASTER_MUTE_ENV_VAR, "0",
        ).strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


__all__ = ["MASTER_MUTE_ENV_VAR", "voice_muted"]
