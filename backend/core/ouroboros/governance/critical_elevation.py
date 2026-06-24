"""CRITICAL_ELEVATION governance state + the Immutable Orange Protocol
(G3 of the Sovereign Cross-Repo Mutator — the operator lock).

This module is the gating substrate that MUST exist before any cross-repo
write is possible. It defines:

1. **CRITICAL_ELEVATION** — a governance tier conceptually ABOVE
   ``approval_required``: a PR is created but the merge is HARD-HALTED for
   explicit operator approval regardless of CI / test / sandbox status.
   Even an all-green PR will NOT auto-merge while elevated.

2. **The Immutable Orange Protocol (Sovereign Law).** Mutations targeting
   ``prime`` (Mind) or ``reactor`` (Nerves) can NEVER reach auto-merge —
   structurally, by any flag, forever. They are PERMANENTLY floored at
   ``approval_required``. This is the ONE intentional hardcode: a safety
   constant that NO env reads, NO graduation bypasses, NO trust level
   relaxes. "It can write its own brain; it can never merge its own brain."

3. The graduation gate for ``jarvis`` (Body): until the Adaptive Trust
   Ledger says the Body has earned trust, a cross-repo jarvis op is held at
   ``critical_elevation`` (hard-halt). Once graduated, the cross-repo floor
   falls away (``None``) and the op flows through the normal pipeline (the
   Body may auto-merge).

Fail-CLOSED everywhere: any error degrades to the MORE restrictive floor
(``critical_elevation`` for jarvis, ``approval_required`` for prime/reactor)
— a cross-repo mutation can never become *less* gated through a failure.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("Ouroboros.CriticalElevation")

# Canonical tier names (lowercase — mirrors risk_tier_floor._ORDER).
CRITICAL_ELEVATION = "critical_elevation"
APPROVAL_REQUIRED = "approval_required"

# ---------------------------------------------------------------------------
# IMMUTABLE ORANGE — Sovereign Law (the ONE intentional hardcode).
# Mind (prime) + Nerves (reactor) are PERMANENTLY human-merged. This set is
# evaluated with NO env override, NO graduation bypass, NO trust-level
# bypass. Do NOT make this env-configurable — that would break the Sovereign
# Law (§5.3 / §6 of the spec).
# ---------------------------------------------------------------------------
_IMMUTABLE_ORANGE_REPOS = frozenset({"prime", "reactor"})

# The Body — the ONLY repo that may ever graduate below Orange to silent
# auto-merge.
_BODY_REPO = "jarvis"

_ENV_CRITICAL_ELEVATION = "JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED"

# Only these tokens explicitly disable the jarvis hard-halt (fail-CLOSED:
# garbage / typo values are treated as enabled, never open the halt).
_FALSY = frozenset({"0", "false", "no", "off"})


class GovernanceTier:
    """String-const extension of the risk-tier ladder.

    These mirror the canonical lowercase tier names. ``CRITICAL_ELEVATION``
    sits conceptually between ``APPROVAL_REQUIRED`` and ``BLOCKED`` — more
    restrictive than approval_required (a hard-halt) but NOT a permanent
    block. The numeric ordering is owned by ``risk_tier_floor._ORDER``."""

    SAFE_AUTO = "safe_auto"
    NOTIFY_APPLY = "notify_apply"
    APPROVAL_REQUIRED = "approval_required"
    CRITICAL_ELEVATION = "critical_elevation"
    BLOCKED = "blocked"


def is_critical_elevation_enabled() -> bool:
    """Master flag for the *jarvis* CRITICAL_ELEVATION hard-halt
    (``JARVIS_CROSS_REPO_CRITICAL_ELEVATION_ENABLED``, default true).

    Fail-CLOSED: only an EXPLICIT falsy token disables the jarvis hard-halt.
    Unset -> default true (enabled). A garbage/typo value (e.g. ``GARBAGE``,
    ``maybe``, ``1.5``) resolves to ENABLED (never silently open the halt).

      * disabled only when: ``value.strip().lower() in {"0","false","no","off"}``
      * anything else (including unset, ``true``, ``1``, ``GARBAGE``) -> enabled.

    NOTE: this flag governs ONLY the jarvis hard-halt. The Immutable Orange
    floor for prime/reactor is NOT gated by it — even with this flag off,
    prime/reactor stay >= approval_required (permanent Sovereign Law)."""
    raw = os.environ.get(_ENV_CRITICAL_ELEVATION)
    if raw is None:
        # Unset -> default enabled.
        return True
    return raw.strip().lower() not in _FALSY


def _is_immutable_orange(target_repo: str) -> bool:
    """True iff the target repo is Mind (prime) or Nerves (reactor).

    Normalised, but evaluated WITHOUT any env input — this is the hardcoded
    Sovereign Law."""
    return (target_repo or "").strip().lower() in _IMMUTABLE_ORANGE_REPOS


def cross_repo_elevation_floor(
    *,
    target_repo: str,
    crosses_repo: bool,
) -> Optional[str]:
    """Return the cross-repo governance floor for an op, or ``None``.

    Resolution order (the law dominates, evaluated FIRST):

      1. **IMMUTABLE ORANGE (Sovereign Law):** ``target_repo in
         {prime, reactor}`` -> ``"approval_required"`` as a HARD floor —
         NO env override, NO graduation bypass, NO trust-level bypass.
         Un-disableable by design (Mind/Nerves can NEVER auto-merge). This
         is evaluated before any flag check or ledger lookup so it can never
         be skipped by an error elsewhere. NOTE: targeting prime/reactor is
         ITSELF a cross into the Mind/Nerves, so the law dominates even when
         the caller passes ``crosses_repo=False`` (a contradictory hint can
         never relax the law).

      2. ``crosses_repo == False`` -> ``None`` (single-repo *jarvis* op,
         untouched). Only reached for non-immutable-orange targets.

      3. ``target_repo == "jarvis"`` crossing a boundary:
           * the master flag is OFF -> ``None`` (no jarvis hard-halt);
           * NOT graduated -> ``"critical_elevation"`` (hard-halt);
           * graduated -> ``None`` (Body may auto-merge — normal flow).

      4. Any other (unknown) repo crossing a boundary -> fail-CLOSED to
         ``"critical_elevation"`` (never relax on an unrecognised target).

    Fail-CLOSED: any error -> the MOST restrictive floor for the class
    (``approval_required`` for prime/reactor, ``critical_elevation`` for
    jarvis/unknown).
    """
    # (1) The Sovereign Law — evaluated FIRST, before anything that could
    # raise. Un-bypassable.
    if _is_immutable_orange(target_repo):
        return APPROVAL_REQUIRED

    # (2) Single-repo ops are untouched.
    if not crosses_repo:
        return None

    repo_norm = (target_repo or "").strip().lower()

    # (3) Body (jarvis) — gated by the Trust Ledger + the master flag.
    if repo_norm == _BODY_REPO:
        try:
            if not is_critical_elevation_enabled():
                # The flag governs only the jarvis hard-halt.
                return None
            from backend.core.ouroboros.governance.cross_repo_trust_ledger import (  # noqa: E501
                get_cross_repo_trust_ledger,
            )
            if get_cross_repo_trust_ledger().is_graduated(_BODY_REPO):
                return None
            return CRITICAL_ELEVATION
        except Exception:  # noqa: BLE001 — fail-CLOSED
            logger.debug(
                "[CriticalElevation] jarvis floor lookup failed — "
                "fail-closed to critical_elevation", exc_info=True,
            )
            return CRITICAL_ELEVATION

    # (4) Unknown repo crossing a boundary — fail-CLOSED.
    logger.debug(
        "[CriticalElevation] unknown target_repo=%r crossing boundary — "
        "fail-closed to critical_elevation", target_repo,
    )
    return CRITICAL_ELEVATION


def record_cross_repo_outcome(
    *,
    repo: str,
    pr_id: str,
    outcome: str,
    complexity: float,
) -> None:
    """Convenience: route a cross-repo integration outcome to the Trust
    Ledger. Fail-soft (NEVER raises)."""
    try:
        from backend.core.ouroboros.governance.cross_repo_trust_ledger import (
            get_cross_repo_trust_ledger,
        )
        get_cross_repo_trust_ledger().record_outcome(
            repo=repo, pr_id=pr_id, outcome=outcome, complexity=complexity,
        )
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug(
            "[CriticalElevation] record_cross_repo_outcome failed",
            exc_info=True,
        )


__all__ = [
    "APPROVAL_REQUIRED",
    "CRITICAL_ELEVATION",
    "GovernanceTier",
    "cross_repo_elevation_floor",
    "is_critical_elevation_enabled",
    "record_cross_repo_outcome",
]
