"""Who owns the O+V lifecycle. One question, one answer, one place.

THE BUG THIS EXISTS TO MAKE IMPOSSIBLE
--------------------------------------
Two processes both booted ``GovernedLoopService``:

* ``ov`` → ``scripts/ouroboros_battle_test`` → GLS. Seconds, nothing else.
* ``unified_supervisor`` → ``_ensure_governance_pipeline()`` → GLS, as a
  fallback, 107 seconds into a boot dominated by GCP retries and audio
  degradation, under a 30-second stopwatch it then lost.

Observed 2026-08-15: the supervisor's copy hydrated the operator's dial,
exceeded its 30s budget by 1.8s, and was abandoned by ``wait_for`` — while
``asyncio.shield`` kept it running. The process then held
``_governed_loop = None`` and logged ``GLS failed:`` with an empty reason
(``TimeoutError`` stringifies to ""), so an orphaned governed loop was live
inside a process that believed governance was absent.

Neither owner was wrong. Having two was. This module makes ownership a
DECLARED fact rather than a race between whichever boot path ran first.

WHY A ROLE AND NOT A BOOLEAN
----------------------------
``JARVIS_GOVERNANCE_DISABLED=1`` on the supervisor would answer "should I
skip?" without ever saying who does it instead, and the second reader of
that flag would have to guess. A role names the owner, so every consumer
asks the same question and gets an answer that is true for the whole system:
``ov`` owns the loop, the supervisor owns the body (audio, vision, HUD).

BYPASS IS NOT DEGRADATION
-------------------------
A supervisor that skips governance because ``ov`` owns it is working exactly
as designed, and must not log a warning, set a failure reason, or report a
degraded mode. Warning about intended behaviour is how an operator learns to
ignore warnings — the same economics the audit ratchets are built on.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger("Ouroboros.LifecycleOwnership")

LIFECYCLE_OWNERSHIP_SCHEMA_VERSION: str = "lifecycle_ownership.1"

#: The `ov` cockpit process — `scripts/ouroboros_battle_test` boots the
#: 6-layer stack directly and imports nothing from the monolith.
OWNER_OV: str = "ov"

#: The 98K-line kernel. Owns the BODY — audio, vision, the HUD bridge — and
#: after 2026-08-15 no longer races `ov` for the governed loop.
OWNER_SUPERVISOR: str = "supervisor"

#: Every owner this system recognises. Closed, like `_VALID_SOURCES`: an
#: ownership vocabulary a deployment could invent privately is one nobody
#: else can reason about.
VALID_OWNERS: Tuple[str, ...] = (OWNER_OV, OWNER_SUPERVISOR)

#: `ov` by default — the process an operator starts when they want O+V, and
#: the one whose whole reason to exist is this loop. The supervisor booting
#: it was always the fallback, and a fallback that runs by default is just a
#: second owner wearing a modest name.
DEFAULT_OWNER: str = OWNER_OV

ENV_VAR: str = "JARVIS_GOVERNANCE_OWNER"

__all__ = [
    "DEFAULT_OWNER",
    "ENV_VAR",
    "LIFECYCLE_OWNERSHIP_SCHEMA_VERSION",
    "OWNER_OV",
    "OWNER_SUPERVISOR",
    "VALID_OWNERS",
    "bypass_note",
    "governance_owner",
    "owns_governance",
    "ov_owns_governance",
    "supervisor_owns_governance",
]


def governance_owner() -> str:
    """The declared owner of the O+V lifecycle. NEVER raises.

    An unrecognised value resolves to the default rather than failing the
    boot: this is read on a startup path, and a typo in one env var must not
    be able to take the organism down. It is logged at INFO with the value
    seen, because a silently-corrected setting is one nobody ever fixes.
    """
    try:
        raw = (os.environ.get(ENV_VAR) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return DEFAULT_OWNER
    if not raw:
        return DEFAULT_OWNER
    if raw in VALID_OWNERS:
        return raw
    logger.info(
        "[LifecycleOwnership] %s=%r is not one of %s — using %r",
        ENV_VAR, raw, list(VALID_OWNERS), DEFAULT_OWNER,
    )
    return DEFAULT_OWNER


def owns_governance(component: str) -> bool:
    """Does *component* own the governed loop in this deployment?

    The one predicate both construction sites consult. Two call sites asking
    the question two ways is how the dual ownership arose in the first place.
    """
    return governance_owner() == (component or "").strip().lower()


def supervisor_owns_governance() -> bool:
    return owns_governance(OWNER_SUPERVISOR)


def ov_owns_governance() -> bool:
    return owns_governance(OWNER_OV)


def bypass_note(component: str) -> str:
    """The line a bypassing component logs. INFO-shaped, never alarming.

    Says who DOES own it, so an operator reading a supervisor log that never
    mentions governance again is told where it went rather than left to
    conclude it broke.
    """
    owner = governance_owner()
    return (
        f"governance lifecycle owned by {owner!r}, not {component!r} — "
        f"skipping the governed loop here by design "
        f"(set {ENV_VAR}={OWNER_SUPERVISOR} to invert)"
    )
